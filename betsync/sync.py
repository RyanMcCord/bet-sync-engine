"""The sync engine: one worker per provider, so a slow or failing sportsbook never holds up another.

A sync cycle, for one provider:
  1. Fetch a page from the saved cursor. Transient failures are retried with exponential
     backoff and jitter; a 429 waits for Retry-After; a permanent error ends the cycle.
  2. Translate each record (a record that can't be translated is set aside; the rest go on).
  3. Apply the page and advance the cursor in one transaction.
  4. Repeat while the provider has more, up to a page limit per cycle.
  5. Re-fetch a few bets that are still missing fields they should have (e.g. a settled bet
     whose payout the provider left out), if the provider supports single-record fetches.

Partial success is kept: if page 3 fails, pages 1 and 2 are stored and the cursor points
after them. After a failed cycle the worker waits longer each time (up to a cap) before
trying again, so a provider that's down isn't hammered; the first success resets that.
"""
from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .domain import Outcome
from .providers.base import Provider, ProviderError, RateLimited, RecordError

log = logging.getLogger("betsync.sync")


@dataclass
class SyncPolicy:
    poll_interval: float = 5.0          # seconds between cycles when all is well
    attempts: int = 4                   # tries per request before the cycle gives up
    backoff_base: float = 0.5           # first retry waits up to this long
    backoff_cap: float = 8.0            # no single retry waits longer than this
    rate_limit_cap: float = 30.0        # never wait longer than this on a Retry-After
    failure_backoff_cap: float = 60.0   # longest gap between failed cycles
    max_pages_per_cycle: int = 20
    repairs_per_cycle: int = 10         # incomplete bets to re-fetch per cycle


@dataclass
class RunResult:
    started_at: datetime
    finished_at: datetime | None = None
    ok: bool = False
    pages: int = 0
    received: int = 0
    rejected: int = 0
    retries: int = 0
    repaired: int = 0
    error: str | None = None
    counts: dict = field(default_factory=lambda: {o: 0 for o in Outcome})


def utcnow():
    """The current time in UTC (the default clock; tests pass their own)."""
    return datetime.now(timezone.utc)


