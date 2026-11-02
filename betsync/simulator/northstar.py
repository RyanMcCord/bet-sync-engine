"""Northstar Sportsbook (simulated): a REST API that returns each bet's *current* state.

    GET /v1/bets?updated_since=<ISO 8601>&page_token=<token>&limit=<n>

- Bets whose lastModified >= updated_since, oldest first, paginated with an opaque token.
- Readable schema: string IDs ("NS-10007"), American odds as strings ("+150"), dollar
  amounts as strings, ISO timestamps, verbose statuses (SETTLED_WIN), team codes.
- Its trouble: duplicates (records repeated across pages) and a lagging read replica that
  sometimes serves an out-of-date version of a bet.
"""
from datetime import UTC, datetime

from .faults import Faults, Response

STATUS = {"open": "PENDING", "won": "SETTLED_WIN", "lost": "SETTLED_LOSS", "push": "SETTLED_PUSH",
          "void": "VOIDED", "cashed_out": "CASHED_OUT"}
MARKET = {"moneyline": "MONEYLINE", "spread": "SPREAD", "total": "TOTAL"}


def _iso(dt):
    """Format a datetime as Northstar does: ISO 8601 in UTC with a Z."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dollars(cents):
    """Format cents as a dollar string like '25.00', keeping None as None."""
    return None if cents is None else f"{cents / 100:.2f}"


def _american(odds):
    """Format American odds as Northstar does: '+150' or '-110'."""
    return f"+{odds}" if odds > 0 else str(odds)


def render(bet, version):
    """Render one version of a simulated bet as a Northstar record."""
    return {
        "betId": f"NS-{10000 + bet.number}",
        "placedAt": _iso(bet.placed_at),
        "status": STATUS[version.status],
        "wager": {"amount": _dollars(bet.stake_cents), "currency": "USD"},
        "price": _american(bet.odds_american),
        "payout": _dollars(version.payout_cents),
        "settledAt": _iso(version.settled_at) if version.settled_at else None,
        "selections": [{
            "eventId": f"ns-evt-{900 + leg.game.gid}",
            "event": {"league": leg.game.league, "away": leg.game.away, "home": leg.game.home,
                      "startTime": _iso(leg.game.starts_at)},
            "market": MARKET[leg.market],
            "pick": leg.selection.upper(),
            "line": leg.line,
        } for leg in bet.legs],
        "lastModified": _iso(version.at),
    }


class Northstar:
    """Simulated Northstar: paged current state, with duplicates and a lagging replica."""

    name = "northstar"

    def __init__(self, world, faults: Faults):
        """world holds the true bets; faults decides how this book misbehaves."""
        self.world, self.faults = world, faults

    def list_bets(self, params: dict) -> Response:
        """GET /v1/bets: bets changed since updated_since, oldest change first, paged by page_token.
        Subject to faults, which include duplicates across pages and a lagging replica that serves
        older versions.
        """
        failure = self.faults.before()
        if failure:
            return failure
        try:
            since = datetime.fromisoformat(params.get("updated_since", "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
            offset = int(params.get("page_token") or 0)
            limit = max(1, min(int(params.get("limit", 25)), 100))
        except ValueError:
            return Response(400, {"error": "bad parameters"}, {})

        rows = sorted((v.at, bet.number, render(bet, v))
                      for bet, v in self.world.current_bets(self.name) if v.at >= since)
        page = [r[2] for r in rows[offset:offset + limit]]

        if page and self.faults.chance(self.faults.profile.stale_rate):
            # A lagging replica: one bet comes back as an older version than the newest one.
            changed = [(bet, bet.versions) for bet, v in self.world.current_bets(self.name) if len(bet.versions) > 1
                       and bet.versions[-1].at <= self.world.now()]
            if changed:
                bet, versions = self.faults.rng.choice(changed)
                page.append(render(bet, versions[0]))
        if offset and self.faults.chance(self.faults.profile.duplicate_rate):
            page = [r[2] for r in rows[max(0, offset - 3):offset]] + page   # the end of the last page, again

        more = offset + limit < len(rows)
        return Response(200, {"data": page, "next_page_token": str(offset + limit) if more else None},
                        {}, delay=self.faults.latency())
