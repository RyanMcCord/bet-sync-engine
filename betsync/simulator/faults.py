"""What can go wrong at a sportsbook, per request. Seeded, so a scenario misbehaves the same way every run."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import asdict, dataclass, replace


@dataclass
class FaultProfile:
    error_rate: float = 0.0          # chance a request fails with a 500/503
    timeout_rate: float = 0.0        # chance a request hangs longer than any sensible client timeout
    latency_ms: tuple[int, int] = (0, 0)   # extra delay on every request, min..max
    rate_limit_per_sec: float = 0.0  # 0 = no limit; above it, 429 with Retry-After
    duplicate_rate: float = 0.0      # chance a page repeats records the client has already had
    stale_rate: float = 0.0          # chance a page includes an out-of-date version of a bet (lagging replica)
    missing_field_rate: float = 0.0  # chance a record leaves out an optional field
    outage: bool = False             # everything fails with 503 until turned off

    def to_dict(self):
        d = asdict(self)
        d["latency_ms"] = list(self.latency_ms)
        return d


SCENARIOS = {
    # Well-behaved books: useful to see the happy path.
    "calm": {"northstar": FaultProfile(), "cascade": FaultProfile()},
    # The default: every kind of trouble shows up, but data still flows.
    "realistic": {
        "northstar": FaultProfile(error_rate=0.08, timeout_rate=0.03, latency_ms=(20, 250), rate_limit_per_sec=4,
                                  duplicate_rate=0.25, stale_rate=0.2),
        "cascade": FaultProfile(error_rate=0.12, timeout_rate=0.05, latency_ms=(50, 400), rate_limit_per_sec=3,
                                duplicate_rate=0.3, missing_field_rate=0.25),
    },
    # Heavy weather: most requests struggle; freshness should visibly degrade.
    "chaos": {
        "northstar": FaultProfile(error_rate=0.35, timeout_rate=0.15, latency_ms=(100, 900), rate_limit_per_sec=2,
                                  duplicate_rate=0.5, stale_rate=0.4),
        "cascade": FaultProfile(error_rate=0.45, timeout_rate=0.2, latency_ms=(200, 1200), rate_limit_per_sec=1,
                                duplicate_rate=0.5, missing_field_rate=0.5),
    },
}


@dataclass(frozen=True)
class Response:
    """A sportsbook's answer, before it goes over HTTP. `delay` is how long the book takes to send it."""
    status: int
    body: dict
    headers: dict
    delay: float = 0.0


HANG_SECONDS = 30.0   # how long a "timed out" request hangs; far beyond the client's timeout


class Faults:
    """The live fault settings for one book, adjustable through its control endpoint."""

    def __init__(self, profile: FaultProfile, seed: int):
        self.profile = profile
        self.rng = random.Random(seed)
        self._lock = threading.Lock()
        self._window_start, self._count = time.monotonic(), 0

    def update(self, **changes):
        with self._lock:
            if "latency_ms" in changes:
                changes["latency_ms"] = tuple(changes["latency_ms"])
            self.profile = replace(self.profile, **changes)

    def chance(self, rate):
        with self._lock:
            return self.rng.random() < rate

    def before(self) -> Response | None:
        """A failure to return instead of serving the request, if any."""
        p = self.profile
        if p.outage:
            return Response(503, {"error": "service unavailable"}, {})
        if p.rate_limit_per_sec and not self._allow(p.rate_limit_per_sec):
            return Response(429, {"error": "rate limited"}, {"Retry-After": "1"})
        if self.chance(p.timeout_rate):
            return Response(504, {"error": "gateway timeout"}, {}, delay=HANG_SECONDS)
        if self.chance(p.error_rate):
            return Response(self.rng.choice([500, 502, 503]), {"error": "upstream error"}, {})
        return None

    def latency(self) -> float:
        lo, hi = self.profile.latency_ms
        with self._lock:
            return self.rng.uniform(lo, hi) / 1000 if hi else 0.0

    def _allow(self, per_sec):
        with self._lock:
            now = time.monotonic()
            if now - self._window_start >= 1:
                self._window_start, self._count = now, 0
            self._count += 1
            return self._count <= per_sec
