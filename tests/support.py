"""Test helpers: run the simulated books in-process, without HTTP, but with the same failures."""
import os
import random
import tempfile
from datetime import datetime, timezone

from betsync.providers import CascadeProvider, NorthstarProvider
from betsync.providers.base import PermanentError, RateLimited, TransientError
from betsync.simulator import ManualClock, Simulator
from betsync.store import Store
from betsync.sync import SyncPolicy, SyncWorker

START = datetime(2026, 11, 1, 19, 0, tzinfo=timezone.utc)
CLIENT_TIMEOUT = 3.0


def in_process(routes, timeout=CLIENT_TIMEOUT):
    """A get_json for an adapter that calls a simulated book directly. Mirrors providers/http.py:
    a response slower than the timeout is a timeout, 429 is rate limited, 5xx transient, 4xx permanent.
    routes: (path) -> handler(params) or None."""
    def get_json(path, params):
        handler = routes(path)
        if handler is None:
            raise PermanentError(f"no route for {path}")
        resp = handler({k: str(v) for k, v in params.items() if v is not None})
        if resp.delay > timeout:
            raise TransientError(f"timed out after {timeout}s")
        if resp.status == 429:
            raise RateLimited("rate limited (429)", float(resp.headers.get("Retry-After", 1)))
        if resp.status >= 500:
            raise TransientError(f"server error ({resp.status})")
        if resp.status >= 400:
            raise PermanentError(f"request rejected ({resp.status})")
        return resp.body
    return get_json


def temp_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Store(path), path


class Rig:
    """A simulator, a store and a worker per book, all in-process with a manual clock. Waits are skipped."""

    def __init__(self, scenario="calm", seed=7, policy=None):
        self.clock = ManualClock(START)
        self.sim = Simulator(self.clock, scenario=scenario, seed=seed)
        # no real rate-limit windows in-process: requests come too fast; test 429 handling directly instead
        for book in self.sim.books.values():
            book.faults.update(rate_limit_per_sec=0)
        self.store, self.path = temp_store()
        sim = self.sim

        def northstar_routes(path):
            return sim.northstar.list_bets if path == "/v1/bets" else None

        def cascade_routes(path):
            if path == "/api/tickets/changes":
                return sim.cascade.changes
            tail = path.rsplit("/", 1)[-1]
            return (lambda params: sim.cascade.ticket(int(tail))) if tail.isdigit() else None

        self.providers = {
            "northstar": NorthstarProvider(in_process(northstar_routes)),
            "cascade": CascadeProvider(in_process(cascade_routes)),
        }
        policy = policy or SyncPolicy(attempts=6)
        self.workers = {name: SyncWorker(p, self.store, policy, now=lambda: self.clock.now(), rng=random.Random(1))
                        for name, p in self.providers.items()}
        for w in self.workers.values():
            w.stop.wait = lambda seconds: False   # don't actually sleep between retries

    def sync(self, name=None):
        return {n: w.run_cycle() for n, w in self.workers.items() if name in (None, n)}

    def close(self):
        try:
            os.remove(self.path)
            for suffix in ("-wal", "-shm"):
                if os.path.exists(self.path + suffix):
                    os.remove(self.path + suffix)
        except OSError:
            pass
