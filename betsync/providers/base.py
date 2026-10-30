"""The contract every sportsbook adapter implements, and the errors it may raise.

An adapter does two things (plus one optional one) and nothing else:
  fetch(cursor)        get the next page of raw records from the provider
  to_observation(raw)  translate one raw record into the canonical model
  fetch_record(id)     optional: get one record again, to fill in missing fields

Retries, backoff, deduplication, conflict resolution, storage and health all live in the
sync engine, so adding or changing a provider never means touching them.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..domain import Observation


class ProviderError(Exception):
    """Fetching failed. `retryable` tells the sync engine whether trying again could help."""
    retryable = True


class TransientError(ProviderError):
    """5xx, timeouts, dropped connections: likely to work if we try again later."""


class RateLimited(ProviderError):
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
    records: list[dict]
    next_cursor: str | None    # where to resume once this page is safely stored
    has_more: bool             # more pages available right now


class Provider(ABC):
    name: str

    @abstractmethod
    def fetch(self, cursor: str | None) -> Page:
        """Raises TransientError, RateLimited or PermanentError."""

    @abstractmethod
    def to_observation(self, raw: dict) -> Observation:
        """Raises RecordError if the record can't be translated."""

    def fetch_record(self, external_id: str) -> dict | None:
        """Fetch one record again, to fill in fields that were missing. Optional: providers
        without a single-record endpoint return None, and the gap stays visible in the API."""
        return None

    def record_id(self, raw: dict) -> str:
        """The provider's ID for a raw record, for logs and the rejected-records list."""
        return "?"
