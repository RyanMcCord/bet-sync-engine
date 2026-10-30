"""Cascade Bet (simulated): a change feed that returns every change to a ticket, in order.

    GET /api/tickets/changes?since_seq=<n>&max=<n>
    GET /api/tickets/<ticket>               one ticket as it stands now

- Changes with seq > since_seq, each carrying the ticket as it stood at that change.
- Terse schema: numeric ticket IDs, decimal odds, money in integer cents, epoch-second
  timestamps, one-letter states (O/W/L/P/V/C), full team names, home/away sides.
- Its trouble: at-least-once delivery (changes redelivered after the client has had them)
  and records that leave out optional fields.
"""
from .faults import Faults, Response

STATE = {"open": "O", "won": "W", "lost": "L", "push": "P", "void": "V", "cashed_out": "C"}
SPORT = {"NBA": "basketball/nba", "NFL": "football/nfl"}
LEG_TYPE = {"moneyline": "ml", "spread": "spread", "total": "total"}


def _epoch(dt):
    """A datetime as epoch seconds, keeping None as None."""
    return int(dt.timestamp()) if dt else None


def _decimal(odds):
    """Convert American odds to decimal, rounded to 4 places as Cascade sends them."""
    return round(1 + (odds / 100 if odds > 0 else 100 / -odds), 4)


def render(bet, version, team_names):
    """Render one version of a simulated bet as a Cascade ticket."""
    def side(leg):
        """Cascade's side for a leg: over/under for totals, otherwise home or away."""
        if leg.market == "total":
            return leg.selection            # over / under
        return "home" if leg.selection == leg.game.home else "away"

    return {
        "ticket": 77000 + bet.number,
        "state": STATE[version.status],
        "stake": bet.stake_cents,
        "decimal_odds": _decimal(bet.odds_american),
        "return": version.payout_cents,
        "placed": _epoch(bet.placed_at),
        "settled": _epoch(version.settled_at),
        "legs": [{
            "game": {"sport": SPORT[leg.game.league], "home": team_names[leg.game.league][leg.game.home],
                     "away": team_names[leg.game.league][leg.game.away], "start": _epoch(leg.game.starts_at)},
            "type": LEG_TYPE[leg.market],
            "side": side(leg),
            "points": leg.line,
        } for leg in bet.legs],
        "updated": _epoch(version.at),
    }


class Cascade:
    name = "cascade"
    OPTIONAL = ("decimal_odds", "return")

    def __init__(self, world, faults: Faults):
        """world holds the true bets; faults decides how this book misbehaves."""
        from ..domain import TEAMS
        self.world, self.faults, self.team_names = world, faults, TEAMS

    def ticket(self, number: int) -> Response:
        """GET /api/tickets/{ticket}: one ticket as it stands now (subject to the same faults)."""
        failure = self.faults.before()
        if failure:
            return failure
        for bet, version in self.world.current_bets(self.name):
            if 77000 + bet.number == number:
                ticket = render(bet, version, self.team_names)
                if self.faults.chance(self.faults.profile.missing_field_rate):
                    ticket.pop(self.faults.rng.choice(self.OPTIONAL), None)
                return Response(200, {"ticket": ticket}, {}, delay=self.faults.latency())
        return Response(404, {"error": "no such ticket"}, {})

    def changes(self, params: dict) -> Response:
        """GET /api/tickets/changes: changes after since_seq, in order. Subject to faults, which
        include redelivering changes the client has already had and leaving out optional fields.
        """
        failure = self.faults.before()
        if failure:
            return failure
        try:
            since = int(params.get("since_seq", 0))
            limit = max(1, min(int(params.get("max", 25)), 100))
        except ValueError:
            return Response(400, {"error": "bad parameters"}, {})

        log = [{"seq": i + 1, "ticket": render(bet, v, self.team_names)}
               for i, (bet, v) in enumerate(self.world.change_log(self.name))]
        page = log[since:since + limit]
        if since and self.faults.chance(self.faults.profile.duplicate_rate):
            page = log[max(0, since - 3):since] + page          # redelivered: at-least-once
        for change in page:
            if self.faults.chance(self.faults.profile.missing_field_rate):
                change["ticket"].pop(self.faults.rng.choice(self.OPTIONAL), None)
        last = page[-1]["seq"] if page else since
        return Response(200, {"changes": page, "last_seq": max(last, since), "more": since + limit < len(log)},
                        {}, delay=self.faults.latency())
