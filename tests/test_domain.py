"""The canonical model and the conflict rule: pure logic, no I/O."""
import unittest
from datetime import UTC, datetime, timedelta

from betsync.domain import (
    Bet,
    BetStatus,
    Current,
    Event,
    Leg,
    Market,
    Observation,
    Outcome,
    decide,
    decimal_to_american,
    parse_american,
    team_code,
)

T = datetime(2026, 11, 1, 19, 0, tzinfo=UTC)
LEG = (Leg("nba-2026-11-01-phx-at-lal", Market.MONEYLINE, "LAL"),)


def bet(status=BetStatus.OPEN, **kw):
    """A sample Northstar bet; keyword arguments override its fields."""
    fields = {"sportsbook": "northstar", "external_id": "NS-1", "status": status, "placed_at": T,
              "stake_cents": 2500, "legs": LEG, "odds_american": 150}
    return Bet(**{**fields, **kw})


class ConflictRuleTests(unittest.TestCase):
    """The conflict rule: which observation wins, and what's a duplicate."""

    def test_first_sighting_is_inserted(self):
        """A bet we've never seen is inserted."""
        self.assertEqual(decide(None, Observation(bet(), T)).outcome, Outcome.INSERTED)

    def test_newer_wins(self):
        """A newer version replaces the stored one."""
        d = decide(Current(bet(), T), Observation(bet(BetStatus.WON, payout_cents=6250), T + timedelta(hours=3)))
        self.assertEqual(d.outcome, Outcome.UPDATED)
        self.assertEqual(d.bet.status, BetStatus.WON)

    def test_older_update_arriving_late_is_ignored(self):
        """An older version arriving after a newer one is stale and not applied."""
        settled = Current(bet(BetStatus.WON, payout_cents=6250), T + timedelta(hours=3))
        d = decide(settled, Observation(bet(), T))
        self.assertEqual((d.outcome, d.bet), (Outcome.STALE, None))

    def test_same_information_again_is_a_duplicate(self):
        """The same content again is a duplicate, even with a newer timestamp."""
        self.assertEqual(decide(Current(bet(), T), Observation(bet(), T)).outcome, Outcome.DUPLICATE)
        # a newer timestamp with nothing actually changed is still nothing to apply
        self.assertEqual(decide(Current(bet(), T), Observation(bet(), T + timedelta(minutes=5))).outcome,
                         Outcome.DUPLICATE)

    def test_same_timestamp_different_content_is_a_correction(self):
        """Different content at the same timestamp is treated as a correction and applied."""
        d = decide(Current(bet(), T), Observation(bet(stake_cents=3000), T))
        self.assertEqual((d.outcome, d.bet.stake_cents), (Outcome.UPDATED, 3000))

    def test_missing_optional_fields_are_carried_over_not_erased(self):
        """An optional field left out of an update keeps its stored value, and is noted as carried over."""
        won_without_odds = bet(BetStatus.WON, odds_american=None, payout_cents=6250)
        d = decide(Current(bet(), T), Observation(won_without_odds, T + timedelta(hours=3),
                                                  missing_fields=("odds_american",)))
        self.assertEqual((d.outcome, d.bet.odds_american, d.carried_over), (Outcome.UPDATED, 150, ("odds_american",)))

    def test_carried_over_field_that_matches_is_still_a_duplicate(self):
        """Leaving out a field we already hold doesn't, by itself, count as a change."""
        d = decide(Current(bet(), T), Observation(bet(odds_american=None), T, missing_fields=("odds_american",)))
        self.assertEqual(d.outcome, Outcome.DUPLICATE)

    def test_a_null_that_was_sent_is_not_carried_over(self):
        """A null the book actually sent is a value, not a gap, so it replaces the stored one."""
        # The book sent payout = null on purpose (e.g. not settled); that's a value, not a gap.
        d = decide(Current(bet(payout_cents=100), T), Observation(bet(payout_cents=None), T + timedelta(minutes=1)))
        self.assertIsNone(d.bet.payout_cents)


class IdentityTests(unittest.TestCase):
    """Canonical identity for teams, games and bets."""

    def test_codes_and_names_resolve_to_one_team(self):
        """Team codes and full names resolve to one code; unknown teams get a stable slug."""
        self.assertEqual(team_code("NBA", "Los Angeles Lakers"), ("LAL", True))
        self.assertEqual(team_code("NBA", "lal"), ("LAL", True))
        self.assertEqual(team_code("NBA", "Seattle SuperSonics"), ("seattle-supersonics", False))

    def test_both_books_describe_the_same_game(self):
        """Codes vs names, and slightly different start times, still give one event ID."""
        a = Event.from_teams("NBA", "PHX", "LAL", T)
        b = Event.from_teams("NBA", "Phoenix Suns", "Los Angeles Lakers", T + timedelta(minutes=7))
        self.assertEqual(a.id, b.id)
        self.assertEqual(a.id, "nba-2026-11-01-phx-at-lal")

    def test_game_date_is_local_not_utc(self):
        """A late game is dated by its local (Eastern) day, not its UTC day."""
        late_tipoff = datetime(2026, 11, 2, 2, 30, tzinfo=UTC)   # 9:30 PM Eastern on Nov 1
        self.assertIn("2026-11-01", Event.from_teams("NBA", "PHX", "LAL", late_tipoff).id)

    def test_bet_ids_are_namespaced_by_sportsbook(self):
        """Bet IDs carry their sportsbook as a prefix."""
        self.assertEqual(bet().id, "northstar:NS-1")


class OddsTests(unittest.TestCase):
    """Converting between the books' odds formats."""

    def test_conversions(self):
        """Decimal and American odds convert correctly, and invalid American odds are rejected."""
        self.assertEqual(decimal_to_american(2.5), 150)
        self.assertEqual(decimal_to_american(1.9091), -110)
        self.assertEqual(decimal_to_american(2.0), 100)
        self.assertEqual(parse_american("+150"), 150)
        self.assertEqual(parse_american("-110"), -110)
        self.assertEqual(parse_american("EVEN"), 100)
        with self.assertRaises(ValueError):
            parse_american("+50")
        with self.assertRaises(ValueError):
            decimal_to_american(1.0)


if __name__ == "__main__":
    unittest.main()
