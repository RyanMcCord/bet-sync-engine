"""Northstar adapter: a "current state" API paged by last-modified time.

Cursor: "<updated_since>|<page_token>|<high_water_mark>".
- Within a sweep we follow page tokens.
- When a sweep ends, the next one starts at the newest lastModified we saw, minus an
  overlap window. Paging a list that changes underneath us can skip records that share a
  timestamp, so we deliberately re-read a little; the duplicates that causes are harmless
  because writes are idempotent.
"""
from datetime import datetime, timedelta, timezone

from ..domain import Bet, BetStatus, Event, Leg, Market, Observation, parse_american, team_code
from .base import Page, PermanentError, Provider, RecordError

STATUS = {"PENDING": BetStatus.OPEN, "SETTLED_WIN": BetStatus.WON, "SETTLED_LOSS": BetStatus.LOST,
          "SETTLED_PUSH": BetStatus.PUSH, "VOIDED": BetStatus.VOID, "CASHED_OUT": BetStatus.CASHED_OUT}
MARKET = {"MONEYLINE": Market.MONEYLINE, "SPREAD": Market.SPREAD, "TOTAL": Market.TOTAL}
EPOCH = "1970-01-01T00:00:00Z"
OVERLAP = timedelta(minutes=2)
PAGE_SIZE = 50


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cents(amount):
    return None if amount is None else round(float(amount) * 100)


class NorthstarProvider(Provider):
    name = "northstar"

    def __init__(self, get_json):
        self.get_json = get_json   # (path, params) -> dict, raising the errors in base.py

    def fetch(self, cursor):
        since, token, high = (cursor or f"{EPOCH}||").split("|")
        body = self.get_json("/v1/bets", {"updated_since": since, "page_token": token or None, "limit": PAGE_SIZE})
        if not isinstance(body.get("data"), list):
            raise PermanentError("Northstar response has no 'data' list")
        records = body["data"]
        for r in records:
            try:
                modified = r["lastModified"]
            except (KeyError, TypeError):
                continue
            if not high or _ts(modified) > _ts(high):
                high = modified
        next_token = body.get("next_page_token")
        if next_token:
            return Page(records, f"{since}|{next_token}|{high}", has_more=True)
        resume = _iso(_ts(high) - OVERLAP) if high else since
        return Page(records, f"{max(resume, since, key=_ts)}||", has_more=False)

    def record_id(self, raw):
        return str(raw.get("betId", "?")) if isinstance(raw, dict) else "?"

    def to_observation(self, raw):
        try:
            events, aliases, legs = [], [], []
            for s in raw["selections"]:
                ev = s["event"]
                event = Event.from_teams(ev["league"], ev["away"], ev["home"], _ts(ev["startTime"]))
                events.append(event)
                aliases.append((s["eventId"], event.id))
                pick = s["pick"]
                selection = pick.lower() if pick in ("OVER", "UNDER") else team_code(ev["league"], pick)[0]
                legs.append(Leg(event.id, MARKET[s["market"]], selection, s.get("line")))
            missing = tuple(f for f, key in (("odds_american", "price"), ("payout_cents", "payout")) if key not in raw)
            bet = Bet(
                sportsbook=self.name,
                external_id=raw["betId"],
                status=STATUS[raw["status"]],
                placed_at=_ts(raw["placedAt"]),
                stake_cents=_cents(raw["wager"]["amount"]),
                legs=tuple(legs),
                odds_american=parse_american(raw["price"]) if raw.get("price") else None,
                payout_cents=_cents(raw.get("payout")),
                settled_at=_ts(raw["settledAt"]) if raw.get("settledAt") else None,
            )
            return Observation(bet, _ts(raw["lastModified"]), tuple(events), tuple(aliases), missing, raw)
        except (KeyError, TypeError, ValueError) as e:
            raise RecordError(f"Northstar bet {self.record_id(raw)}: {e.__class__.__name__}: {e}") from e
