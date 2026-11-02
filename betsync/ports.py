"""Ports: the interfaces between the application and the outside world.

The application (betsync/application/) depends only on these and on the domain. Adapters
(betsync/adapters/) implement them. Swapping SQLite for Postgres, or adding a third
sportsbook, means writing a new adapter; the use cases don't change.

    Provider        a sportsbook: fetch pages of raw records and translate them (driven)
    SyncStore       what the sync use case needs from storage, including one transaction
                    per page through PageWriter (driven)
    ReadStore       what the read use case needs from storage (driven)

Also here: the errors a Provider may raise, and the small records the ports exchange.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .domain import Bet, Current, Event, Observation, Outcome

# --- provider port --------------------------------------------------------------------


class ProviderError(Exception):
    """Fetching failed. `retryable` tells the sync engine whether trying again could help."""

    retryable = True


class TransientError(ProviderError):
    """5xx, timeouts, dropped connections: likely to work if we try again later."""


class RateLimited(ProviderError):
    """429: the provider asked us to slow down, and said for how long."""

    def __init__(self, message, retry_after: float):
        """retry_after is how many seconds the provider asked us to wait."""
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(ProviderError):
    """4xx (other than 429) or a response we don't understand: retrying won't help."""

    retryable = False


class RecordError(Exception):
    """One record can't be translated. It's set aside; the rest of the page still syncs."""


@dataclass(frozen=True)
class Page:
    """One page of raw records from a provider, and where to resume after it."""

    records: list[dict]
    next_cursor: str | None    # where to resume once this page is safely stored
    has_more: bool             # more pages available right now


class Provider(ABC):
    """The contract every sportsbook adapter implements.

    An adapter does two things (plus one optional one) and nothing else:
      fetch(cursor)        get the next page of raw records from the provider
      to_observation(raw)  translate one raw record into the canonical model
      fetch_record(id)     optional: get one record again, to fill in missing fields

    Retries, backoff, deduplication, conflict resolution, storage and health all live in the
    application, so adding or changing a provider never means touching them.
    """

    name: str

    @abstractmethod
    def fetch(self, cursor: str | None) -> Page:
        """Raises TransientError, RateLimited or PermanentError."""

    @abstractmethod
    def to_observation(self, raw: dict) -> Observation:
        """Raises RecordError if the record can't be translated."""

    def fetch_record(self, external_id: str) -> dict | None:
        """Fetch one record again, to fill in fields that were missing. Optional: providers
        without a single-record endpoint return None, and the gap stays visible in the API.
        """
        return None

    def record_id(self, raw: dict) -> str:
        """The provider's ID for a raw record, for logs and the rejected-records list."""
        return "?"


# --- records exchanged with storage -----------------------------------------------------


@dataclass
class ProviderState:
    """A book's sync position and health, as storage holds it."""

    provider: str
    cursor: str | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0
    next_attempt_at: datetime | None = None


@dataclass
class RunResult:
    """What one sync cycle did, for logs, /status and the sync_runs history."""

    started_at: datetime
    finished_at: datetime | None = None
    ok: bool = False
    pages: int = 0
    received: int = 0
    rejected: int = 0
    retries: int = 0
    repaired: int = 0
    error: str | None = None
    counts: dict = field(default_factory=lambda: dict.fromkeys(Outcome, 0))


# --- storage ports ---------------------------------------------------------------------


class PageWriter(Protocol):
    """Writes for one page, inside one transaction: everything commits together or not at all."""

    def current(self, bet_id: str) -> Current | None:
        """The stored version of a bet and its provider timestamp, or None if we've never seen it."""

    def save_events(self, provider: str, events: tuple[Event, ...], aliases: tuple[tuple[str, str], ...]) -> None:
        """Save canonical games, and the provider's own IDs for them, if they're new."""

    def insert_bet(self, bet: Bet, source_updated_at: datetime, now: datetime) -> None:
        """Store a bet seen for the first time."""

    def update_bet(self, bet: Bet, source_updated_at: datetime, carried_over: tuple[str, ...],
                   now: datetime) -> None:
        """Replace a stored bet with a newer version, noting any fields carried over."""

    def confirm_bet(self, bet_id: str, source_updated_at: datetime, now: datetime) -> None:
        """Note that the provider reported a bet again with nothing changed."""

    def record_observation(self, provider: str, observation: Observation, outcome: Outcome,
                           now: datetime) -> None:
        """Append a received record and what was done with it to the bet's history."""

    def record_rejected(self, provider: str, record_id: str, reason: str, raw: Any, now: datetime) -> None:
        """Set aside a record that couldn't be translated, for inspection."""

    def set_cursor(self, provider: str, cursor: str | None) -> None:
        """Move the provider's cursor (committed with the page it follows)."""


class SyncStore(Protocol):
    """What the sync use case needs from storage."""

    def page(self) -> AbstractContextManager[PageWriter]:
        """Open one write transaction for a page."""

    def state(self, provider: str) -> ProviderState:
        """A book's cursor and health, or an empty state if it has never synced."""

    def ensure_provider(self, provider: str) -> None:
        """Create a book's state if it doesn't exist yet."""

    def incomplete(self, sportsbook: str, limit: int = 10) -> list[str]:
        """External IDs of bets with fields we still don't know."""

    def record_cycle(self, provider: str, *, ok: bool, at: datetime, error: str | None,
                     next_attempt_at: datetime) -> None:
        """Record how a cycle went, and when the next attempt is due."""

    def record_run(self, provider: str, run: RunResult) -> None:
        """Save one cycle's counts."""


Row = Mapping[str, Any]


class ReadStore(Protocol):
    """What the read use case needs from storage. Rows are read-only mappings of column to value."""

    def state(self, provider: str) -> ProviderState:
        """A book's cursor and health, or an empty state if it has never synced."""

    def bets(self, sportsbook: str | None = None, status: str | None = None, event_id: str | None = None,
             limit: int = 100, offset: int = 0) -> list[Row]:
        """Stored bets, newest placed first, filtered by sportsbook, status or game."""

    def bet(self, bet_id: str) -> Row | None:
        """One stored bet, or None."""

    def history(self, bet_id: str, limit: int = 50) -> list[Row]:
        """A bet's observations, newest first."""

    def events(self) -> list[Row]:
        """Every game, with its provider aliases and the number of bets on it."""

    def incomplete(self, sportsbook: str, limit: int = 10) -> list[str]:
        """External IDs of bets with fields we still don't know."""

    def count_bets(self) -> dict:
        """Bet counts by sportsbook, then by status."""

    def recent_runs(self, provider: str, limit: int = 10) -> list[Row]:
        """A book's most recent sync runs, newest first."""

    def outcome_totals(self, provider: str) -> dict:
        """How many observations from a book ended in each outcome, plus rejections."""
