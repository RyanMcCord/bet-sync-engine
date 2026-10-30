"""The simulated truth both sportsbooks report on: games that start, play out and finish,
and a bettor's bets that get placed and later settle, cash out or void.

Everything is generated from a seed, and only versions whose time has come are visible, so
the data changes over time while the service runs and is exactly reproducible in tests.
The sportsbooks (northstar.py, cascade.py) render this truth in their own schemas.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..domain import TEAMS


# --- clocks -------------------------------------------------------------------------

class ManualClock:
    """For tests: time only moves when told to."""

    def __init__(self, start: datetime):
        """Start the clock at a fixed time."""
        self._now = start

    def now(self) -> datetime:
        """The current simulated time."""
        return self._now

    def advance(self, **kw):
        """Move time forward, e.g. advance(minutes=30)."""
        self._now += timedelta(**kw)


class ScaledClock:
    """For the running service: simulated time runs `speed` times faster than real time."""

    def __init__(self, start: datetime, speed: float):
        """Start simulated time at start, running speed times faster than the wall clock."""
        self.start, self.speed, self._wall = start, speed, time.monotonic()

    def now(self) -> datetime:
        """The current simulated time."""
        return self.start + timedelta(seconds=(time.monotonic() - self._wall) * self.speed)


# --- the world ----------------------------------------------------------------------

@dataclass(frozen=True)
class Game:
    gid: int
    league: str
    away: str           # canonical team code
    home: str
    starts_at: datetime
    ends_at: datetime
    away_score: int
    home_score: int
    postponed: bool = False


@dataclass(frozen=True)
class LegTruth:
    game: Game
    market: str          # moneyline | spread | total
    selection: str       # team code, "over" or "under"
    line: float | None
    price: int = -110    # American odds for this leg

    def result(self) -> str:
        """win | loss | push for a finished game."""
        g = self.game
        if self.market == "total":
            diff = (g.away_score + g.home_score) - self.line
            diff = diff if self.selection == "over" else -diff
        else:
            mine, theirs = (g.home_score, g.away_score) if self.selection == g.home else (g.away_score, g.home_score)
            diff = mine - theirs
            if self.market == "spread":
                diff += self.line
        return "win" if diff > 0 else "loss" if diff < 0 else "push"


@dataclass(frozen=True)
class BetVersion:
    """A bet as it stood from `at` onwards."""
    at: datetime
    status: str                    # open | won | lost | push | void | cashed_out
    payout_cents: int | None
    settled_at: datetime | None


@dataclass
class BetTruth:
    book: str
    number: int                    # the book renders its own ID from this
    placed_at: datetime
    stake_cents: int
    odds_american: int
    legs: list[LegTruth]
    versions: list[BetVersion] = field(default_factory=list)

    def version_at(self, now: datetime) -> BetVersion | None:
        """The latest version of this bet at the given time, or None if it hadn't been placed yet."""
        visible = [v for v in self.versions if v.at <= now]
        return visible[-1] if visible else None


def _american_to_decimal(odds: int) -> float:
    """Convert American odds to decimal, unrounded."""
    return 1 + (odds / 100 if odds > 0 else 100 / -odds)


