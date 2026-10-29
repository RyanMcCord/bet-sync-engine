"""python -m betsync: start the simulated sportsbooks, the sync engine and the API, in one process."""
import logging
import os
import sys
from datetime import datetime, timezone

from . import config, providers
from .api import create_app
from .service import FreshnessPolicy, ReadService
from .simulator import ScaledClock, Simulator
from .simulator.http import create_app as create_upstream_app
from .store import Store
from .sync import SyncEngine, SyncPolicy


def main(argv=None):
    cfg = config.load(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    if cfg.fresh:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(cfg.db_path + suffix):
                os.remove(cfg.db_path + suffix)

    # The simulated sportsbooks. Simulated time starts now and runs `speed` times faster.
    sim = Simulator(ScaledClock(datetime.now(timezone.utc), cfg.speed), scenario=cfg.scenario, seed=cfg.seed)

    store = Store(cfg.db_path)
    adapters = providers.build(f"http://127.0.0.1:{cfg.port}/upstream", cfg.request_timeout)
    engine = SyncEngine(adapters, store, SyncPolicy(poll_interval=cfg.poll_interval))
    service = ReadService(store, [p.name for p in adapters], FreshnessPolicy(cfg.stale_after))
    app = create_app(service, engine, upstream=create_upstream_app(sim))

    logging.getLogger("betsync").info(
        "scenario=%s seed=%d speed=%gx poll=%gs stale_after=%gs db=%s; API on http://%s:%d (docs at /docs)",
        cfg.scenario, cfg.seed, cfg.speed, cfg.poll_interval, cfg.stale_after, cfg.db_path, cfg.host, cfg.port)

    import uvicorn
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
