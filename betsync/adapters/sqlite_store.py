"""SQLite storage: the canonical state, plus enough history to explain how it got that way.

    bets            canonical bets (the API's source of truth)
    events          canonical games; event_aliases maps each book's own event IDs onto them
    observations    every record we received and what we did with it (inserted / updated /
                    duplicate / stale), so a bet's history can be traced back to the provider
                    (bets.incomplete lists fields a bet should have but no provider has sent yet)
    rejected        records we couldn't translate, kept for inspection
    provider_state  per provider: cursor, last attempt, last success, consecutive failures
    sync_runs       one row per sync cycle, with counts

Applying a page and advancing that provider's cursor happen in one transaction, so a crash
mid-page means the page is fetched again (at-least-once) rather than lost.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime

from ..domain import Bet, BetStatus, Current, Leg, Market, Outcome
from ..ports import ProviderState

SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id                 TEXT PRIMARY KEY,          -- sportsbook:external_id
    sportsbook         TEXT NOT NULL,
    external_id        TEXT NOT NULL,
    status             TEXT NOT NULL,
    placed_at          TEXT NOT NULL,
    settled_at         TEXT,
    stake_cents        INTEGER NOT NULL,
    odds_american      INTEGER,
    payout_cents       INTEGER,
    legs               TEXT NOT NULL,             -- JSON
    source_updated_at  TEXT NOT NULL,             -- the provider's timestamp for the version we hold
    content_hash       TEXT NOT NULL,
    version            INTEGER NOT NULL,          -- how many times the content changed
    carried_over       TEXT NOT NULL DEFAULT '[]',-- optional fields kept from an earlier version
    incomplete         TEXT NOT NULL DEFAULT '[]',-- fields it should have that we don't know yet
    first_seen_at      TEXT NOT NULL,
    last_changed_at    TEXT NOT NULL,
    last_confirmed_at  TEXT NOT NULL              -- last time the provider reported this bet at all
);
CREATE INDEX IF NOT EXISTS bets_by_book ON bets (sportsbook, status);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, league TEXT NOT NULL, away TEXT NOT NULL, home TEXT NOT NULL,
    starts_at TEXT NOT NULL, teams_known INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS event_aliases (
    provider TEXT NOT NULL, provider_event_id TEXT NOT NULL, event_id TEXT NOT NULL,
    PRIMARY KEY (provider, provider_event_id)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL, bet_id TEXT NOT NULL, received_at TEXT NOT NULL,
    source_updated_at TEXT NOT NULL, outcome TEXT NOT NULL, status TEXT NOT NULL,
    content_hash TEXT NOT NULL, missing_fields TEXT NOT NULL, raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_by_bet ON observations (bet_id, id);
CREATE TABLE IF NOT EXISTS rejected (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL, record_id TEXT NOT NULL, received_at TEXT NOT NULL, reason TEXT NOT NULL, raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_state (
    provider TEXT PRIMARY KEY,
    cursor TEXT,
    last_attempt_at TEXT, last_success_at TEXT, last_error TEXT, last_error_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL, ok INTEGER NOT NULL,
    pages INTEGER NOT NULL, received INTEGER NOT NULL, inserted INTEGER NOT NULL, updated INTEGER NOT NULL,
    duplicate INTEGER NOT NULL, stale INTEGER NOT NULL, rejected INTEGER NOT NULL, retries INTEGER NOT NULL,
    repaired INTEGER NOT NULL DEFAULT 0, error TEXT
);
"""


def iso(dt: datetime | None) -> str | None:
    """Format a datetime for storage: ISO 8601 in UTC, keeping None as None."""
    return dt.astimezone(UTC).isoformat() if dt else None


def parse(value: str | None) -> datetime | None:
    """Read a stored ISO 8601 timestamp back into a datetime, keeping None as None."""
    return datetime.fromisoformat(value) if value else None


