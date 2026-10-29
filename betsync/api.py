"""The HTTP API. Thin by design: routing and validation here, everything else in ReadService."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from .domain import BetStatus
from .service import ReadService


def create_app(service: ReadService, engine=None, upstream=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
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
        return service.bets(sportsbook, status.value if status else None, event_id, limit, offset)

    @app.get("/bets/{bet_id}", summary="One bet, with every observation that shaped it")
    def get_bet(bet_id: str):
        bet = service.bet(bet_id)
        if bet is None:
            raise HTTPException(404, f"No bet {bet_id!r}. IDs look like northstar:NS-10007 or cascade:77012.")
        return bet

    @app.get("/events", summary="Canonical games, with each sportsbook's own ID for them")
    def list_events():
        return service.events()

    @app.get("/status", summary="Is synchronisation healthy, degraded, stale or failing?")
    def status():
        return service.status()

    @app.get("/healthz", summary="Liveness: the API is up (see /status for sync health)")
    def healthz():
        return {"ok": True}

    @app.post("/sync", status_code=202, summary="Run a sync cycle now instead of waiting")
    def sync_now(provider: str | None = Query(None, description="one sportsbook, or all if left out")):
        if engine is None:
            raise HTTPException(409, "No sync engine is running in this process.")
        if provider and provider not in engine.workers:
            raise HTTPException(404, f"Unknown sportsbook {provider!r}.")
        engine.trigger(provider)
        return {"triggered": [provider] if provider else sorted(engine.workers)}

    if upstream is not None:
        app.mount("/upstream", upstream)   # the simulated sportsbooks, served by the same process

    return app
