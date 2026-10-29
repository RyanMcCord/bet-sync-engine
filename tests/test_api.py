"""The HTTP API, through FastAPI's test client."""
import unittest

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:   # pragma: no cover
    HAVE_FASTAPI = False

from betsync.service import ReadService

from .support import Rig


class FakeEngine:
    def __init__(self):
        self.workers, self.triggered = {"northstar": None, "cascade": None}, []

    def start(self):
        pass

    def shutdown(self):
        pass

    def trigger(self, name=None):
        self.triggered.append(name)


@unittest.skipUnless(HAVE_FASTAPI, "FastAPI isn't installed (pip install -r requirements.txt)")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from betsync.api import create_app
        cls.rig = Rig("realistic")
        for _ in range(30):
            cls.rig.sync()
            cls.rig.clock.advance(minutes=30)
        cls.rig.sync()
        cls.engine = FakeEngine()
        service = ReadService(cls.rig.store, ["northstar", "cascade"], now=lambda: cls.rig.clock.now())
        cls.client = TestClient(create_app(service, cls.engine))

    @classmethod
    def tearDownClass(cls):
        cls.rig.close()

    def test_list_and_filter_bets(self):
        body = self.client.get("/bets").json()
        self.assertEqual(set(body["freshness"]), {"northstar", "cascade"})
        self.assertGreater(body["count"], 0)
        bet = body["bets"][0]
        for key in ("id", "sportsbook", "status", "stake_cents", "odds_american", "legs", "freshness"):
            self.assertIn(key, bet)
        won = self.client.get("/bets", params={"status": "won", "sportsbook": "cascade"}).json()["bets"]
        self.assertTrue(won and all(b["status"] == "won" and b["sportsbook"] == "cascade" for b in won))
        self.assertEqual(self.client.get("/bets", params={"status": "maybe"}).status_code, 422)
        self.assertEqual(len(self.client.get("/bets", params={"limit": 3}).json()["bets"]), 3)

    def test_filter_by_game_spans_both_books(self):
        event = max(self.client.get("/events").json()["events"], key=lambda e: e["bets"])
        bets = self.client.get("/bets", params={"event_id": event["id"], "limit": 500}).json()["bets"]
        self.assertEqual(len(bets), event["bets"])
        self.assertTrue(all(any(l["event_id"] == event["id"] for l in b["legs"]) for b in bets))

    def test_one_bet_with_history(self):
        bet_id = self.client.get("/bets", params={"status": "lost"}).json()["bets"][0]["id"]
        body = self.client.get(f"/bets/{bet_id}").json()
        self.assertEqual(body["id"], bet_id)
        self.assertEqual(body["history"][-1]["outcome"], "inserted")
        missing = self.client.get("/bets/northstar:NOPE")
        self.assertEqual(missing.status_code, 404)

    def test_status_and_health(self):
        status = self.client.get("/status").json()
        self.assertIn(status["status"], ("ok", "degraded", "stale", "never_synced"))
        for name in ("northstar", "cascade"):
            p = status["providers"][name]
            for key in ("status", "last_success_at", "consecutive_failures", "totals", "recent_runs"):
                self.assertIn(key, p)
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})

    def test_trigger_sync(self):
        self.assertEqual(self.client.post("/sync", params={"provider": "cascade"}).status_code, 202)
        self.assertEqual(self.engine.triggered[-1], "cascade")
        self.assertEqual(self.client.post("/sync", params={"provider": "nope"}).status_code, 404)
        self.assertEqual(self.client.post("/sync").json(), {"triggered": ["cascade", "northstar"]})


if __name__ == "__main__":
    unittest.main()
