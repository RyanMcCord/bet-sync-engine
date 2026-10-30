"""Cascade adapter: an ordered change feed.

Cursor: the last sequence number stored. The feed delivers at least once, so changes we've
already had can come again; writes are idempotent, so that's harmless.
"""
from datetime import datetime, timezone

from ..domain import Bet, BetStatus, Event, Leg, Market, Observation, decimal_to_american
from .base import Page, PermanentError, Provider, RecordError

STATE = {"O": BetStatus.OPEN, "W": BetStatus.WON, "L": BetStatus.LOST, "P": BetStatus.PUSH,
         "V": BetStatus.VOID, "C": BetStatus.CASHED_OUT}
LEAGUE = {"basketball/nba": "NBA", "football/nfl": "NFL"}
MARKET = {"ml": Market.MONEYLINE, "spread": Market.SPREAD, "total": Market.TOTAL}
PAGE_SIZE = 50


def _ts(epoch):
    """Cascade timestamps are epoch seconds; turn one into a UTC datetime."""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


class CascadeProvider(Provider):
    name = "cascade"

    def __init__(self, get_json):
        """get_json is (path, params) -> dict, raising the errors in base.py."""
        self.get_json = get_json

    def fetch(self, cursor):
        """Get the next changes after the cursor (a sequence number). The new cursor is the last
        sequence returned, and never moves backwards.
        """
        since = int(cursor or 0)
        body = self.get_json("/api/tickets/changes", {"since_seq": since, "max": PAGE_SIZE})
        if not isinstance(body.get("changes"), list):
            raise PermanentError("Cascade response has no 'changes' list")
        last = int(body.get("last_seq", since))
        return Page([c.get("ticket") for c in body["changes"]], str(max(last, since)), bool(body.get("more")))

    def fetch_record(self, external_id):
        """Fetch one ticket as it stands now, to fill in fields an earlier record left out."""
        body = self.get_json(f"/api/tickets/{int(external_id)}", {})
        return body.get("ticket")

    def record_id(self, raw):
        """The ticket number of a raw record, or '?' if it has none."""
        return str(raw.get("ticket", "?")) if isinstance(raw, dict) else "?"

    def to_observation(self, raw):
        """Translate one Cascade ticket into the canonical model: decimal odds to American, one-letter
        states to statuses, home/away sides to team codes. Absent optional fields are reported as
        missing rather than guessed. Raises RecordError if the ticket can't be read.
        """
        try:
            events, legs = [], []
            for leg in raw["legs"]:
                g = leg["game"]
                league = LEAGUE[g["sport"]]
                event = Event.from_teams(league, g["away"], g["home"], _ts(g["start"]))
                events.append(event)
                side = leg["side"]
                selection = {"home": event.home, "away": event.away}.get(side, side)
                legs.append(Leg(event.id, MARKET[leg["type"]], selection, leg.get("points")))
            # A key that's absent means "not sent this time"; a null means "no value" (e.g. not settled yet).
            missing = tuple(f for f, key in (("odds_american", "decimal_odds"), ("payout_cents", "return"))
                            if key not in raw)
            bet = Bet(
                sportsbook=self.name,
                external_id=str(raw["ticket"]),
                status=STATE[raw["state"]],
                placed_at=_ts(raw["placed"]),
                stake_cents=int(raw["stake"]),
                legs=tuple(legs),
                odds_american=decimal_to_american(raw["decimal_odds"]) if raw.get("decimal_odds") else None,
                payout_cents=raw.get("return"),
                settled_at=_ts(raw["settled"]) if raw.get("settled") else None,
            )
            return Observation(bet, _ts(raw["updated"]), tuple(events), (), missing, raw)
        except (KeyError, TypeError, ValueError) as e:
            raise RecordError(f"Cascade ticket {self.record_id(raw)}: {e.__class__.__name__}: {e}") from e
