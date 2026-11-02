"""Adapters: everything that talks to the outside world.

    providers/       the Provider port, once per sportsbook (driven)
    sqlite_store.py  the SyncStore and ReadStore ports on SQLite (driven)
    api.py           the HTTP API, which calls the application's read service (driving)

Adapters depend on the domain and the ports; none of them imports another adapter.
"""
