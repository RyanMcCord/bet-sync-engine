"""The read side: what the API returns, including how fresh each answer is.

Kept separate from the web framework so it's easy to test, and so the freshness rules live
in one place:

    ok             last cycle succeeded, and the last success is recent
    degraded       recent cycles are failing, but the data is still within the freshness window
    stale          no success within the freshness window: data may be out of date
    never_synced   no successful sync yet

Every bet carries its sportsbook's freshness, so a consumer can tell "this bet is open"
from "this bet was open the last time we could reach that book".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .store import Store, parse

SEVERITY = {"ok": 0, "degraded": 1, "stale": 2, "never_synced": 3}


@dataclass
class FreshnessPolicy:
    stale_after: float = 30.0   # seconds since the last successful sync before data counts as stale


def utcnow():
    """The current time in UTC (the default clock; tests pass their own)."""
    return datetime.now(timezone.utc)


class ReadService:
    def __init__(self, store: Store, providers: list[str], policy: FreshnessPolicy | None = None, now=utcnow):
        """store holds the data; providers lists the books to report on; policy sets the freshness window."""
        self.store, self.providers = store, providers
        self.policy = policy or FreshnessPolicy()
        self.now = now

    # --- freshness ----------------------------------------------------------------------

    def provider_health(self, name) -> dict:
        """One book's freshness: never_synced, stale (no success inside the window), degraded (recent
        attempts failing but data still recent) or ok, with its timings and last error.
        """
        s = self.store.state(name)
        now = self.now()
        age = (now - s.last_success_at).total_seconds() if s.last_success_at else None
        if s.last_success_at is None:
            status = "never_synced"
        elif age > self.policy.stale_after:
            status = "stale"
        elif s.consecutive_failures > 0:
            status = "degraded"
        else:
            status = "ok"
        return {
            "status": status,
            "last_success_at": _iso(s.last_success_at),
            "seconds_since_success": None if age is None else round(age, 1),
            "last_attempt_at": _iso(s.last_attempt_at),
            "consecutive_failures": s.consecutive_failures,
            "last_error": s.last_error,
            "last_error_at": _iso(s.last_error_at),
            "next_attempt_at": _iso(s.next_attempt_at),
            "cursor": s.cursor,
        }

    def _freshness_by_book(self):
        """Health for every book, keyed by name."""
        return {name: self.provider_health(name) for name in self.providers}

    # --- endpoints ----------------------------------------------------------------------

    def bets(self, sportsbook=None, status=None, event_id=None, limit=100, offset=0) -> dict:
        """A page of canonical bets, each carrying its own book's freshness, plus a summary per book."""
        health = self._freshness_by_book()
        rows = self.store.bets(sportsbook, status, event_id, limit, offset)
        return {
            "freshness": {name: _summary(h) for name, h in health.items()},
            "count": len(rows),
            "bets": [self._bet(r, health) for r in rows],
        }

    def bet(self, bet_id) -> dict | None:
        """One bet with its observation history (newest first), or None if we've never seen it."""
        row = self.store.bet(bet_id)
        if not row:
            return None
        out = self._bet(row, self._freshness_by_book())
        out["history"] = [{
            "received_at": h["received_at"], "provider_updated_at": h["source_updated_at"],
            "status": h["status"], "outcome": h["outcome"], "missing_fields": json.loads(h["missing_fields"]),
        } for h in self.store.history(bet_id)]
        return out

    def events(self) -> dict:
        """Every canonical game, with each book's ID for it and how many bets have a leg on it."""
        return {"events": [{
            "id": e["id"], "league": e["league"], "away": e["away"], "home": e["home"], "starts_at": e["starts_at"],
            "teams_recognised": bool(e["teams_known"]),
            "provider_ids": json.loads(e["aliases"] or "[]"),
            "bets": e["bets"],
        } for e in self.store.events()]}

    def status(self) -> dict:
        """Health per book, with outcome totals and recent runs, and the worst status as the overall one."""
        providers = {}
        for name in self.providers:
            h = self.provider_health(name)
            h["totals"] = self.store.outcome_totals(name)
            h["bets_with_unknown_fields"] = len(self.store.incomplete(name, 10_000))
            h["recent_runs"] = [{
                "started_at": r["started_at"], "ok": bool(r["ok"]), "pages": r["pages"], "received": r["received"],
                "inserted": r["inserted"], "updated": r["updated"], "duplicate": r["duplicate"], "stale": r["stale"],
                "rejected": r["rejected"], "retries": r["retries"], "repaired": r["repaired"], "error": r["error"],
            } for r in self.store.recent_runs(name, 5)]
            providers[name] = h
        worst = max((p["status"] for p in providers.values()), key=SEVERITY.get, default="never_synced")
        return {
            "status": worst,
            "checked_at": _iso(self.now()),
            "stale_after_seconds": self.policy.stale_after,
            "providers": providers,
            "bets": self.store.count_bets(),
        }

    def _bet(self, r, health) -> dict:
        """Turn a stored bet row into its API shape, attaching its book's freshness."""
        book = health.get(r["sportsbook"]) or {"status": "never_synced", "last_success_at": None}
        return {
            "id": r["id"],
            "sportsbook": r["sportsbook"],
            "external_id": r["external_id"],
            "status": r["status"],
            "type": "parlay" if len(json.loads(r["legs"])) > 1 else "single",
            "placed_at": r["placed_at"],
            "settled_at": r["settled_at"],
            "stake_cents": r["stake_cents"],
            "odds_american": r["odds_american"],
            "payout_cents": r["payout_cents"],
            "legs": json.loads(r["legs"]),
            "freshness": {
                "provider_updated_at": r["source_updated_at"],    # the book's own timestamp for this version
                "last_changed_at": r["last_changed_at"],          # when our copy last changed
                "last_confirmed_at": r["last_confirmed_at"],      # when the book last reported this bet at all
                "sportsbook_status": book["status"],
                "sportsbook_last_synced_at": book["last_success_at"],
                "stale": book["status"] in ("stale", "never_synced"),
                "version": r["version"],
                "fields_from_earlier_version": json.loads(r["carried_over"]),
                "unknown_fields": json.loads(r["incomplete"]),     # should be known; being re-fetched
            },
        }


def _summary(h):
    """The short form of a book's health, as included with every list of bets."""
    return {"status": h["status"], "last_success_at": h["last_success_at"],
            "seconds_since_success": h["seconds_since_success"]}


def _iso(dt):
    """Format a datetime as ISO 8601, keeping None as None."""
    return dt.isoformat() if dt else None