def _legs_json(legs):
    """Serialise a bet's legs to JSON for the legs column."""
    return json.dumps([{"event_id": leg.event_id, "market": leg.market.value, "selection": leg.selection,
                        "line": leg.line}
                       for leg in legs])


def _row_to_bet(row) -> Bet:
    """Rebuild a canonical Bet from a stored row, so the conflict rule can compare against it."""
    legs = tuple(Leg(leg["event_id"], Market(leg["market"]), leg["selection"], leg["line"])
                 for leg in json.loads(row["legs"]))
    return Bet(row["sportsbook"], row["external_id"], BetStatus(row["status"]), parse(row["placed_at"]),
               row["stake_cents"], legs, row["odds_american"], row["payout_cents"], parse(row["settled_at"]))


class Store:
    """SQLite storage. Implements the SyncStore and ReadStore ports."""

    def __init__(self, path: str = "betsync.db"):
        """Open (or create) the database at path and make sure the schema exists."""
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use in WAL mode so reads don't block the writer."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self):
        """A write transaction. Writers take turns, and everything inside commits together or is
        rolled back together.
        """
        with self._write_lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    # --- provider state -------------------------------------------------------------

    def state(self, provider) -> ProviderState:
        """A book's cursor and health, or an empty state if it has never synced."""
        row = self._conn().execute("SELECT * FROM provider_state WHERE provider = ?", (provider,)).fetchone()
        if not row:
            return ProviderState(provider)
        return ProviderState(row["provider"], row["cursor"], parse(row["last_attempt_at"]),
                             parse(row["last_success_at"]),
                             row["last_error"], parse(row["last_error_at"]), row["consecutive_failures"],
                             parse(row["next_attempt_at"]))

    def states(self) -> list[ProviderState]:
        """Cursor and health for every known book."""
        rows = self._conn().execute("SELECT provider FROM provider_state ORDER BY provider").fetchall()
        return [self.state(r["provider"]) for r in rows]

    def ensure_provider(self, provider):
        """Create a book's state row if it doesn't exist yet."""
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO provider_state (provider) VALUES (?)", (provider,))

    def record_cycle(self, provider, *, ok: bool, at: datetime, error: str | None, next_attempt_at: datetime):
        """Record how a cycle went: success resets the failure count, failure records the error and
        adds one. Either way, store when the next attempt is due.
        """
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO provider_state (provider) VALUES (?)", (provider,))
            if ok:
                c.execute("UPDATE provider_state SET last_attempt_at = ?, last_success_at = ?, "
                          "consecutive_failures = 0, "
                          "next_attempt_at = ? WHERE provider = ?", (iso(at), iso(at), iso(next_attempt_at), provider))
            else:
                c.execute("UPDATE provider_state SET last_attempt_at = ?, last_error = ?, last_error_at = ?, "
                          "consecutive_failures = consecutive_failures + 1, next_attempt_at = ? WHERE provider = ?",
                          (iso(at), error, iso(at), iso(next_attempt_at), provider))

    def record_run(self, provider, run):
        """Save one cycle's counts to sync_runs, keeping only each book's latest 500."""
        with self.transaction() as c:
            c.execute("INSERT INTO sync_runs (provider, started_at, finished_at, ok, pages, received, inserted, "
                      "updated, "
                      "duplicate, stale, rejected, retries, repaired, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (provider, iso(run.started_at), iso(run.finished_at), int(run.ok), run.pages, run.received,
                       run.counts[Outcome.INSERTED], run.counts[Outcome.UPDATED], run.counts[Outcome.DUPLICATE],
                       run.counts[Outcome.STALE], run.rejected, run.retries, run.repaired, run.error))
            # keep the table from growing forever
            c.execute("DELETE FROM sync_runs WHERE provider = ? AND id NOT IN "
                      "(SELECT id FROM sync_runs WHERE provider = ? ORDER BY id DESC LIMIT 500)", (provider, provider))

    # --- writing a page ---------------------------------------------------------------

    @contextmanager
    def page(self):
        """Open one write transaction for a page and hand back its writer (the PageWriter port)."""
        with self.transaction() as c:
            yield SqlitePageWriter(c)

    # --- reads ----------------------------------------------------------------------

    def bets(self, sportsbook=None, status=None, event_id=None, limit=100, offset=0) -> list[sqlite3.Row]:
        """Stored bet rows, newest placed first, filtered by sportsbook, status or game."""
        q, args = "SELECT * FROM bets WHERE 1=1", []
        if sportsbook:
            q, args = q + " AND sportsbook = ?", args + [sportsbook]
        if status:
            q, args = q + " AND status = ?", args + [status]
        if event_id:
            q += " AND EXISTS (SELECT 1 FROM json_each(bets.legs) WHERE json_extract(value, '$.event_id') = ?)"
            args = args + [event_id]
        q += " ORDER BY placed_at DESC, id LIMIT ? OFFSET ?"
        return self._conn().execute(q, args + [limit, offset]).fetchall()

    def incomplete(self, sportsbook, limit=10) -> list[str]:
        """External IDs of bets with fields we still don't know, oldest change first."""
        rows = self._conn().execute("SELECT external_id FROM bets WHERE sportsbook = ? AND incomplete <> '[]' "
                                    "ORDER BY last_changed_at LIMIT ?", (sportsbook, limit)).fetchall()
        return [r["external_id"] for r in rows]

    def count_bets(self) -> dict:
        """Bet counts by sportsbook, then by status."""
        rows = self._conn().execute("SELECT sportsbook, status, count(*) AS n FROM bets "
                                    "GROUP BY sportsbook, status").fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["sportsbook"], {})[r["status"]] = r["n"]
        return out

    def bet(self, bet_id) -> sqlite3.Row | None:
        """One stored bet row, or None."""
        return self._conn().execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()

    def history(self, bet_id, limit=50) -> list[sqlite3.Row]:
        """A bet's observations, newest first."""
        return self._conn().execute("SELECT id, provider, received_at, source_updated_at, outcome, status, "
                                    "missing_fields "
                                    "FROM observations WHERE bet_id = ? ORDER BY id DESC LIMIT ?",
                                    (bet_id, limit)).fetchall()

    def events(self) -> list[sqlite3.Row]:
        """Every game, with its provider aliases and the number of bets that have a leg on it."""
        return self._conn().execute(
            "SELECT e.*, (SELECT json_group_array(json_object('provider', a.provider, 'id', a.provider_event_id)) "
            "FROM event_aliases a WHERE a.event_id = e.id) AS aliases, "
            "(SELECT count(*) FROM bets b, json_each(b.legs) l "
            "WHERE json_extract(l.value, '$.event_id') = e.id) AS bets "
            "FROM events e ORDER BY e.starts_at, e.id").fetchall()

    def recent_runs(self, provider, limit=10) -> list[sqlite3.Row]:
        """A book's most recent sync runs, newest first."""
        return self._conn().execute("SELECT * FROM sync_runs WHERE provider = ? ORDER BY id DESC LIMIT ?",
                                    (provider, limit)).fetchall()

    def outcome_totals(self, provider) -> dict:
        """How many observations from a book ended in each outcome, plus how many were rejected."""
        rows = self._conn().execute("SELECT outcome, count(*) AS n FROM observations WHERE provider = ? "
                                    "GROUP BY outcome",
                                    (provider,)).fetchall()
        totals = {o.value: 0 for o in Outcome}
        totals.update({r["outcome"]: r["n"] for r in rows})
        totals["rejected"] = self._conn().execute("SELECT count(*) FROM rejected WHERE provider = ?",
                                                  (provider,)).fetchone()[0]
        return totals


