"""The sync engine against the simulated sportsbooks (in-process, simulated time, no real waits)."""
import unittest
from datetime import timedelta

from betsync.domain import Outcome
from betsync.providers.base import PermanentError, RateLimited, TransientError
from betsync.service import ReadService
from betsync.sync import SyncPolicy

from .support import Rig


def truth(rig):
    """What each book really says right now, keyed by canonical bet id."""
    out = {}
    for b, v in rig.sim.world.current_bets("northstar"):
        out[f"northstar:NS-{10000 + b.number}"] = (v.status, b.stake_cents, v.payout_cents, b.odds_american)
    for b, v in rig.sim.world.current_bets("cascade"):
        out[f"cascade:{77000 + b.number}"] = (v.status, b.stake_cents, v.payout_cents, b.odds_american)
    return out


def stored(rig):
    """What the store holds for each bet: status, stake, payout and odds."""
    return {r["id"]: (r["status"], r["stake_cents"], r["payout_cents"], r["odds_american"])
            for r in rig.store.bets(limit=1000)}


def run_day(rig, cycles=34, step=timedelta(minutes=30)):
    """Sync, then advance the clock, over and over: about a simulated day by default."""
    for _ in range(cycles):
        rig.sync()
        rig.clock.advance(seconds=step.total_seconds())
    rig.sync()


class ConvergenceTests(unittest.TestCase):
    """However badly the books behave, the canonical view ends up matching what they really say."""

    def check(self, scenario, seed=7):
        """Sync a simulated day under a scenario, then check the store matches the simulated truth
        for every bet. Returns the rig for further checks.
        """
        rig = Rig(scenario, seed=seed)
        self.addCleanup(rig.close)
        run_day(rig)
        want, got = truth(rig), stored(rig)
        self.assertEqual(set(got), set(want))
        for bet_id, (status, stake, payout, odds) in want.items():
            g = got[bet_id]
            self.assertEqual(g[:3], (status, stake, payout), bet_id)
            # odds: Cascade quotes decimal odds, so allow rounding after converting back to American
            self.assertLessEqual(abs(g[3] - odds), 2, bet_id)
        return rig

    def test_calm(self):
        """With well-behaved books, the store ends up matching the truth."""
        self.check("calm")

    def test_realistic(self):
        """With realistic faults it still matches, and stale and duplicate records were actually seen."""
        rig = self.check("realistic")
        totals = {p: rig.store.outcome_totals(p) for p in ("northstar", "cascade")}
        self.assertGreater(totals["northstar"]["stale"], 0)       # out-of-order versions were seen and ignored
        self.assertGreater(totals["cascade"]["duplicate"], 0)     # redeliveries were seen and ignored

    def test_chaos_across_seeds(self):
        """Under heavy faults it still matches, across more than one seed."""
        for seed in (3, 11):
            with self.subTest(seed=seed):
                self.check("chaos", seed)


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        """A fresh rig with well-behaved books."""
        self.rig = Rig("calm")
        self.addCleanup(self.rig.close)

    def test_syncing_again_with_nothing_new_changes_nothing(self):
        """Syncing again with nothing new inserts or updates nothing."""
        run_day(self.rig, cycles=4)
        before = {r["id"]: (r["version"], r["content_hash"]) for r in self.rig.store.bets(limit=1000)}
        for _ in range(3):
            runs = self.rig.sync()
            for run in runs.values():
                self.assertEqual(run.counts[Outcome.INSERTED] + run.counts[Outcome.UPDATED], 0)
        after = {r["id"]: (r["version"], r["content_hash"]) for r in self.rig.store.bets(limit=1000)}
        self.assertEqual(before, after)

    def test_replaying_a_whole_feed_from_the_start_changes_nothing(self):
        """Forgetting every cursor and replaying both feeds from the start changes nothing."""
        run_day(self.rig)
        before = stored(self.rig)
        with self.rig.store.transaction() as c:   # forget every cursor
            c.execute("UPDATE provider_state SET cursor = NULL")
        runs = self.rig.sync()
        self.assertEqual(stored(self.rig), before)
        for run in runs.values():
            self.assertEqual(run.counts[Outcome.INSERTED] + run.counts[Outcome.UPDATED], 0)
            self.assertGreater(run.counts[Outcome.DUPLICATE] + run.counts[Outcome.STALE], 0)


