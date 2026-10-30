"""End to end over real HTTP: simulated books on a real server, the real HTTP client, the sync
engine's worker threads, and the read service. Real timeouts, real 429s, a real outage."""
import os
import socket
import threading
import time
import unittest
from datetime import datetime, timezone

import requests
import uvicorn

from betsync import providers
from betsync.providers.base import RateLimited, TransientError
from betsync.service import FreshnessPolicy, ReadService
from betsync.simulator import ScaledClock, Simulator
from betsync.simulator.http import create_app
from betsync.sync import SyncEngine, SyncPolicy

from .support import temp_store


def free_port():
    """A local port nothing is listening on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(check, timeout=15.0, every=0.1):
    """Poll check until it returns True or the timeout passes. Returns whether it succeeded."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(every)
    return False


class HttpEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Serve the simulated books on a real local HTTP server, with time running 600x faster."""
        cls.sim = Simulator(ScaledClock(datetime.now(timezone.utc), speed=600), scenario="calm", seed=7)
        port = free_port()
        cls.base = f"http://127.0.0.1:{port}"
        cls.server = uvicorn.Server(uvicorn.Config(create_app(cls.sim), host="127.0.0.1", port=port,
                                                   log_level="critical", timeout_graceful_shutdown=1))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        assert wait_for(lambda: cls.server.started), "simulator server didn't start"

    @classmethod
    def tearDownClass(cls):
        """Stop the server."""
        cls.server.should_exit = True
        cls.thread.join(5)

    def setUp(self):
        """Clear any faults from earlier tests and give each test a fresh store."""
        for book in self.sim.books.values():
            book.faults.update(outage=False, error_rate=0, timeout_rate=0, rate_limit_per_sec=0)
        self.store, path = temp_store()
        self.addCleanup(lambda: [os.remove(path + s) for s in ("", "-wal", "-shm") if os.path.exists(path + s)])

    def control(self, book, **changes):
        """Change a book's fault settings through its _control endpoint."""
        r = requests.post(f"{self.base}/{book}/_control", json=changes, timeout=5)
        self.assertEqual(r.status_code, 200, r.text)

    def test_client_turns_http_trouble_into_typed_errors(self):
        """Real timeouts, 429s and refused connections become the right typed errors."""
        client = providers.HttpClient(f"{self.base}/cascade", timeout=0.5)
        self.control("cascade", timeout_rate=1.0)
        started = time.monotonic()
        with self.assertRaises(TransientError):
            client.get_json("/api/tickets/changes", {})
        self.assertLess(time.monotonic() - started, 3)        # gave up at our timeout, not the server's hang
        self.control("cascade", timeout_rate=0.0, rate_limit_per_sec=1)
        with self.assertRaises(RateLimited) as ctx:
            for _ in range(5):
                client.get_json("/api/tickets/changes", {})
        self.assertEqual(ctx.exception.retry_after, 1.0)
        with self.assertRaises(TransientError):
            providers.HttpClient("http://127.0.0.1:1", timeout=0.5).get_json("/", {})   # nothing listening

    def test_sync_engine_end_to_end_with_an_outage(self):
        """With real threads and HTTP, both books sync; an outage takes Cascade from degraded to stale
        while Northstar stays ok; and Cascade recovers on its own when the outage ends.
        """
        adapters = providers.build(self.base, timeout=0.5)
        engine = SyncEngine(adapters, self.store, SyncPolicy(poll_interval=0.2, backoff_base=0.05, backoff_cap=0.2,
                                                             failure_backoff_cap=0.5))
        service = ReadService(self.store, ["northstar", "cascade"], FreshnessPolicy(stale_after=1.5))
        engine.start()
        self.addCleanup(engine.shutdown)
        status = lambda name: service.status()["providers"][name]["status"]

        self.assertTrue(wait_for(lambda: service.status()["status"] == "ok"), service.status())
        counts = service.status()["bets"]
        self.assertGreater(sum(counts["northstar"].values()), 0)
        self.assertGreater(sum(counts["cascade"].values()), 0)

        # Cascade goes down: Northstar carries on; Cascade degrades, then goes stale, but its bets are still served.
        self.control("cascade", outage=True)
        self.assertTrue(wait_for(lambda: status("cascade") == "degraded", timeout=5))
        self.assertTrue(wait_for(lambda: status("cascade") == "stale", timeout=5))
        self.assertEqual(status("northstar"), "ok")
        stale_bets = service.bets(sportsbook="cascade")["bets"]
        self.assertTrue(stale_bets and all(b["freshness"]["stale"] for b in stale_bets))
        self.assertIn("503", service.status()["providers"]["cascade"]["last_error"])

        # And recovers on its own once it's back.
        self.control("cascade", outage=False)
        self.assertTrue(wait_for(lambda: status("cascade") == "ok", timeout=5), service.status()["providers"]["cascade"])


if __name__ == "__main__":
    unittest.main()