class SyncWorker:
    def __init__(self, provider: Provider, store, policy: SyncPolicy | None = None, *,
                 now=utcnow, rng: random.Random | None = None, stop: threading.Event | None = None):
        """Set up a worker for one provider. now, rng and stop can be replaced in tests to control
        time, jitter and sleeping.
        """
        self.provider, self.store = provider, store
        self.policy = policy or SyncPolicy()
        self.now = now
        self.rng = rng or random.Random()
        self.stop = stop or threading.Event()
        self.wake = threading.Event()        # set to run a cycle now (POST /sync)
        store.ensure_provider(provider.name)

    # --- one cycle --------------------------------------------------------------------

    def run_cycle(self) -> RunResult:
        """Run one sync cycle: fetch pages from the saved cursor, translate each record, and apply
        each page together with its new cursor. If that succeeded, re-fetch incomplete bets. Then
        record the run and the book's health. A failure keeps the pages already applied.
        """
        name = self.provider.name
        run = RunResult(started_at=self.now())
        cursor = self.store.state(name).cursor
        try:
            for _ in range(self.policy.max_pages_per_cycle):
                page = self._fetch(cursor, run)
                observations, rejected = [], []
                for raw in page.records:
                    try:
                        observations.append(self.provider.to_observation(raw))
                    except RecordError as e:
                        rejected.append((self.provider.record_id(raw), str(e), raw))
                        log.warning("%s: set aside a record: %s", name, e)
                counts = self.store.apply_page(name, observations, rejected, page.next_cursor, self.now())
                cursor = page.next_cursor
                run.pages += 1
                run.received += len(page.records)
                run.rejected += len(rejected)
                for outcome, n in counts.items():
                    run.counts[outcome] += n
                if not page.has_more:
                    break
            run.ok = True
        except ProviderError as e:
            run.error = f"{e.__class__.__name__}: {e}"
        if run.ok:
            self._repair(run)
        run.finished_at = self.now()
        failures = 0 if run.ok else self.store.state(name).consecutive_failures + 1
        self.store.record_cycle(name, ok=run.ok, at=run.finished_at, error=run.error,
                                next_attempt_at=run.finished_at + timedelta(seconds=self.next_delay(failures)))
        self.store.record_run(name, run)
        c = run.counts
        log.log(logging.INFO if run.ok else logging.WARNING,
                "%s: %s pages=%d received=%d inserted=%d updated=%d duplicate=%d stale=%d rejected=%d retries=%d repaired=%d%s",
                name, "ok" if run.ok else "FAILED", run.pages, run.received, c[Outcome.INSERTED], c[Outcome.UPDATED],
                c[Outcome.DUPLICATE], c[Outcome.STALE], run.rejected, run.retries, run.repaired,
                f" error={run.error}" if run.error else "")
        return run

    def _repair(self, run):
        """Ask the provider again for bets that are missing fields they should have. Best effort:
        anything that fails stays incomplete (and visible in the API) and is tried next cycle."""
        name = self.provider.name
        for external_id in self.store.incomplete(name, self.policy.repairs_per_cycle):
            try:
                raw = self.provider.fetch_record(external_id)
                if raw is None:
                    return   # this provider can't fetch single records
                obs = self.provider.to_observation(raw)
            except (ProviderError, RecordError) as e:
                log.info("%s: couldn't repair %s yet: %s", name, external_id, e)
                continue
            counts = self.store.apply_page(name, [obs], [], self.store.state(name).cursor, self.now())
            if counts[Outcome.UPDATED] and not obs.bet.unknown_fields():
                run.repaired += 1

    def _fetch(self, cursor, run):
        """Fetch one page, retrying transient errors with exponential backoff and full jitter, and
        waiting out rate limits. Permanent errors are raised at once; the last error is raised if
        every attempt fails.
        """
        p = self.policy
        for attempt in range(1, p.attempts + 1):
            try:
                return self.provider.fetch(cursor)
            except RateLimited as e:
                wait = min(e.retry_after, p.rate_limit_cap)
                err = e
            except ProviderError as e:
                if not e.retryable:
                    raise
                # exponential backoff with full jitter: spreads retries out instead of synchronising them
                wait = self.rng.uniform(0, min(p.backoff_cap, p.backoff_base * 2 ** (attempt - 1)))
                err = e
            if attempt == p.attempts:
                raise err
            run.retries += 1
            log.info("%s: %s; retry %d/%d in %.1fs", self.provider.name, err, attempt, p.attempts - 1, wait)
            if self.stop.wait(wait):
                raise err   # shutting down

    def next_delay(self, consecutive_failures: int) -> float:
        """Seconds until the next cycle: the poll interval, doubled per consecutive failure, capped."""
        if consecutive_failures <= 0:
            return self.policy.poll_interval
        return min(self.policy.failure_backoff_cap, self.policy.poll_interval * 2 ** consecutive_failures)

    # --- running forever --------------------------------------------------------------

    def run_forever(self):
        """Run cycles until stopped, sleeping until the next attempt is due or until woken by
        POST /sync. An unexpected error is logged and doesn't stop the worker.
        """
        log.info("%s: worker started", self.provider.name)
        while not self.stop.is_set():
            try:
                self.run_cycle()
            except Exception:   # a bug must not kill the worker; it's logged and the next cycle tries again
                log.exception("%s: unexpected error in sync cycle", self.provider.name)
            state = self.store.state(self.provider.name)
            due = state.next_attempt_at or self.now()
            wait = max(0.0, (due - self.now()).total_seconds())
            if self.wake.wait(wait):
                self.wake.clear()
        log.info("%s: worker stopped", self.provider.name)


class SyncEngine:
    """Starts and stops one worker thread per provider."""

    def __init__(self, providers, store, policy: SyncPolicy | None = None):
        """Create one worker per provider, all sharing the store and one stop signal."""
        self.stop = threading.Event()
        self.workers = {p.name: SyncWorker(p, store, policy, stop=self.stop) for p in providers}
        self.threads = []

    def start(self):
        """Start each worker on its own daemon thread."""
        for name, worker in self.workers.items():
            t = threading.Thread(target=worker.run_forever, name=f"sync-{name}", daemon=True)
            t.start()
            self.threads.append(t)

    def trigger(self, name=None):
        """Run a cycle now instead of waiting (for one provider, or all)."""
        for n, worker in self.workers.items():
            if name in (None, n):
                worker.wake.set()

    def shutdown(self, timeout=5):
        """Ask every worker to stop, wake any that are sleeping, and wait for their threads."""
        self.stop.set()
        for w in self.workers.values():
            w.wake.set()
        for t in self.threads:
            t.join(timeout)
