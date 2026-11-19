"""The chaos drill's GPS burst is a pure function of its seed (fast, no database, no network)."""

from __future__ import annotations

from sim.drill import build_burst
from sim.scenario import at
from sim.world import World
from tower.config import ROOT


def test_burst_is_deterministic_and_counts_what_it_injects():
    world = World(ROOT / "data" / "network", 20261113)
    ships = world.in_transit(at(8, 15))[:40]
    a = build_burst(world, ships, at(8, 15), 7)
    b = build_burst(world, ships, at(8, 15), 7)
    assert a.batches == b.batches
    assert a.batches != build_burst(world, ships, at(8, 15), 8).batches
    assert a.stats["valid"] == len(a.valid_ids) == 160
    assert (a.stats["dup"], a.stats["skewed"], a.stats["bad"]) == (30, 12, 4)
    assert sum(len(x) for x in a.batches) == 160 + 30 + 12 + 4
