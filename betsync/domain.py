"""The canonical model: what a bet is in *our* system, regardless of which sportsbook reported it.

This module is the boundary where provider data stops being provider data. Adapters
(betsync/providers) translate each sportsbook's schema into these types; nothing past
that point knows a provider's field names, status codes, odds format or ID scheme.

No I/O here. Everything is plain data and pure functions, so it's tested on its own.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo


class BetStatus(str, Enum):
    OPEN = "open"
    WON = "won"
    LOST = "lost"
    PUSH = "push"
    VOID = "void"
    CASHED_OUT = "cashed_out"

    @property
    def settled(self) -> bool:
        return self is not BetStatus.OPEN


class Market(str, Enum):
    MONEYLINE = "moneyline"
    SPREAD = "spread"
    TOTAL = "total"


# --- events: the same real-world game, described differently by each book ------------

# Canonical team codes. Books use codes ("LAL") or full names ("Los Angeles Lakers");
# both resolve here. An unknown team still gets a stable slug and the event is flagged.
TEAMS = {
    "NBA": {
        "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets",
        "GSW": "Golden State Warriors", "LAL": "Los Angeles Lakers", "MIA": "Miami Heat", "MIL": "Milwaukee Bucks",
        "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder", "PHX": "Phoenix Suns", "LAC": "LA Clippers",
    },
    "NFL": {
        "BUF": "Buffalo Bills", "DAL": "Dallas Cowboys", "DET": "Detroit Lions", "GB": "Green Bay Packers",
        "KC": "Kansas City Chiefs", "LV": "Las Vegas Raiders", "PHI": "Philadelphia Eagles", "SF": "San Francisco 49ers",
    },
}
_EASTERN = ZoneInfo("America/New_York")


def team_code(league: str, name_or_code: str) -> tuple[str, bool]:
    """Resolve a team to its canonical code. Returns (code, known)."""
    teams = TEAMS.get(league, {})
    value = (name_or_code or "").strip()
    if value.upper() in teams:
        return value.upper(), True
    for code, name in teams.items():
        if name.lower() == value.lower():
            return code, True
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown", False


def event_id(league: str, away: str, home: str, starts_at: datetime) -> str:
    """One ID per real game, whichever book describes it: league, local (Eastern) game date, teams.
    The date rather than the exact start time, because books disagree by minutes."""
    day = starts_at.astimezone(_EASTERN).date().isoformat()
    return f"{league.lower()}-{day}-{away.lower()}-at-{home.lower()}"


@dataclass(frozen=True)
class Event:
    id: str
    league: str
    away: str            # canonical team code
    home: str
    starts_at: datetime
    teams_known: bool = True

    @property
    def name(self) -> str:
        return f"{self.away} @ {self.home}"

    @classmethod
    def from_teams(cls, league, away_name, home_name, starts_at):
        away, away_known = team_code(league, away_name)
        home, home_known = team_code(league, home_name)
        return cls(event_id(league, away, home, starts_at), league, away, home, starts_at, away_known and home_known)


# --- bets ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Leg:
    event_id: str
    market: Market
    selection: str           # team code, "over" or "under"
    line: float | None = None


@dataclass(frozen=True)
class Bet:
    """A bet as we own it. Money is in integer cents; odds are American."""
    sportsbook: str
    external_id: str
    status: BetStatus
    placed_at: datetime
    stake_cents: int
    legs: tuple[Leg, ...]
    odds_american: int | None = None      # None = the book didn't say (and we never knew)
    payout_cents: int | None = None       # what came back when settled (0 for a loss)
    settled_at: datetime | None = None

    @property
    def id(self) -> str:
        return bet_id(self.sportsbook, self.external_id)

    @property
    def kind(self) -> str:
        return "parlay" if len(self.legs) > 1 else "single"

    def unknown_fields(self) -> tuple[str, ...]:
        """Fields this bet should have but we don't know yet: the odds always, and the payout
        and settlement time once it's settled."""
        needed = ["odds_american"] + (["payout_cents", "settled_at"] if self.status.settled else [])
        return tuple(f for f in needed if getattr(self, f) is None)

    def content_hash(self) -> str:
        """Identity of the bet's *content*, used to spot repeats of the same information."""
        data = asdict(self)
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def bet_id(sportsbook: str, external_id: str) -> str:
    return f"{sportsbook}:{external_id}"


# Fields a provider may leave out of an update without that meaning "erase it".
OPTIONAL_FIELDS = ("odds_american", "payout_cents", "settled_at")


@dataclass(frozen=True)
class Observation:
    """One provider report of one bet, already translated into canonical terms."""
    bet: Bet
    source_updated_at: datetime            # the provider's own "last modified" for this bet
    events: tuple[Event, ...] = ()         # the games its legs refer to
    event_aliases: tuple[tuple[str, str], ...] = ()   # (provider's own event id, canonical event id)
    missing_fields: tuple[str, ...] = ()   # optional fields the provider left out this time
    raw: dict = field(default_factory=dict, compare=False)


# --- the conflict rule --------------------------------------------------------------

class Outcome(str, Enum):
    INSERTED = "inserted"      # first time we've seen this bet
    UPDATED = "updated"        # newer information replaced the current state
    DUPLICATE = "duplicate"    # same information we already have: no change
    STALE = "stale"            # older than what we have: recorded, not applied


@dataclass(frozen=True)
class Current:
    """What we currently hold for a bet."""
    bet: Bet
    source_updated_at: datetime


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    bet: Bet | None                  # the bet to store (None when nothing changes)
    carried_over: tuple[str, ...] = ()   # optional fields kept from the previous version


def decide(current: Current | None, incoming: Observation) -> Decision:
    """Newest provider timestamp wins.

    - Nothing stored yet: insert.
    - Older than what we have: stale. A late or replayed update never overwrites newer state.
    - Same content as what we hold (at the same or a newer timestamp): duplicate. Nothing
      changes, though the store still notes when the provider last confirmed it.
    - Different content at a newer timestamp, or at the same timestamp (we treat that as a
      correction): update. Optional fields the provider left out this time are carried
      over from the version we hold, rather than erased.
    """
    if current is None:
        return Decision(Outcome.INSERTED, incoming.bet)
    if incoming.source_updated_at < current.source_updated_at:
        return Decision(Outcome.STALE, None)

    merged, carried = incoming.bet, []
    for name in incoming.missing_fields:
        kept = getattr(current.bet, name)
        if kept is not None and name in OPTIONAL_FIELDS:
            merged = replace(merged, **{name: kept})
            carried.append(name)

    if merged.content_hash() == current.bet.content_hash():
        return Decision(Outcome.DUPLICATE, None)
    return Decision(Outcome.UPDATED, merged, tuple(carried))


# --- odds helpers (books quote odds differently) --------------------------------------

def decimal_to_american(decimal_odds: float) -> int:
    if decimal_odds <= 1:
        raise ValueError(f"decimal odds must be above 1, got {decimal_odds}")
    if decimal_odds >= 2:
        return round((decimal_odds - 1) * 100)
    return round(-100 / (decimal_odds - 1))


def parse_american(value: str | int) -> int:
    text = str(value).strip().upper()
    if text in ("EVEN", "EV"):
        return 100
    odds = int(text.replace("+", ""))
    if -100 < odds < 100:
        raise ValueError(f"not American odds: {value!r}")
    return odds
