"""Serves the simulated sportsbooks over HTTP (a plain Starlette app, kept apart from our own API).

    GET  /northstar/v1/bets               Northstar's bets
    GET  /cascade/api/tickets/changes     Cascade's change feed
    GET  /cascade/api/tickets/{ticket}    one Cascade ticket
    GET  /{book}/_control                 the book's current fault settings
    POST /{book}/_control                 change them, e.g. {"outage": true} or {"error_rate": 0.5}
"""
import asyncio
from dataclasses import fields

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .faults import FaultProfile


def create_app(sim) -> Starlette:
    async def send(resp):
        if resp.delay:
            await asyncio.sleep(resp.delay)
        return JSONResponse(resp.body, status_code=resp.status, headers=resp.headers)

    async def northstar_bets(request: Request):
        return await send(sim.northstar.list_bets(dict(request.query_params)))

    async def cascade_changes(request: Request):
        return await send(sim.cascade.changes(dict(request.query_params)))

    async def cascade_ticket(request: Request):
        return await send(sim.cascade.ticket(int(request.path_params["ticket"])))

    async def control(request: Request):
        book = sim.books.get(request.path_params["book"])
        if not book:
            return JSONResponse({"error": "unknown sportsbook"}, status_code=404)
        if request.method == "POST":
            changes = await request.json()
            allowed = {f.name for f in fields(FaultProfile)}
            unknown = set(changes) - allowed
            if unknown:
                return JSONResponse({"error": f"unknown settings: {sorted(unknown)}", "allowed": sorted(allowed)},
                                    status_code=400)
            book.faults.update(**changes)
        return JSONResponse({"sportsbook": book.name, "scenario": sim.scenario, "faults": book.faults.profile.to_dict(),
                             "simulated_time": sim.clock.now().isoformat()})

    return Starlette(routes=[
        Route("/northstar/v1/bets", northstar_bets),
        Route("/cascade/api/tickets/changes", cascade_changes),
        Route("/cascade/api/tickets/{ticket:int}", cascade_ticket),
        Route("/{book}/_control", control, methods=["GET", "POST"]),
    ])