class World:
    BOOKS = ("northstar", "cascade")

    def __init__(self, clock, seed: int = 7, games: int = 12, bets_per_book: int = 24):
        """Generate the games and each book's bets from the seed."""
        self.clock = clock
        self.rng = random.Random(seed)
        start = clock.now()
        self.games = [self._make_game(i, start) for i in range(games)]
        self.bets = {book: [self._make_bet(book, n, start) for n in range(bets_per_book)] for book in self.BOOKS}
        self._lock = threading.Lock()

    def _make_game(self, gid, start):
        """Make one game: a league, two teams, a start time around now and a final score."""
        rng = self.rng
        league = rng.choice(["NBA", "NBA", "NFL"])
        away, home = rng.sample(sorted(TEAMS[league]), 2)
        # Some games are already over when the service starts, some live, most still to come.
        starts = start + timedelta(minutes=rng.randrange(-420, 540, 15))
        length = timedelta(minutes=150 if league == "NBA" else 195)
        if league == "NBA":
            away_score, home_score = rng.randint(95, 128), rng.randint(95, 128)
        else:
            away_score, home_score = rng.choice([10, 13, 17, 20, 23, 24, 27, 31]), rng.choice([10, 14, 17, 20, 21, 24, 28, 30])
        if away_score == home_score:
            home_score += 3
        return Game(gid, league, away, home, starts, starts + length, away_score, home_score,
                    postponed=rng.random() < 0.04)

    def _make_leg(self, game):
        """Make one leg on a game: a moneyline, spread or total pick."""
        rng = self.rng
        market = rng.choice(["moneyline", "spread", "total"])
        if market == "moneyline":
            return LegTruth(game, market, rng.choice([game.away, game.home]), None)
        if market == "spread":
            line = rng.choice([-7.5, -6.5, -4.5, -3.0, -2.5, -1.5, 1.5, 2.5, 3.0, 4.5, 6.5])
            return LegTruth(game, market, rng.choice([game.away, game.home]), line)
        total = game.away_score + game.home_score + rng.choice([-9.5, -4.5, -3.0, -0.5, 0.5, 3.0, 4.5, 9.5])
        return LegTruth(game, market, rng.choice(["over", "under"]), float(total))

    def _make_bet(self, book, n, start):
        """Make one bet for a book: one to three legs, a stake and price, and the versions it goes
        through as its games finish (open, then settled).
        """
        rng = self.rng
        games = rng.sample(self.games, 1 if rng.random() < 0.75 else rng.choice([2, 3]))
        legs = []
        for g in games:
            leg = self._make_leg(g)
            price = (rng.choice([-150, -125, -115, -110, -105, 100, 110, 130, 165, 210]) if leg.market == "moneyline"
                     else rng.choice([-115, -110, -105]))
            legs.append(LegTruth(leg.game, leg.market, leg.selection, leg.line, price))
        decimal = 1.0
        for leg in legs:
            decimal *= _american_to_decimal(leg.price)
        odds = round((decimal - 1) * 100) if decimal >= 2 else round(-100 / (decimal - 1))
        first_start = min(l.game.starts_at for l in legs)
        placed = first_start - timedelta(minutes=rng.randrange(10, 360))
        stake = rng.choice([500, 1000, 1500, 2000, 2500, 5000, 10000])
        bet = BetTruth(book, n, placed, stake, odds, legs)
        bet.versions = self._lifecycle(bet)
        return bet

    def _lifecycle(self, bet):
        """open when placed, then exactly one way out: void, cash out, or settle on the results."""
        rng = self.rng
        versions = [BetVersion(bet.placed_at, "open", None, None)]
        delay = timedelta(minutes=rng.randrange(2, 12) if bet.book == "northstar" else rng.randrange(5, 35))
        postponed = [l for l in bet.legs if l.game.postponed]
        if postponed:
            at = postponed[0].game.starts_at + timedelta(minutes=30)
            versions.append(BetVersion(at, "void", bet.stake_cents, at))
            return versions
        if len(bet.legs) == 1 and rng.random() < 0.1:   # cashed out mid-game
            game = bet.legs[0].game
            at = game.starts_at + (game.ends_at - game.starts_at) / 2
            versions.append(BetVersion(at, "cashed_out", int(bet.stake_cents * rng.uniform(0.3, 1.4)), at))
            return versions
        results = [l.result() for l in bet.legs]
        at = max(l.game.ends_at for l in bet.legs) + delay
        if "loss" in results:
            versions.append(BetVersion(at, "lost", 0, at))
        elif all(r == "push" for r in results):
            versions.append(BetVersion(at, "push", bet.stake_cents, at))
        else:
            # pushed legs drop out of a parlay; the winning legs are priced on their own
            factor = 1.0
            for leg, r in zip(bet.legs, results):
                if r == "win":
                    factor *= _american_to_decimal(leg.price)
            versions.append(BetVersion(at, "won", round(bet.stake_cents * factor), at))
        return versions

    # --- what the books can see right now ---------------------------------------------

    def now(self):
        """The current simulated time."""
        return self.clock.now()

    def current_bets(self, book):
        """[(bet, version)] for every bet the book currently knows about, as it stands now."""
        now = self.now()
        out = []
        for bet in self.bets[book]:
            v = bet.version_at(now)
            if v:
                out.append((bet, v))
        return out

    def change_log(self, book):
        """Every version that has happened so far, oldest first: [(bet, version)]."""
        now = self.now()
        log = [(bet, v) for bet in self.bets[book] for v in bet.versions if v.at <= now]
        return sorted(log, key=lambda bv: (bv[1].at, bv[0].number))