class OrderingTests(unittest.TestCase):
    def test_a_late_old_version_never_reopens_a_settled_bet(self):
        """An old open version delivered after settlement is recorded as stale and doesn't reopen the bet."""
        rig = Rig("calm")
        self.addCleanup(rig.close)
        run_day(rig)
        settled = [r for r in rig.store.bets(sportsbook="northstar", limit=1000) if r["status"] != "open"]
        target = settled[0]
        bet, _ = next((b, v) for b, v in rig.sim.world.current_bets("northstar")
                      if f"northstar:NS-{10000 + b.number}" == target["id"])
        from betsync.simulator.northstar import render
        old = render(bet, bet.versions[0])              # the "open" version, delivered late
        obs = rig.providers["northstar"].to_observation(old)
        counts = rig.store.apply_page("northstar", [obs], [], rig.store.state("northstar").cursor, rig.clock.now())
        self.assertEqual(counts[Outcome.STALE], 1)
        self.assertEqual(rig.store.bet(target["id"])["status"], target["status"])
        self.assertEqual(rig.store.history(target["id"])[0]["outcome"], "stale")   # but it's on record


class MissingFieldTests(unittest.TestCase):
    def test_odds_left_out_of_an_update_are_kept(self):
        """When a newer update leaves out the odds, the stored odds are kept."""
        rig = Rig("calm")
        self.addCleanup(rig.close)
        rig.sync("cascade")
        row = next(r for r in rig.store.bets(sportsbook="cascade", limit=1000))
        bet, version = next((b, v) for b, v in rig.sim.world.current_bets("cascade") if f"cascade:{77000 + b.number}" == row["id"])
        from betsync.simulator.cascade import render
        from betsync.domain import TEAMS
        newer = render(bet, version, TEAMS)
        newer["updated"] += 60
        newer["stake"] += 100           # something really changed...
        del newer["decimal_odds"]       # ...and the odds were left out
        obs = rig.providers["cascade"].to_observation(newer)
        rig.store.apply_page("cascade", [obs], [], rig.store.state("cascade").cursor, rig.clock.now())
        after = rig.store.bet(row["id"])
        self.assertEqual(after["odds_american"], row["odds_american"])
        self.assertEqual(after["stake_cents"], row["stake_cents"] + 100)
        self.assertIn("odds_american", after["carried_over"])


class RepairTests(unittest.TestCase):
    def test_a_field_we_never_received_is_flagged_then_fetched_again(self):
        """A bet missing a field we never had is flagged, then re-fetched and filled in on the next cycle."""
        rig = Rig("calm")
        self.addCleanup(rig.close)
        run_day(rig)
        row = next(r for r in rig.store.bets(sportsbook="cascade", limit=1000) if r["status"] == "lost")
        with rig.store.transaction() as c:   # as if the settled ticket had arrived without its payout
            c.execute("UPDATE bets SET payout_cents = NULL, incomplete = '[\"payout_cents\"]' WHERE id = ?", (row["id"],))
        svc = ReadService(rig.store, ["northstar", "cascade"], now=lambda: rig.clock.now())
        self.assertEqual(svc.bet(row["id"])["freshness"]["unknown_fields"], ["payout_cents"])
        run = rig.sync("cascade")["cascade"]
        self.assertEqual(run.repaired, 1)
        after = rig.store.bet(row["id"])
        self.assertEqual((after["payout_cents"], after["incomplete"]), (0, "[]"))

    def test_providers_without_single_record_fetch_leave_the_gap_visible(self):
        """A book that can't fetch one record leaves the gap flagged instead of guessing."""
        rig = Rig("calm")
        self.addCleanup(rig.close)
        rig.sync("northstar")
        row = rig.store.bets(sportsbook="northstar", limit=1)[0]
        with rig.store.transaction() as c:
            c.execute("UPDATE bets SET incomplete = '[\"odds_american\"]' WHERE id = ?", (row["id"],))
        self.assertEqual(rig.sync("northstar")["northstar"].repaired, 0)
        self.assertEqual(rig.store.bet(row["id"])["incomplete"], '["odds_american"]')


