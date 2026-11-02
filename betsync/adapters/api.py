"""The HTTP API. Thin by design: routing and validation here, everything else in ReadService."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from ..application.read import ReadService
from ..domain import BetStatus


def create_app(service: ReadService, engine=None, upstream=None) -> FastAPI:
    """Build the FastAPI app over a read service. If a sync engine is given, it starts and stops
    with the app; if a simulator app is given, it's mounted at /upstream.
    """
    @asynccontextmanager
    async def lifespan(app):
        """Start the sync engine when the app starts and stop it when the app shuts down."""
        if engine:
            engine.start()
        yield
        if engine:
            engine.shutdown()

    app = FastAPI(
        title="Bet sync engine",
        description="Bets from two sportsbooks, synced into one canonical view, with freshness on every answer.",
        version="1.0",
        lifespan=lifespan,
    )

    @app.get("/bets", summary="Canonical bets, newest first, with each sportsbook's freshness")
    def list_bets(sportsbook: str | None = Query(None, description="northstar or cascade"),
                  status: BetStatus | None = None,
                  event_id: str | None = Query(None, description="only bets with a leg on this game"),
                  limit: int = Query(100, ge=1, le=500),
                  offset: int = Query(0, ge=0)):
        """List canonical bets, filtered by sportsbook, status or game, with each book's freshness."""
        return service.bets(sportsbook, status.value if status else None, event_id, limit, offset)

    @app.get("/bets/{bet_id}", summary="One bet, with every observation that shaped it")
    def get_bet(bet_id: str):
        """Return one bet with its observation history, or 404 if we've never seen it."""
        bet = service.bet(bet_id)
        if bet is None:
            raise HTTPException(404, f"No bet {bet_id!r}. IDs look like northstar:NS-10007 or cascade:77012.")
        return bet

    @app.get("/events", summary="Canonical games, with each sportsbook's own ID for them")
    def list_events():
        """List canonical games, with each book's own ID for them and how many bets touch them."""
        return service.events()

    @app.get("/status", summary="Is synchronisation healthy, degraded, stale or failing?")
    def status():
        """Report sync health per book and overall."""
        return service.status()

    @app.get("/healthz", summary="Liveness: the API is up (see /status for sync health)")
    def healthz():
        """Liveness check: the API process is up. Sync health is in /status."""
        return {"ok": True}

    @app.post("/sync", status_code=202, summary="Run a sync cycle now instead of waiting")
    def sync_now(provider: str | None = Query(None, description="one sportsbook, or all if left out")):
        """Wake one book's worker, or all of them, to run a sync cycle now instead of waiting."""
        if engine is None:
            raise HTTPException(409, "No sync engine is running in this process.")
        if provider and provider not in engine.workers:
            raise HTTPException(404, f"Unknown sportsbook {provider!r}.")
        engine.trigger(provider)
        return {"triggered": [provider] if provider else sorted(engine.workers)}

    if upstream is not None:
        app.mount("/upstream", upstream)   # the simulated sportsbooks, served by the same process

    return app
