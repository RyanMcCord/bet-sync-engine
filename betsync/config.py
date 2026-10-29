"""Settings, from environment variables (all optional) or command-line flags."""
import argparse
import os
from dataclasses import dataclass


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = "betsync.db"
    scenario: str = "realistic"      # calm | realistic | chaos: how badly the simulated books behave
    seed: int = 7                    # same seed, same simulated world and same failures
    speed: float = 60.0              # simulated seconds per real second (60 = a minute per second)
    poll_interval: float = 5.0       # seconds between sync cycles per sportsbook
    stale_after: float = 30.0        # seconds without a successful sync before a book's data is stale
    request_timeout: float = 3.0     # seconds before a request to a sportsbook counts as timed out
    fresh: bool = False              # delete the database on start


def load(argv=None) -> Config:
    env = os.environ.get
    d = Config()
    p = argparse.ArgumentParser(prog="python -m betsync", description="Bet sync engine with two simulated sportsbooks.")
    p.add_argument("--host", default=env("BETSYNC_HOST", d.host))
    p.add_argument("--port", type=int, default=int(env("BETSYNC_PORT", d.port)))
    p.add_argument("--db", dest="db_path", default=env("BETSYNC_DB", d.db_path))
    p.add_argument("--scenario", choices=["calm", "realistic", "chaos"], default=env("BETSYNC_SCENARIO", d.scenario))
    p.add_argument("--seed", type=int, default=int(env("BETSYNC_SEED", d.seed)))
    p.add_argument("--speed", type=float, default=float(env("BETSYNC_SPEED", d.speed)))
    p.add_argument("--poll-interval", type=float, default=float(env("BETSYNC_POLL_INTERVAL", d.poll_interval)))
    p.add_argument("--stale-after", type=float, default=float(env("BETSYNC_STALE_AFTER", d.stale_after)))
    p.add_argument("--request-timeout", type=float, default=float(env("BETSYNC_REQUEST_TIMEOUT", d.request_timeout)))
    p.add_argument("--fresh", action="store_true", help="start with an empty database")
    return Config(**vars(p.parse_args(argv)))