class SqlitePageWriter:
    """The PageWriter port on one open SQLite transaction."""

    def __init__(self, conn: sqlite3.Connection):
        """conn has a transaction open; the Store commits or rolls it back."""
        self.c = conn

    def current(self, bet_id):
        """The stored version of a bet and its provider timestamp, or None if we've never seen it."""
        row = self.c.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
        return Current(_row_to_bet(row), parse(row["source_updated_at"])) if row else None

    def save_events(self, provider, events, aliases):
        """Save canonical games, and the provider's own IDs for them, if they're new."""
        for e in events:
            self.c.execute("INSERT OR IGNORE INTO events (id, league, away, home, starts_at, teams_known) "
                           "VALUES (?,?,?,?,?,?)",
                           (e.id, e.league, e.away, e.home, iso(e.starts_at), int(e.teams_known)))
        for provider_event_id, event_id in aliases:
            self.c.execute("INSERT OR IGNORE INTO event_aliases VALUES (?,?,?)",
                           (provider, provider_event_id, event_id))

    def insert_bet(self, bet, source_updated_at, now):
        """Store a bet seen for the first time, at version 1."""
        b = bet
        self.c.execute("INSERT INTO bets (id, sportsbook, external_id, status, placed_at, settled_at, stake_cents, "
                       "odds_american, payout_cents, legs, source_updated_at, content_hash, version, carried_over, "
                       "incomplete, first_seen_at, last_changed_at, last_confirmed_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,'[]',?,?,?,?)",
                       (b.id, b.sportsbook, b.external_id, b.status.value, iso(b.placed_at), iso(b.settled_at),
                        b.stake_cents, b.odds_american, b.payout_cents, _legs_json(b.legs), iso(source_updated_at),
                        b.content_hash(), json.dumps(b.unknown_fields()), iso(now), iso(now), iso(now)))

    def update_bet(self, bet, source_updated_at, carried_over, now):
        """Replace a stored bet with a newer version and bump its version number."""
        b = bet
        self.c.execute("UPDATE bets SET status = ?, placed_at = ?, settled_at = ?, stake_cents = ?, odds_american = ?, "
                       "payout_cents = ?, legs = ?, source_updated_at = ?, content_hash = ?, version = version + 1, "
                       "carried_over = ?, incomplete = ?, last_changed_at = ?, last_confirmed_at = ? WHERE id = ?",
                       (b.status.value, iso(b.placed_at), iso(b.settled_at), b.stake_cents, b.odds_american,
                        b.payout_cents, _legs_json(b.legs), iso(source_updated_at), b.content_hash(),
                        json.dumps(list(carried_over)), json.dumps(b.unknown_fields()), iso(now), iso(now), b.id))

    def confirm_bet(self, bet_id, source_updated_at, now):
        """Nothing changed, but the provider has just confirmed the bet; keep its newest timestamp."""
        self.c.execute("UPDATE bets SET last_confirmed_at = ?, source_updated_at = max(source_updated_at, ?) "
                       "WHERE id = ?", (iso(now), iso(source_updated_at), bet_id))

    def record_observation(self, provider, observation, outcome, now):
        """Append a received record and its outcome to the bet's history."""
        obs = observation
        self.c.execute("INSERT INTO observations (provider, bet_id, received_at, source_updated_at, outcome, status, "
                       "content_hash, missing_fields, raw) VALUES (?,?,?,?,?,?,?,?,?)",
                       (provider, obs.bet.id, iso(now), iso(obs.source_updated_at), outcome.value,
                        obs.bet.status.value, obs.bet.content_hash(), json.dumps(obs.missing_fields),
                        json.dumps(obs.raw, default=str)))

    def record_rejected(self, provider, record_id, reason, raw, now):
        """Keep a record that couldn't be translated, with the reason, for inspection."""
        self.c.execute("INSERT INTO rejected (provider, record_id, received_at, reason, raw) VALUES (?,?,?,?,?)",
                       (provider, record_id, iso(now), reason, json.dumps(raw, default=str)))

    def set_cursor(self, provider, cursor):
        """Move the provider's cursor, as part of this page's transaction."""
        self.c.execute("INSERT OR IGNORE INTO provider_state (provider) VALUES (?)", (provider,))
        self.c.execute("UPDATE provider_state SET cursor = ? WHERE provider = ?", (cursor, provider))
