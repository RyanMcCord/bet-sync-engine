"""Two simulated sportsbooks (Northstar and Cascade) reporting on one seeded, evolving world,
each with its own schema and its own ways of misbehaving. Self-contained: no network, no
credentials. They run as HTTP servers so the sync engine meets real timeouts and status codes.
"""
from .faults import SCENARIOS, Faults, FaultProfile, Response
from .world import ManualClock, ScaledClock, World
from .northstar import Northstar
from .cascade import Cascade


class Simulator:
    def __init__(self, clock, scenario="realistic", seed=7):
        """Build the seeded world and both books, each with its own copy of the scenario's fault
        settings. Raises ValueError for an unknown scenario.
        """
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; choose from {', '.join(SCENARIOS)}")
        self.clock, self.scenario = clock, scenario
        self.world = World(clock, seed=seed)
        profiles = SCENARIOS[scenario]
        self.northstar = Northstar(self.world, Faults(FaultProfile(**profiles["northstar"].to_dict()), seed + 1))
        self.cascade = Cascade(self.world, Faults(FaultProfile(**profiles["cascade"].to_dict()), seed + 2))
        self.books = {"northstar": self.northstar, "cascade": self.cascade}


__all__ = ["Simulator", "ManualClock", "ScaledClock", "World", "Faults", "FaultProfile", "Response", "SCENARIOS"]