class FailureTests(unittest.TestCase):
    def setUp(self):
        """A rig with 3 attempts per request, recording every wait instead of sleeping."""
        self.rig = Rig("calm", policy=SyncPolicy(attempts=3, poll_interval=5, failure_backoff_cap=60))
        self.addCleanup(self.rig.close)
        self.waits = []
        for w in self.rig.workers.values():
            w.stop.wait = lambda seconds, waits=self.waits: waits.append(seconds) or False

    def flaky(self, name, errors):
        """Make a provider raise these errors, in order, before working normally."""
        provider = self.rig.providers[name]
        real, queue = provider.fetch, list(errors)

        def fetch(cursor):
            """Raise the next queued error, or fetch for real once the queue is empty."""
            if queue:
                raise queue.pop(0)
            return real(cursor)
        provider.fetch = fetch

    def test_transient_errors_are_retried_with_growing_backoff(self):
        """Transient errors are retried, with jittered waits that grow each time."""
        self.flaky("cascade", [TransientError("503"), TransientError("timeout")])
        run = self.rig.sync("cascade")["cascade"]
        self.assertTrue(run.ok)
        self.assertEqual(run.retries, 2)
        self.assertLessEqual(self.waits[0], 0.5)      # full jitter: up to base, then base*2, capped
        self.assertLessEqual(self.waits[1], 1.0)

    def test_rate_limits_wait_for_retry_after(self):
        """A 429 waits exactly as long as Retry-After says."""
        self.flaky("northstar", [RateLimited("429", retry_after=7)])
        self.assertTrue(self.rig.sync("northstar")["northstar"].ok)
        self.assertEqual(self.waits, [7])

    def test_permanent_errors_are_not_retried(self):
        """A permanent error fails the cycle without retrying."""
        self.flaky("northstar", [PermanentError("400")])
        run = self.rig.sync("northstar")["northstar"]
        self.assertFalse(run.ok)
        self.assertEqual(run.retries, 0)

    def test_partial_success_is_kept_and_resumes_where_it_stopped(self):
        """Pages applied before a failure are kept, and the next cycle resumes at the failed page."""
        provider = self.rig.providers["cascade"]
        real = provider.fetch
        calls = []

        def fetch(cursor):
            """Serve small pages, and fail on the second call."""
            calls.append(cursor)
            if len(calls) == 2:
                raise PermanentError("page 2 broke")
            page = real(cursor)
            return page.__class__(page.records[:5], str(int(cursor or 0) + 5), True)   # small pages
        provider.fetch = fetch
        run = self.rig.sync("cascade")["cascade"]
        self.assertFalse(run.ok)
        self.assertEqual(run.counts[Outcome.INSERTED], 5)         # page 1 is kept
        self.assertEqual(self.rig.store.state("cascade").cursor, "5")
        provider.fetch = real
        self.rig.sync("cascade")
        self.assertEqual(calls[0], None)
        self.assertEqual(len(stored(self.rig)), len([k for k in truth(self.rig) if k.startswith("cascade")]))

    def test_a_record_that_cant_be_read_is_set_aside_not_fatal(self):
        """One unreadable record is set aside as rejected, and the rest of the page still syncs."""
        provider = self.rig.providers["northstar"]
        real = provider.fetch

        def fetch(cursor):
            """Put one unreadable record at the front of each real page."""
            page = real(cursor)
            return page.__class__([{"betId": "NS-BROKEN", "status": "???"}] + page.records, page.next_cursor, page.has_more)
        provider.fetch = fetch
        run = self.rig.sync("northstar")["northstar"]
        self.assertTrue(run.ok)
        self.assertEqual(run.rejected, 1)
        self.assertEqual(self.rig.store.outcome_totals("northstar")["rejected"], 1)
        self.assertGreater(run.counts[Outcome.INSERTED], 0)

    def test_one_book_down_doesnt_affect_the_other(self):
        """An outage at Cascade doesn't stop Northstar from syncing."""
        self.rig.sim.cascade.faults.update(outage=True)
        runs = self.rig.sync()
        self.assertFalse(runs["cascade"].ok)
        self.assertTrue(runs["northstar"].ok)
        self.assertGreater(len(self.rig.store.bets(sportsbook="northstar")), 0)

    def test_failed_cycles_back_off_and_success_resets(self):
        """Each failed cycle doubles the wait before the next, up to the cap; one success resets it."""
        w = self.rig.workers["cascade"]
        self.assertEqual([w.next_delay(n) for n in range(6)], [5, 10, 20, 40, 60, 60])
        self.rig.sim.cascade.faults.update(outage=True)
        for _ in range(3):
            self.rig.sync("cascade")
        state = self.rig.store.state("cascade")
        self.assertEqual(state.consecutive_failures, 3)
        self.assertEqual((state.next_attempt_at - state.last_attempt_at).total_seconds(), 40)
        self.rig.sim.cascade.faults.update(outage=False)
        self.rig.sync("cascade")
        self.assertEqual(self.rig.store.state("cascade").consecutive_failures, 0)


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        """A fresh rig and a read service on its clock."""
        self.rig = Rig("calm")
        self.addCleanup(self.rig.close)
        self.svc = ReadService(self.rig.store, ["northstar", "cascade"], now=lambda: self.rig.clock.now())

    def status(self, name):
        """A book's freshness status right now."""
        return self.svc.provider_health(name)["status"]

    def test_lifecycle_of_a_failing_book(self):
        """A book goes never_synced, ok, degraded, then stale as an outage goes on, and back to ok
        once it recovers, while the other book stays ok.
        """
        self.assertEqual(self.status("cascade"), "never_synced")
        self.rig.sync()
        self.assertEqual(self.status("cascade"), "ok")
        self.rig.sim.cascade.faults.update(outage=True)
        self.rig.clock.advance(seconds=10)
        self.rig.sync()
        self.assertEqual(self.status("cascade"), "degraded")   # failing, but our data is still recent
        self.assertEqual(self.status("northstar"), "ok")
        self.rig.clock.advance(seconds=60)
        self.rig.sync()
        self.assertEqual(self.status("cascade"), "stale")
        self.assertEqual(self.svc.status()["status"], "stale")       # overall = worst book

        bets = self.svc.bets(sportsbook="cascade")["bets"]
        self.assertTrue(all(b["freshness"]["stale"] for b in bets))   # still served, marked stale
        self.assertFalse(any(b["freshness"]["stale"] for b in self.svc.bets(sportsbook="northstar")["bets"]))

        self.rig.sim.cascade.faults.update(outage=False)
        self.rig.sync()
        self.assertEqual(self.status("cascade"), "ok")

    def test_bet_detail_shows_its_history(self):
        """A settled bet's detail shows its history, from inserted to updated, and its version."""
        run_day(self.rig, cycles=34)
        settled = next(r for r in self.rig.store.bets(limit=1000) if r["status"] in ("won", "lost"))
        detail = self.svc.bet(settled["id"])
        outcomes = [h["outcome"] for h in detail["history"]]
        self.assertEqual(outcomes[-1], "inserted")      # oldest last
        self.assertIn("updated", outcomes)
        self.assertEqual(detail["freshness"]["version"], 2)


if __name__ == "__main__":
    unittest.main()
