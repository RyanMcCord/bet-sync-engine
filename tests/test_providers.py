"""The two adapters: each sportsbook's schema in, the same canonical model out."""
import unittest
from datetime import UTC, datetime

from betsync.adapters.providers import CascadeProvider, NorthstarProvider
from betsync.domain import BetStatus, Market
from betsync.ports import PermanentError, RecordError

NS_BET = {
    "betId": "NS-10007", "placedAt": "2026-11-01T18:00:00Z", "status": "SETTLED_WIN",
    "wager": {"amount": "25.00", "currency": "USD"}, "price": "+150", "payout": "62.50",
    "settledAt": "2026-11-02T02:10:00Z",
    "selections": [{"eventId": "ns-evt-901", "event": {"league": "NBA", "away": "PHX", "home": "LAL",
                                                       "startTime": "2026-11-01T23:30:00Z"},
                    "market": "MONEYLINE", "pick": "LAL", "line": None}],
    "lastModified": "2026-11-02T02:10:00Z",
}
def epoch(text):
    """An ISO 8601 timestamp as epoch seconds."""
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).timestamp())


CS_TICKET = {
    "ticket": 77012, "state": "W", "stake": 2500, "decimal_odds": 2.5, "return": 6250,
    "placed": epoch("2026-11-01T18:00:00Z"), "settled": epoch("2026-11-02T02:10:00Z"),
    "legs": [{"game": {"sport": "basketball/nba", "home": "Los Angeles Lakers", "away": "Phoenix Suns",
                       "start": epoch("2026-11-01T23:30:00Z")}, "type": "ml", "side": "home", "points": None}],
    "updated": epoch("2026-11-02T02:10:00Z"),
}


class MappingTests(unittest.TestCase):
    """Each adapter's translation into the canonical model."""

    def test_same_bet_same_canonical_shape(self):
        """The same bet from either book becomes the same canonical bet on the same game."""
        ns = NorthstarProvider(None).to_observation(NS_BET)
        cs = CascadeProvider(None).to_observation(CS_TICKET)
        for o in (ns, cs):
            self.assertEqual(o.bet.status, BetStatus.WON)
            self.assertEqual((o.bet.stake_cents, o.bet.odds_american, o.bet.payout_cents), (2500, 150, 6250))
            self.assertEqual(o.bet.legs[0].market, Market.MONEYLINE)
            self.assertEqual(o.bet.legs[0].selection, "LAL")
        self.assertEqual(ns.bet.legs[0].event_id, cs.bet.legs[0].event_id)   # the same game, from both books
        self.assertEqual((ns.bet.id, cs.bet.id), ("northstar:NS-10007", "cascade:77012"))
        self.assertEqual(ns.event_aliases, (("ns-evt-901", ns.bet.legs[0].event_id),))

    def test_absent_field_is_missing_but_null_is_a_value(self):
        """A field left out is reported missing; a field sent as null is kept as null."""
        ticket = dict(CS_TICKET)
        del ticket["decimal_odds"]
        ticket["return"] = None
        o = CascadeProvider(None).to_observation(ticket)
        self.assertEqual(o.missing_fields, ("odds_american",))
        self.assertIsNone(o.bet.payout_cents)

    def test_totals_and_away_sides(self):
        """Cascade's totals keep over/under, and an away side becomes the away team's code."""
        ticket = dict(CS_TICKET, legs=[
            {"game": CS_TICKET["legs"][0]["game"], "type": "total", "side": "over", "points": 221.5},
            {"game": CS_TICKET["legs"][0]["game"], "type": "spread", "side": "away", "points": 4.5}])
        legs = CascadeProvider(None).to_observation(ticket).bet.legs
        self.assertEqual([(leg.market, leg.selection, leg.line) for leg in legs],
                         [(Market.TOTAL, "over", 221.5), (Market.SPREAD, "PHX", 4.5)])

    def test_bad_record_raises_record_error(self):
        """A record that can't be translated raises RecordError, from either adapter."""
        for provider, raw in ((NorthstarProvider(None), dict(NS_BET, status="WEIRD")),
                              (CascadeProvider(None), {"ticket": 1})):
            with self.assertRaises(RecordError):
                provider.to_observation(raw)


class CursorTests(unittest.TestCase):
    """How each adapter pages through its book and where it resumes."""

    def test_northstar_follows_pages_then_rereads_an_overlap(self):
        """Northstar follows page tokens, then starts the next sweep just before the newest change seen."""
        calls, pages = [], [
            {"data": [dict(NS_BET, lastModified="2026-11-02T02:00:00Z")], "next_page_token": "50"},
            {"data": [dict(NS_BET, lastModified="2026-11-02T02:10:00Z")], "next_page_token": None},
        ]
        p = NorthstarProvider(lambda path, params: calls.append(params) or pages[len(calls) - 1])
        first = p.fetch(None)
        self.assertTrue(first.has_more)
        last = p.fetch(first.next_cursor)
        self.assertEqual(calls[1]["page_token"], "50")
        self.assertFalse(last.has_more)
        # next sweep starts 2 minutes before the newest change seen, so nothing at the edge is skipped
        self.assertEqual(last.next_cursor, "2026-11-02T02:08:00Z||")

    def test_northstar_cursor_never_moves_backwards(self):
        """An empty page leaves Northstar's cursor where it was."""
        p = NorthstarProvider(lambda path, params: {"data": [], "next_page_token": None})
        self.assertEqual(p.fetch("2026-11-02T03:00:00Z||").next_cursor, "2026-11-02T03:00:00Z||")

    def test_cascade_cursor_is_the_last_sequence(self):
        """Cascade's cursor becomes the last sequence number returned."""
        p = CascadeProvider(lambda path, params: {"changes": [{"seq": 41, "ticket": CS_TICKET}], "last_seq": 41,
                                                  "more": True})
        page = p.fetch("40")
        self.assertEqual((page.next_cursor, page.has_more, len(page.records)), ("41", True, 1))

    def test_unexpected_response_shape_is_permanent(self):
        """A response in the wrong shape is a permanent error, not something to retry."""
        with self.assertRaises(PermanentError):
            NorthstarProvider(lambda path, params: {"oops": True}).fetch(None)
        with self.assertRaises(PermanentError):
            CascadeProvider(lambda path, params: {"changes": "nope"}).fetch(None)


if __name__ == "__main__":
    unittest.main()
