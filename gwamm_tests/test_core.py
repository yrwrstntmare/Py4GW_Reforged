"""Offline checks. Run from the folder that contains Sources/:  python tests/test_core.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import simulate
from Sources.gwamm.core.geometry import NavGraph
from Sources.gwamm.core.regions import RegionMap
from Sources.gwamm.core.cartography import CartoGrid, Projection
from Sources.gwamm.core import elites


def test_geometry():
    traps, start, _ = simulate.load()
    nav = NavGraph(traps)
    rm = RegionMap(nav, start, 2500.0)
    assert nav.on_mesh(*start)
    assert sum(rm.reachable) > 0.9 * len(nav.nodes)
    assert all(len(r.nodes) > 0 for r in rm.regions)
    # every reachable node lies within the radius of some viewpoint
    for i, ok in enumerate(rm.reachable):
        if ok:
            x, y = nav.nodes[i]
            assert any(rm.reachable[j] for j in nav.nodes_within(x, y, 2500.0))
    tour = rm.tour(rm.region_at(*start))
    assert sorted(tour) == list(range(len(rm.regions)))
    a, b = nav.nearest_node(*start), rm.regions[-1].node
    path = nav.path(a, b)
    assert path and path[0] == a and path[-1] == b
    print("geometry ok:", len(nav.nodes), "nodes,", len(rm.regions), "regions")


def test_cartography():
    proj = Projection(1000.0, 2000.0, -9229.5, 49152.0)
    cx, cy = proj.cell(0.0, 0.0)
    x0, y0, x1, y1 = proj.cell_corners(cx, cy)
    assert x0 <= 0.0 <= x1 and y0 <= 0.0 <= y1
    assert abs((x1 - x0) - 3072.0) < 1e-6
    words = [0] * 4096
    words[5 * 8 + 1] = 1 << 3                      # cell (35, 5)
    grid = CartoGrid(256, 512, words)
    assert grid.explored(35, 5) and not grid.explored(34, 5) and not grid.explored(-1, 0)
    print("cartography ok")


def test_elites():
    area = elites.AREAS[91]
    sec, pick = elites.choose(6, area, learnt=set(), signets=2)
    assert len(pick) == 2
    profs = {e.profession for e in pick}
    assert profs <= {6, sec}, "elites must come from primary + one secondary only"
    # both Elementalist elites known: both picks come from a single secondary
    sec, pick = elites.choose(6, area, learnt={226, 236}, signets=2)
    assert sec != 0 and {e.profession for e in pick} == {sec}
    sec, pick = elites.choose(6, area, learnt={e.skill_id for e in area}, signets=2)
    assert pick == []
    sec, pick = elites.choose(6, area, learnt={226, 236}, signets=2, unlocked={7})
    assert pick == []
    print("elites ok")


def test_full_runs():
    for seed in (1, 2, 3):
        r = simulate.run(seed)
        assert r["mode"] == "DONE" and r["left"] == 0, r
        assert r["closest_exit"] >= 1000, "walked into an exit zone"
        print("run ok:", r)


def test_route_and_trap_together():
    """A known route and a learnt trap in the same area: planning a step must work (it raised)."""
    import json
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, _b = load()
    route = [tuple(p) for p in json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                             "Sources", "gwamm", "data", "routes.json")))["91"]["points"]]
    eng = Engine(traps, start, hints=route)
    eng.hazards = [eng.nav.nodes[eng.player_node]]
    eng.update(start, [], 50, 0.0)
    assert eng.next_step() is not None


def test_detour_on_wall():
    """Pinned somewhere: the known route is picked up for a stretch, then planning is free again."""
    import json
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, _b = load()
    route = [tuple(p) for p in json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                             "Sources", "gwamm", "data", "routes.json")))["91"]["points"]]
    eng = Engine(traps, start, hints=route)
    eng.update(start, [], 50, 0.0)
    assert not eng.guiding()
    spot = eng.add_wall(start, (start[0] + 2000, start[1]))
    assert spot is not None and eng._detour and eng.guiding()
    assert eng.add_wall(start, (start[0] + 2000, start[1])) is None      # same place: not twice
    eng.update(start, [], 50, 1.0)
    step = eng.next_step()
    assert step is not None and eng.objective.kind == "guide", eng.objective
    for i, pt in enumerate(list(eng.guide)):
        eng.update(pt, [], 50, 2.0 + i)
    assert not eng.guide and not eng._detour
    assert eng.next_step() is not None and eng.objective.kind != "guide"


def test_step_clear():
    """Caught on scenery: the spot is marked without calling in the known route, and there is
    walkable ground nearby to step to that is further from the obstacle than we are."""
    import json, math
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, _b = load()
    route = [tuple(p) for p in json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                             "Sources", "gwamm", "data", "routes.json")))["91"]["points"]]
    eng = Engine(traps, start, hints=route)
    eng.update(start, [], 50, 0.0)
    spot = eng.add_wall(start, (start[0] + 2000, start[1]), detour=False)
    assert spot is not None and not eng._detour and not eng.guiding()
    out = eng.escape_point(start, spot)
    assert out is not None
    assert 300.0 <= math.hypot(out[0] - start[0], out[1] - start[1]) <= 800.0
    assert math.hypot(out[0] - spot[0], out[1] - spot[1]) > math.hypot(start[0] - spot[0], start[1] - spot[1])
    assert eng.next_step() is not None


def test_consumables():
    """Nothing is used unless switched on; death penalty comes first; each bonus obeys its own setting."""
    from Sources.gwamm.core import consumables as C
    from Sources.gwamm.core.engine import Config
    all_keys = [i.key for i in C.CATALOGUE]
    cfg = Config()
    f = C.Facts(deaths=5, foes_left=100, my_morale=40, party_morale=[40] * 8, in_bags=all_keys)
    assert C.decide(cfg, f) == (None, "off")                       # master switch off
    cfg.pcons_on = True
    assert C.decide(cfg, f)[0] is None                             # on, but no item chosen: still nothing
    cfg.pc_birthday_cupcake = C.HARD
    cfg.pc_essence_of_celerity = C.ALWAYS
    cfg.pc_four_leaf_clover = True
    cfg.pc_peppermint_candy_cane = True
    assert C.decide(cfg, f)[0].key == "four_leaf_clover"            # party penalised: that first
    f = C.Facts(deaths=0, foes_left=100, my_morale=60, party_morale=[60, 100, 100, 100], in_bags=all_keys,
                targets={"four_leaf_clover": 100, "peppermint_candy_cane": 100})
    assert C.decide(cfg, f)[0].key == "peppermint_candy_cane"       # only me: the personal one
    f = C.Facts(deaths=0, foes_left=100, my_morale=85, party_morale=[85] * 8, in_bags=all_keys)
    assert C.decide(cfg, f)[0].key == "essence_of_celerity"         # mild penalty: no morale item, the 'always' bonus
    f.running = {"essence_of_celerity"}
    it, why = C.decide(cfg, f)
    assert it is None and "Cupcake" in why                          # running already; cupcake waits for trouble
    f.deaths = 2
    assert C.decide(cfg, f)[0].key == "birthday_cupcake"
    f = C.Facts(deaths=0, foes_left=10, in_bags=all_keys)
    assert C.decide(cfg, f)[0] is None                              # not for the last few foes
    f = C.Facts(deaths=0, foes_left=100, in_bags=[])
    assert C.decide(cfg, f)[0] is None                              # nothing in the bags
    # every item in the catalogue has a setting on Config, off by default
    fresh = Config()
    assert all(not getattr(fresh, i.setting) for i in C.CATALOGUE)


if __name__ == "__main__":
    test_geometry()
    test_cartography()
    test_elites()
    test_full_runs()
    test_route_and_trap_together()
    test_detour_on_wall()
    test_step_clear()
    test_consumables()
    print("ALL PASSED")
