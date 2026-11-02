"""bet-sync-engine: two imperfect, simulated sportsbooks synced into one dependable canonical view.

The layers, from the inside out (METHODOLOGY.md has the rules):

    domain.py      the canonical model and the conflict rule
    ports.py       the interfaces the application needs
    application/   the use cases: sync, and reads
    adapters/      the sportsbooks, SQLite and the HTTP API
    simulator/     the simulated sportsbooks (stand-ins for the outside world)
    __main__.py    the composition root
"""
