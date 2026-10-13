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


def test_world_gate_fix():
    """A gate filed under another area than the one it leads to is put right: the way to Cursed
    Lands from Beetletun went out into Watchtower Coast and stopped there."""
    from Sources.gwamm.core.world import World
    w = World()
    r = w.route(56, [136])
    assert r and r[-1].get("expect") == 56, r


def test_carto_edge_spot():
    """Stood in a map cell, it was credited, but the fogged cell beside it (off the path) was
    not: once, the bot stands on the spot of its cell nearest that fog before giving up."""
    import math
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, b = load()
    proj = Projection(1000.0, 2000.0, b[0], b[3])
    eng = Engine(traps, start, projection=proj)
    cell, (foot, _n, _d) = next(iter(eng.carto.stand.items()))
    side = (cell[0] + 1, cell[1])
    spot = eng._carto_edge_spot(cell, {cell, side})
    assert spot is not None and proj.cell(*spot) == cell
    x0, y0, x1, y1 = proj.cell_corners(*side)
    fog = ((x0 + x1) / 2, (y0 + y1) / 2)
    assert math.hypot(spot[0] - fog[0], spot[1] - fog[1]) <= math.hypot(foot[0] - fog[0], foot[1] - fog[1]) + 1
    assert eng._carto_edge_spot(cell, {cell, side}) is None          # once only


def test_carto_missing():
    """Fog on ground our map cannot walk to is reported, not silently left (Toolbox showed map
    left to do while the bot's count read 0)."""
    from Sources.gwamm.core.cartography import CartoPlan

    class Nav:
        traps = [(0, 0.0, 3000.0, 3000.0, 0.0, 3000.0, 0.0),            # walkable, reached
                 (0, 20000.0, 23000.0, 3000.0, 20000.0, 23000.0, 0.0)]  # walkable, cut off
        nodes = [(1500.0, 1500.0), (21500.0, 1500.0)]
        trap_nodes = [[0], [1]]

        def in_no_go(self, x, y):
            return False

    class RM:
        nav = Nav()
        reachable = [True, False]

    proj = Projection(1000.0, 2000.0, -9229.5, 49152.0)
    plan = CartoPlan(RM(), proj)
    grid = CartoGrid(256, 512, [0] * 4096)
    miss = plan.missing(grid)
    assert miss.get("cut", 0) >= 1 and miss["where"]["cut"], miss
    assert proj.cell(21500.0, 1500.0) not in plan.stand


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
    # quest-time bosses: listed, never counted on, and no signet carried just for them
    wastes = elites.AREAS[444]
    maybe = [e for e in wastes if e.when]
    assert maybe and all(not e.guaranteed for e in maybe) and any(e.guaranteed for e in wastes)
    order = elites.prioritise(4, 6, wastes, set(), 444)          # necromancer / elementalist
    assert [e.when for e in order[:2]] == ["", ""] and len(order) == 4
    assert elites.signets_worth_carrying(order, 1) == 2
    assert elites.signets_worth_carrying([e for e in order if e.when], 1) == 1
    assert elites.signets_worth_carrying([e for e in order if e.when], 0) == 0
    assert elites.signets_worth_carrying([], 1) == 0
    # areas the lists cover with no elite boss are known as such, not as "no data"
    assert 33 in elites.NO_ELITES and 33 not in elites.AREAS and not (elites.NO_ELITES & set(elites.AREAS))
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


def test_route_with_actions_in_order():
    """A route with things to do along it is walked as written from its first point (not turned
    to start at the nearest one), and the engine says which original point comes next."""
    import json
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, _b = load()
    route = [tuple(p) for p in json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                             "Sources", "gwamm", "data", "routes.json")))["91"]["points"]]
    mid = route[len(route) // 2]
    eng = Engine(traps, mid, guide=route, guide_actions=[{"at": 3, "do": "wait", "value": 100}])
    assert eng.guide_src and eng.guide_src == sorted(eng.guide_src)
    assert eng.next_guide_src() == eng.guide_src[0] and eng.guide_src[0] <= 2
    plain = Engine(traps, mid, guide=route)
    assert plain.guide_src is None and plain.next_guide_src() == 10 ** 9
    eng.guide_i = len(eng.guide)
    assert eng.next_guide_src() == 10 ** 9


def test_route_with_actions_survives_death():
    """A death by the next point of an in-order route: that point is still the plan, and map
    cells far from the route are left for after it (the route was dropped for 10 minutes and
    the party crossed the area for the map). A plain route still works elsewhere meanwhile."""
    import json
    from simulate import load
    from Sources.gwamm.core.engine import Engine, Objective
    traps, start, _b = load()
    route = [tuple(p) for p in json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                             "Sources", "gwamm", "data", "routes.json")))["91"]["points"]]
    for actions, kept in (([{"at": 3, "do": "wait", "value": 100}], True), (None, False)):
        eng = Engine(traps, route[0], guide=route, guide_actions=actions)
        eng.update(route[0], [], 50, 0.0)
        assert eng.guiding()
        nxt = eng.guide[eng.guide_i]
        far = max(route, key=lambda p: (p[0] - nxt[0]) ** 2 + (p[1] - nxt[1]) ** 2)
        cell = Objective("carto", ("carto", (0, 0)), far, eng.rm.region_at(*far), 5.0)   # a map cell across the area
        assert eng._cost(cell.pos, cell.region) > eng.cfg.carto_route_reach
        real = eng._all_candidates
        eng._all_candidates = lambda: real() + [cell]
        eng.record_death(nxt)
        eng.update(route[0], [], 50, 1.0)
        kinds = [o.kind for o in eng._candidates()]
        assert ("guide" in kinds) == kept and ("carto" in kinds) == (not kept), (actions, kinds)


def test_wall_never_shuts_the_only_way():
    """A spot the party was pinned at is kept off, unless that would cut part of the map away:
    then it is the only way through and is walked with care instead."""
    import math
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, _b = load()
    eng = Engine(traps, start)
    nav = eng.nav
    reach = [i for i, ok in enumerate(nav.component(nav.nearest_node(*start))) if ok]
    cuts = {}
    for i in reach[::max(1, len(reach) // 150)]:
        x, y = nav.nodes[i]
        if math.hypot(x - start[0], y - start[1]) > 1500.0:
            cuts.setdefault(eng._wall_cuts(start, (x, y)), (x, y))
        if len(cuts) == 2:
            break
    assert set(cuts) == {True, False}, "the map must offer a passage and open ground"
    for cut, (x, y) in cuts.items():
        eng.player_xy = start
        spot = eng.add_wall((x - 350.0, y), (x + 100.0, y), detour=False)
        assert spot is not None
    pen = eng._hazard_penalty()
    for (x, y) in eng.walls:
        at = nav.nearest_node(x, y)
        assert pen[at] == (6.0 if (x, y) in eng.soft_walls else 1000.0), (pen[at], (x, y) in eng.soft_walls)
    assert len(eng.walls) == 2 and len(eng.soft_walls) >= 1 and eng.cfg.wall_radius <= 250.0


def test_shown_portal_keeps_its_doorway():
    """When rings round might-be doors cut the map up and are taken off wholesale, a portal the
    game shows at the edge of the map keeps its doorway ring; unnamed points do not."""
    import math
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    from Sources.gwamm.core.geometry import NavGraph
    traps, start, _b = load()
    nav = NavGraph(traps)
    s0 = nav.nearest_node(*start)
    seen = nav.component(s0)
    reach = [i for i, ok in enumerate(seen) if ok]
    far = lambda i: (nav.nodes[i][0] - start[0]) ** 2 + (nav.nodes[i][1] - start[1]) ** 2
    door = nav.nodes[max((i for i in reach if len(nav.adj[i]) <= 2), key=far)]        # a dead end far away
    spread = [nav.nodes[i] for i in reach[::max(1, len(reach) // 40)] if far(i) > 3000.0 ** 2
              and math.hypot(nav.nodes[i][0] - door[0], nav.nodes[i][1] - door[1]) > 2500.0]
    exits = [(door[0], door[1], 700.0)] + [(x, y, 700.0) for x, y in spread]
    plain = Engine(traps, start, exits=exits)
    shown = Engine(traps, start, exits=exits, portals=[door])
    key = [float(door[0]), float(door[1])]
    assert len(plain.removed_exits) >= len(spread) // 2, "the scene must force rings to be taken off"
    assert key in plain.removed_exits and key not in shown.removed_exits, (len(plain.removed_exits), len(shown.removed_exits))
    assert shown.nav.in_no_go(door[0], door[1])


def test_route_groups_now_or_later():
    """In-order route: a group in sight is taken now unless a later route point passes clearly
    nearer to it; a group the route never gets nearer to is not left to come back for."""
    import json
    import math
    from simulate import load
    from Sources.gwamm.core.engine import Engine, Objective
    traps, start, _b = load()
    route = [tuple(p) for p in json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                             "Sources", "gwamm", "data", "routes.json")))["91"]["points"]]
    eng = Engine(traps, route[0], guide=route, guide_actions=[{"at": 3, "do": "wait", "value": 100}])
    eng.update(route[0], [], 50, 0.0)
    far = max(eng.guide[eng.guide_i + 2:], key=lambda p: (p[0] - route[0][0]) ** 2 + (p[1] - route[0][1]) ** 2)
    by_later = (far[0] + 300.0, far[1])                       # stands by a point the route reaches later
    assert eng.route_comes_closer(by_later)
    here = (eng.player_xy[0] + 800.0, eng.player_xy[1])       # on top of us: fought, not weighed
    assert not eng.route_comes_closer(here)
    nxt = eng.guide[eng.guide_i]
    off = (2 * eng.player_xy[0] - nxt[0], 2 * eng.player_xy[1] - nxt[1])   # behind us, away from the route
    if min(math.hypot(off[0] - x, off[1] - y) for x, y in eng.guide) > math.hypot(off[0] - eng.player_xy[0], off[1] - eng.player_xy[1]):
        assert not eng.route_comes_closer(off)                # the route never gets nearer: now
    later = Objective("cluster", ("cluster", 901), by_later, eng.rm.region_at(*by_later), 25.0)
    real = eng._all_candidates
    eng._all_candidates = lambda: real() + [later]
    assert later not in eng._candidates() and eng.route_waiting == 1
    plain = Engine(traps, route[0], guide=route)
    plain.update(route[0], [], 50, 0.0)
    assert not plain.route_comes_closer(by_later)             # only on in-order routes


def test_ride_ground():
    """Ground crossed as a wurm needs the wurm; where the game set us down does not; the
    nearest burrow is the one to walk to, not one across the sulfur."""
    from Sources.gwamm.core.mounts import RideGround
    g = RideGround()
    assert not g.needs_mount((0, 0), (5000, 0))                 # never ridden: nothing is judged
    g.note((0, 0), False)
    for x in range(1000, 9001, 350):                            # mounted at 1000, rode east
        g.note((x, 0), True)
    assert g.note((9300, 0), False) == "off"                    # set down on rock at 9300
    assert not g.on_ridden((9300, 0)) and not g.needs_mount((9300, 0), (11000, 0))   # on over the rock: fine
    assert g.needs_mount((9300, 0), (5000, 0))                  # back the way we came: mount first
    assert g.nearest_mount((9300, 0), [(1000, 0), (9600, -900)]) == (9600, -900)
    assert g.nearest_mount((9300, 0), [(1000, 0), (9600, -900), (9000, -1500)]) == (9600, -900)
    g.note((9600, -900), True)
    g.note((7000, -900), True)
    assert g.note((7000, -900), False, alive=False) is None     # lost by dying: still wurm ground
    assert g.needs_mount((7000, -900))


def _grid_nav(w, h, step=200.0):
    """A plain open field as a map graph (nodes every `step`, 8 neighbours)."""
    import math
    from collections import defaultdict
    from Sources.gwamm.core import geometry as G
    nav = object.__new__(G.NavGraph)
    nx, ny = int(w // step) + 1, int(h // step) + 1
    nav.nodes = [(i * step, j * step) for j in range(ny) for i in range(nx)]
    nav.adj = [[] for _ in nav.nodes]
    for j in range(ny):
        for i in range(nx):
            for di, dj in ((1, 0), (0, 1), (1, 1), (1, -1)):
                a, b = i + di, j + dj
                if 0 <= a < nx and 0 <= b < ny:
                    u, v = j * nx + i, b * nx + a
                    d = step * math.hypot(di, dj)
                    nav.adj[u].append((v, d))
                    nav.adj[v].append((u, d))
    nav.forbidden = [False] * len(nav.nodes)
    nav._hash = defaultdict(list)
    for k, (x, y) in enumerate(nav.nodes):
        nav._hash[(int(x // G._HASH), int(y // G._HASH))].append(k)
    return nav


def test_foot_way_keeps_off_sulfur():
    """On foot, the walk to a burrow goes round ground ridden as a wurm, and the burrow picked
    is the nearest one reached that way (Sulfurous Wastes: it ran across the sulfur for a
    burrow 11k away and died, again and again)."""
    from Sources.gwamm.core.mounts import RideGround
    nav = _grid_nav(8000, 4000)
    g = RideGround()
    for y in range(0, 3001, 200):                       # a sulfur strip with rock above it
        g.note((4000, y), True)
    g.note((4000, 3000), False, alive=False)
    pts, safe, length = g.foot_way(nav, (1000, 500), (6000, 500))
    assert pts and safe and length > 6000, (safe, length)
    assert all(not g.on_ridden(p) for p in pts[:-1])
    for y in range(3000, 4001, 200):                    # now the strip runs edge to edge
        g.note((4000, y), True)
    _pts, safe, _l = g.foot_way(nav, (1000, 500), (6000, 500))
    assert not safe
    spot, _w, safe = g.safest_mount(nav, (1000, 500), [(6000, 500), (1500, 3800)])
    assert spot == (1500, 3800) and safe
    # a bridge over the strip is another level: walking it is not walking the sulfur
    b = RideGround()
    for y in range(0, 3001, 200):
        b.note((4000, y), True, plane=0)
    b.note((4000, 3000), False, alive=False, plane=0)
    assert b.on_ridden((4000, 1500), plane=0) and b.on_ridden((4000, 1500))
    assert not b.on_ridden((4000, 1500), plane=1)
    assert not b.needs_mount((2000, 1500), (6000, 1500), plane=1)
    assert b.needs_mount((2000, 1500), (6000, 1500), plane=0)
    # learnt ground carries to the next visit; ground found walkable is dropped from it
    cells, levels = b.merged_with([], [])
    c = RideGround()
    c.absorb(cells, levels)
    assert c.needs_mount((2000, 1500), (6000, 1500), plane=0) and not c.on_ridden((4000, 1500), plane=1)
    c.note((4000, 1000), True, plane=0)
    assert c.note((4000, 1000), False, plane=0) == "off"          # set down here: walkable
    cells2, _l = c.merged_with(cells, levels)
    assert len(cells2) < len(cells)
    d = RideGround()
    d.cleared = set(c.cleared)
    d.absorb(cells, levels)
    assert not d.on_ridden((4000, 1000))
    # walked on foot unharmed: cleared; hurt on foot where never ridden: marked
    e = RideGround()
    e.absorb(cells, levels)
    assert e.walked_safely((4000, 2200), plane=0) and not e.on_ridden((4000, 2200), plane=0)
    e.mark((100, 100), plane=0)
    assert e.on_ridden((100, 100), plane=0)


def test_route_chain():
    """Stuck short of an objective: the known route's own stretch towards it, in walking order."""
    from Sources.gwamm.core.geometry import route_chain
    route = [(0, 0), (1000, 0), (-1545, -8141), (-3883, -10860), (-3743, -12348), (5000, 5000)]
    assert route_chain(route, (-1400, -8000), (-3700, -12000)) == [(-1545, -8141), (-3883, -10860), (-3743, -12348)]
    assert route_chain(route, (-3700, -12300), (-1500, -8100)) == [(-3743, -12348), (-3883, -10860), (-1545, -8141)]
    assert route_chain(route, (20000, 20000), (-3700, -12000)) == []


def test_keep_off_doors():
    """A step that would end on a door is pulled back out of its reach."""
    import math
    from Sources.gwamm.core.geometry import keep_off_doors
    door = (24133.0, -4517.0)
    t = keep_off_doors((24585.0, -4445.0), (20000.0, -4000.0), [door])
    assert abs(math.hypot(t[0] - door[0], t[1] - door[1]) - 700.0) < 1.0 and t[0] < door[0]   # our side
    assert keep_off_doors((10000.0, 0.0), (0.0, 0.0), [door]) == (10000.0, 0.0)
    t = keep_off_doors(door, (23000.0, -4517.0), [door])            # dead on it: back towards us
    assert t[0] < door[0]


def test_choose_teleporter():
    """The teleporter whose arrival is nearest the unreachable goal; none if it does not help."""
    from Sources.gwamm.core.geometry import choose_teleporter
    tps = [{"at": [0, 0], "to": [5000, 5000]}, {"at": [1000, 0], "to": [-16000, 12000]}]
    assert choose_teleporter((500, 500), (-17000, 12500), tps)["to"] == [-16000, 12000]
    assert choose_teleporter((-16500, 12200), (-17000, 12500), tps) is None   # already there
    assert choose_teleporter((0, 0), (0, 500), tps) is None


def test_teleporter_sets():
    """A pad with four switches of one kind beside it is a teleporter; loose objects are not."""
    import math
    from Sources.gwamm.core.geometry import teleporter_sets
    g = [(1, 500, 0, 0), (2, 77, 300, 100), (3, 77, 400, 100), (4, 77, 500, 100), (5, 77, 600, 100),
         (9, 12, 9000, 9000), (10, 12, 9050, 9000)]
    sets = teleporter_sets(g)
    assert len(sets) == 1 and sets[0]["pad"][0] == 1 and sorted(a for a, _x, _y in sets[0]["switches"]) == [2, 3, 4, 5]
    assert teleporter_sets([(9, 12, 0, 0), (10, 12, 50, 0)]) == []
    # Prophet's Path, as seen standing on the pad at (-5053, 6002): four switches, each its own
    # id, no pad object - the pad is the centre of the circle they stand on
    pp = [(6, 751, -4308, 5897), (7, 752, -4364, 5680), (8, 753, -4581, 5411), (9, 754, -4809, 5343)]
    sets = teleporter_sets(pp)
    assert len(sets) == 1 and sets[0]["pad"][0] == 0, sets
    assert math.hypot(sets[0]["pad"][1] + 5053, sets[0]["pad"][2] - 6002) < 250, sets
    assert teleporter_sets([(6, 751, 0, 0), (7, 760, 300, 0), (8, 770, 600, 0), (9, 780, 0, 300)]) == []


def test_rank_teleporters():
    """The teleporter on the wrong side is left alone: the pad up on the cut-off ground while we
    are below, and a pad seen to take us away from the goal. One seen to help goes first."""
    from Sources.gwamm.core.geometry import rank_teleporters
    sw = lambda x, y: [(0, x + 700, y)] * 4
    below = {"pad": (0, -5053, 6002), "switches": sw(-5053, 6002)}
    other = {"pad": (0, -8256, 9363), "switches": sw(-8256, 9363)}
    up = {"pad": (0, -16500, 12000), "switches": sw(-16500, 12000)}
    cut = [(-16238, 11104), (-17037, 13891)]
    goal = (-16238, 11104)
    at_edge = (-10000, 4000)
    r = rank_teleporters([up, other, below], at_edge, goal, hints=[(-5053, 6002)], cut=cut)
    assert r == [below, other, up], r                                 # the one up there last
    went_away = [{"at": [-8256, 9363], "to": [9000, -9000]}]
    assert rank_teleporters([other, below], at_edge, goal, learnt=went_away, cut=cut) == [below]
    went_up = [{"at": [-8256, 9363], "to": [-16000, 11500]}]
    assert rank_teleporters([below, other], at_edge, goal, learnt=went_up, hints=[(-5053, 6002)], cut=cut)[0] is other
    # up there, going back down: the pad up there is the right one now
    r = rank_teleporters([below, up], (-16400, 11800), (-5000, 5000), cut=cut)
    assert r[0] is up, r


def test_put_off_helps():
    """A crowd too big to take is only left for later when later is better (death penalty to
    work off). At full strength it is the same crowd after a longer walk."""
    from Sources.gwamm.core import tactics as T
    assert not T.put_off_helps(12, 9, 9)                        # full strength: take it on now
    assert T.put_off_helps(12, T.take_limit(9, 76.0), 9)        # at -24%: leave it, come back stronger
    assert not T.put_off_helps(6, 7, 9)                         # within the limit: never a question


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


def test_wipe_zone():
    """Where the whole party died is kept off for a while (not for good: it has to be done) and
    paths to other places cost more there."""
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, _b = load()
    eng = Engine(traps, start)
    eng.update(start, [], 50, 0.0)
    spot = eng.nav.nodes[eng.player_node]
    assert not eng._dangerous(spot) and not eng._zone_penalty()
    assert eng.record_wipe(spot) == 1 and eng.record_wipe((spot[0] + 300, spot[1])) == 2 and len(eng.wipes) == 1
    assert eng._dangerous(spot) and eng.in_wipe_zone(spot)
    pen = eng._hazard_penalty()
    assert pen and pen[eng.player_node] == eng.cfg.wipe_path_factor
    assert eng.next_step() is not None                 # still plans (it is not a wall)
    assert eng.walled_off() == set()                   # no enemies known: nobody behind a wall
    eng.update(spot, [], 50, eng.cfg.wipe_return_seconds + 1.0)
    assert not eng._dangerous(spot) and eng.in_wipe_zone(spot)   # later: gone back to, carefully


def test_pull_rule():
    """Pull a group that has not noticed us; fight strays where they stand; forecast who joins in."""
    from Sources.gwamm.core import tactics as T
    group = [(1800.0, 0.0), (1900.0, 100.0), (2000.0, -100.0), (2100.0, 0.0)]
    assert T.pull_plan((0.0, 0.0), group, 4)[1] == 4                      # four, 1800 away: fetch them
    assert T.pull_plan((0.0, 0.0), group[:2], 4) is None                  # a pair: just fight it
    assert T.pull_plan((1000.0, 0.0), group, 4) is None                   # already on top of them
    assert T.pull_plan((-2000.0, 0.0), group, 4) is None                  # too far to fetch
    assert T.pull_plan((0.0, 0.0), [], 4) is None
    others = group + [(-600.0, 900.0), (300.0, -1200.0)]                  # two more standing beside us
    assert T.fight_forecast((0.0, 0.0), group[0], others) == (4, 2)
    assert T.fight_forecast((-3000.0, 0.0), group[0], others) == (4, 0)   # further back: the group alone


def test_engagement():
    """The forecast: who comes, who joins in, which patrols pass by, and what to do about it."""
    from Sources.gwamm.core import tactics as T
    def E(i, x, y, patrol=False, trail=()):
        return {"id": i, "xy": (float(x), float(y)), "patrol": patrol, "trail": list(trail)}
    boss = [E(1, 1800, 0), E(2, 2000, 200), E(3, 2100, -200), E(4, 2300, 0)]
    r = T.engagement((0, 0), boss, 4, 9)
    assert r["verdict"] == "pull" and r["group"] == 4 and r["total"] == 4 and r["target"] == 1
    assert T.engagement((0, 0), boss[:2], 4, 9)["verdict"] == "fight"            # a pair: just fight it
    assert T.engagement((1500, 0), boss, 4, 9) is None                           # already on top of them
    # a big patrol circling that group, at the moment right beside it: wait
    patrol = [E(10 + k, 2600 + 150 * k, 1800, True, [(2600, 1800), (1900, 1300), (1200, 900)]) for k in range(8)]
    r = T.engagement((0, 0), boss + patrol, 4, 9)
    assert r["verdict"] == "wait" and r["patrol_near"] == 8 and r["group"] == 4, r
    # a patrol of five passing on our side, clear of the group it circles: take the patrol first
    passing = [E(30 + k, -300 + 120 * k, 1900, True, [(-300, 1900), (2000, 1500)]) for k in range(5)]
    r = T.engagement((0, 0), boss + passing, 4, 9)
    assert r["verdict"] == "pull" and r.get("patrol_first") and r["group"] == 5 and r["target"] >= 30, r
    # the same five walking right through the group: taking them now brings the group too, so wait
    mixed = [E(30 + k, 2000 + 120 * k, 900, True, [(-300, 1900), (2000, 900)]) for k in range(5)]
    r = T.engagement((0, 0), boss + mixed, 4, 8)
    assert r["verdict"] == "wait" and r["total"] == 9 and not r.get("patrol_first"), r   # nine at once, limit eight
    # the same patrol at the far end of its beat: its path still passes by, but it is away now
    away = [E(10 + k, 6000 + 150 * k, 5000, True, [(6000, 5000), (1900, 1300)]) for k in range(8)]
    r = T.engagement((0, 0), boss + away, 4, 9)
    assert r["verdict"] == "pull" and r["patrols"] == 8 and r["patrol_near"] == 0, r
    # a patrol well off, never seen near the group, but walking straight at it: counts
    def W(i, x, y, vx, vy, n=8):
        d = E(i, x, y, True, [(x - 200 * k, y) for k in range(n)]); d["vel"] = (vx, vy); return d
    coming = [W(40 + k, 5000 + 100 * k, 0, -150.0, 0.0) for k in range(5)]
    r = T.engagement((0, 0), boss + coming, 4, 9)
    assert r["verdict"] == "wait" and r["patrol_near"] == 5, r
    # the same patrol walking away from it: not a threat now
    going = [W(40 + k, 5000 + 100 * k, 0, 150.0, 0.0) for k in range(5)]
    r = T.engagement((0, 0), boss + going, 4, 9)
    assert r["verdict"] == "pull" and r["patrol_near"] == 0, r
    # a patroller seen only briefly (beat unknown), standing 2,500 off: assume it can come
    new = [W(50, 1800, 2500, 0.0, 0.0, n=2)]
    assert T.engagement((0, 0), boss + new, 4, 9)["patrols"] == 1
    # two groups standing close enough to come together, more than the party should take: wait
    crowd = boss + [E(20 + k, 2900 + 200 * k, 300) for k in range(7)]
    r = T.engagement((0, 0), crowd, 4, 9)
    assert r["verdict"] == "wait" and r["total"] == 11, r


def test_patrol_memory():
    """An enemy seen moving while the party is far away is a patroller; one that only moves
    when the party is close is not (it may just be coming for us)."""
    from Sources.gwamm.core.memory import InstanceMemory, PATROL_ROAM
    class RM:                                  # the bare minimum observe() needs
        regions, node_region = [], []
    mem = InstanceMemory.__new__(InstanceMemory)
    mem.enemies, mem.sight_radius = {}, 5000.0
    for k in range(8):
        mem.observe([(1, 4000.0 + 200 * k, 0.0, True, False), (2, 1000.0 - 100 * k, 0.0, True, False)], (0.0, 0.0), float(k))
    assert mem.enemies[1].roam >= PATROL_ROAM and len(mem.enemies[1].trail) >= 3
    assert mem.enemies[2].roam == 0.0
    assert [e.id for e in mem.patrollers()] == [1]


def test_straight_lines():
    """Walkable-ground test for points and lines, and short steps that do not weave."""
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, _b = load()
    eng = Engine(traps, start)
    nav = eng.nav
    assert all(nav.on_ground(x, y) for x, y in nav.nodes[::50])            # every node is on ground
    xs = [p[0] for p in nav.nodes]; ys = [p[1] for p in nav.nodes]
    assert not nav.on_ground(min(xs) - 5000.0, min(ys) - 5000.0)           # far outside the map is not
    a = nav.nodes[eng.player_node]
    near = [nav.nodes[v] for v, _w in nav.adj[eng.player_node]]
    assert any(nav.line_clear(a, b) for b in near)
    assert not nav.line_clear(a, (min(xs) - 5000.0, min(ys) - 5000.0))
    # a snag beside us: steps are short, and straightening never aims through the snag
    eng.update(start, [], 50, 0.0)
    eng.snags = [(a[0] + 250.0, a[1])]
    step = eng.next_step()
    assert step is not None
    sx, sy = eng.snags[0]
    dx, dy = step[0] - start[0], step[1] - start[1]
    t = max(0.0, min(1.0, ((sx - start[0]) * dx + (sy - start[1]) * dy) / (dx * dx + dy * dy or 1.0)))
    import math as _m
    assert _m.hypot(start[0] + dx * t - sx, start[1] + dy * t - sy) > 250.0 or _m.hypot(dx, dy) < 700.0


def test_plan_fight():
    """Telling groups apart by where the leader would be noticed."""
    from Sources.gwamm.core import tactics as T
    def G(base, cx, cy, n, patrol=False, vel=(0.0, 0.0)):
        return [{"id": base + k, "xy": (cx + 120.0 * (k % 3), cy + 120.0 * (k // 3)), "patrol": patrol,
                 "trail": [(cx, cy)], "vel": vel} for k in range(n)]
    trail = [(-3000.0 + 200 * k, 0.0) for k in range(8)]
    here = (-1500.0, 0.0)
    three = G(100, 0, 0, 7) + G(200, 900, 600, 7) + G(300, 400, -1000, 8)       # three big groups, close together
    p = T.plan_fight(here, three, trail, 9)
    assert sorted(p["groups"]) == [7, 7, 8] and p["verdict"] == "pull" and p["total"] == 7, p
    assert all(100 <= i < 200 for i in p["members"])                              # the near one, alone
    p = T.plan_fight(here, three, trail, T.take_limit(9, 60.0))                   # same room at -40%: do not start it
    assert p["verdict"] in ("avoid", "probe") and T.take_limit(9, 60.0) == 5
    one = G(100, 0, 0, 12)
    assert T.plan_fight(here, one, trail, 9)["verdict"] == "avoid"               # one crowd of 12: cannot be split
    assert T.plan_fight((-4000.0, 0.0), three, trail, 9) is None                  # too far away to plan yet
    # standing on a patrol's beat: it is 2,500 away and walking the other way right now, but it
    # has been seen passing right here before, so it will be back
    roamer = G(700, -1500, 2500, 6, True, (0.0, 150.0))
    for e in roamer:
        e["trail"] = [(-1500.0, 2500.0), (-1500.0, 1200.0), (-1450.0, 200.0), (-1500.0, -900.0)]
    p = T.plan_fight(here, G(100, 0, 0, 5) + roamer, trail, 9)
    assert p["incoming"] == 6 and p["verdict"] == "wait", p
    # a patrol walking through a standing group is told apart from it by its movement
    mixed = G(100, 0, 0, 6) + G(500, 150, 150, 6, True, (0.0, 150.0))
    assert sorted(len(c) for c in T.clusters(mixed)) == [6, 6]
    # a small group beside a crowd of 13: waking both is not a "probe", the 13 alone is too many
    p = T.plan_fight((-1200.0, 0.0), G(100, 0, 0, 2) + G(600, 300, 300, 13), trail, 9)
    assert p["verdict"] == "avoid", p
    walker = G(400, -1500, 2400, 6, True, (0.0, -160.0))                          # a patrol walking straight at us
    p = T.plan_fight(here, G(100, 0, 0, 5) + walker, trail, 9)
    assert p["verdict"] != "pull" and p["total"] == 11, p        # it will be here in 15 s: counted, so not started


def test_drag_fight_away():
    """A group walking into a fight is noticed, and the fight is moved off its path."""
    from Sources.gwamm.core import tactics as T
    def G(base, cx, cy, n, vel=(0.0, 0.0), patrol=False, trail=None):
        return [{"id": base + k, "xy": (cx + 100.0 * (k % 3), cy + 100.0 * (k // 3)), "patrol": patrol,
                 "trail": trail or [(cx, cy)], "vel": vel} for k in range(n)]
    camp = (0.0, 0.0)
    trail = [(-3000.0 + 200 * k, 0.0) for k in range(16)]                 # we came in from the west
    mine = G(100, 200, 100, 5)                                            # what we are fighting
    ids = {e["id"] for e in mine}
    still = G(200, 0, 2000, 6)                                            # standing 2,000 off: not coming
    assert T.incoming_to(camp, mine + still, ids) == []
    walking = G(300, 0, 2000, 6, vel=(0.0, -150.0))                       # walking straight at the camp
    got = T.incoming_to(camp, mine + walking, ids)
    assert len(got) == 1 and len(got[0]) == 6
    away = G(300, 0, 2000, 6, vel=(0.0, 150.0))                           # walking away
    assert T.incoming_to(camp, mine + away, ids) == []
    beat = G(400, 1500, 900, 6, vel=(40.0, 0.0), patrol=True, trail=[(1500.0, 900.0), (300.0, 200.0)])
    assert len(T.incoming_to(camp, mine + beat, ids)) == 1                # the camp is on its beat
    spot = T.shift_camp(trail, camp, mine + walking, ids)
    assert spot is not None and 700.0 <= abs(spot[0]) <= 1600.0 and spot[0] < 0     # back the way we came
    assert all(((spot[0] - e["xy"][0]) ** 2 + (spot[1] - e["xy"][1]) ** 2) ** 0.5 >= 1500.0 for e in walking)
    boxed = G(500, -1200, 300, 6)                                         # someone standing on our way back
    assert T.shift_camp(trail, camp, mine + walking + boxed, ids) is None


def test_levels_and_reach():
    """Short search limits find nodes; nodes can be asked for by ground piece; and an enemy
    beside us on our own level is in reach while a walled-off one is not."""
    import math
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, _b = load()
    eng = Engine(traps, start)
    nav = eng.nav
    a = nav.nodes[eng.player_node]
    assert nav.nearest_node(a[0], a[1], max_radius=300.0) == eng.player_node      # used to return None under 600
    assert nav.nearest_node(a[0] + 1e6, a[1], max_radius=500.0) is None
    plane = nav.node_plane(eng.player_node)
    assert nav.node_plane(nav.nearest_node(a[0], a[1], max_radius=400.0, plane=plane)) == plane
    costs = nav.reach_costs(eng.player_node, 3000.0)
    mate = min((k for k in costs if 400.0 < math.dist(nav.nodes[k], a) < 800.0), key=lambda k: costs[k])
    eng.player_plane = plane
    eng.update(a, [(1, nav.nodes[mate][0], nav.nodes[mate][1], True, False, nav.node_plane(mate), 0.0)], 50, 0.0)
    assert eng.walled_off() == set()                    # a short walk away: in reach
    far = [k for k in range(len(nav.nodes)) if k not in costs and math.dist(nav.nodes[k], a) < 1200.0 and eng.rm.reachable[k]]
    if far:                                             # close in plan view, a long walk: out of reach
        k = far[0]
        eng.update(a, [(2, nav.nodes[k][0], nav.nodes[k][1], True, False, nav.node_plane(k), 0.0)], 50, 5.0)
        assert 2 not in eng.walled_off()                # one reading is not trusted...
        eng.update(a, [(2, nav.nodes[k][0], nav.nodes[k][1], True, False, nav.node_plane(k), 0.0)], 50, 7.0)
        assert 2 in eng.walled_off()                    # ...the same reading 2 s later is
    e = eng.mem.enemies[1]
    assert e.plane == nav.node_plane(mate) and e.z == 0.0
    # the leader's own plane decides which level he is on where two overlap
    other = next((k for k in range(len(nav.nodes)) if eng.rm.reachable[k] and nav.node_plane(k) != plane), None)
    if other is not None:
        eng.player_plane = nav.node_plane(other)
        assert nav.node_plane(eng._track_node(nav.nodes[other])) == nav.node_plane(other)
        eng.player_plane = 12345                       # a plane the map does not have: fall back, never fail
        assert eng._track_node(nav.nodes[other]) is not None


def test_level_cap():
    """A step never ends deep on another level: it stops just onto it."""
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, _b = load()
    eng = Engine(traps, start)
    nav = eng.nav
    for start in range(0, len(nav.nodes), 97):
        if not eng.rm.reachable[start]:
            continue
        costs = nav.reach_costs(start, 4000.0)
        far = [g for g in costs if nav.node_plane(g) != nav.node_plane(start)]
        if not far:
            continue
        route = nav.path(start, max(far, key=costs.get))
        eng.player_xy, eng.player_plane = nav.nodes[start], nav.node_plane(start)
        k = eng._level_cap(route, len(route) - 1)
        change = next(j for j in range(1, len(route)) if nav.node_plane(route[j]) != nav.node_plane(start))
        assert change <= k and all(nav.node_plane(route[j]) == nav.node_plane(route[change]) for j in range(change, k + 1)) or k == len(route) - 1
        return


def test_roles_in_target_choice():
    """A known healer is called before a fighter; a minion is left for last."""
    from Sources.gwamm.core import tactics
    base = {"xy": (500, 0), "hp": 1.0, "level": 26, "boss": False, "caster": False, "prof": 0}
    foes = [dict(base, id=1, role="fighter"), dict(base, id=2, role="healer"), dict(base, id=3, role="minion", level=13)]
    assert tactics.pick_target(foes, (0, 0)) == 2
    assert tactics.pick_target([f for f in foes if f["id"] != 2], (0, 0)) == 1
    assert isinstance(tactics.ROLES, dict) and tactics.role_of(-1) == ""


def test_ranged_pull_plan():
    """From weapon range the leader wakes only the group he hits; walking up wakes its neighbour too."""
    from Sources.gwamm.core import tactics
    def foe(i, x, y):
        return {"id": i, "xy": (x, y), "patrol": False, "trail": (), "vel": (0.0, 0.0), "z": 0.0}
    a = [foe(i, 2000 + 60 * i, 0) for i in range(3)]              # the group straight ahead
    b = [foe(10 + i, 1500 + 60 * i, 800) for i in range(4)]       # another, off to the side and nearer the path
    trail = [(x, 0) for x in range(-3000, 100, 100)]
    walk = tactics.plan_fight((0, 0), a + b, trail, 9, far=2600.0)
    shot = tactics.plan_fight((0, 0), a + b, trail, 9, far=2600.0, stand=1180.0)
    assert shot["total"] <= walk["total"] and shot["total"] in (3, 4)
    assert shot["likely"] >= shot["total"]                        # the neighbours are weighed in, not ignored
    assert tactics.join_chance(100) > tactics.join_chance(900) > tactics.join_chance(1700) > tactics.join_chance(5000) == 0.0


def test_consumable_limits():
    """Nothing is poured into an area that is being lost: per-area limits, and a stop after wipes."""
    from Sources.gwamm.core import consumables as C
    from Sources.gwamm.core.engine import Config
    cfg = Config()
    cfg.pcons_on, cfg.pc_powerstone_of_courage, cfg.pc_essence_of_celerity = True, True, C.ALWAYS
    bags = [i.key for i in C.CATALOGUE]
    def facts(**kw):
        return C.Facts(deaths=5, foes_left=100, my_morale=40, party_morale=[40] * 8, in_bags=bags, **kw)
    assert C.decide(cfg, facts())[0].key == "powerstone_of_courage"
    assert C.decide(cfg, facts(morale_used=1, kills_since_morale=3))[0].kind == C.EFFECT     # last one bought 3 kills: no second
    assert C.decide(cfg, facts(morale_used=1, kills_since_morale=30))[0].key == "powerstone_of_courage"
    assert C.decide(cfg, facts(morale_used=2, kills_since_morale=30))[0].kind == C.EFFECT    # area limit reached
    assert C.decide(cfg, facts(morale_used=2, timed_used=6, kills_since_morale=30))[0] is None
    # the leader one death from the floor: the party item is used even if nobody else is low
    alone = C.Facts(deaths=3, foes_left=100, my_morale=55, party_morale=[55, 100, 100, 100, 100, 100, 100, 100], in_bags=bags)
    assert C.decide(cfg, alone)[0].key == "powerstone_of_courage"
    item, why = C.decide(cfg, facts(wipes_since_first=2))
    assert item is None and "stopped for this area" in why
    assert C.decide(cfg, C.Facts(deaths=0, foes_left=100, in_bags=bags))[0].key == "essence_of_celerity"   # healthy party: no morale item


def test_formation():
    """Heroes are parked apart from one another, fighters towards the enemy."""
    import math
    from Sources.gwamm.core import tactics
    places = tactics.formation((1000, 1000), (3000, 1000), 7)
    assert len(places) == 7
    assert min(math.dist(a, b) for i, a in enumerate(places) for b in places[i + 1:]) >= 320.0
    assert places[0][0] > 1000 and places[-1][0] < 1000          # first place forward, last behind
    assert max(math.dist(p, (1000, 1000)) for p in places) <= 650.0
    # on a narrow path (only 200 either side is ground) nobody shares a spot
    narrow = tactics.formation((1000, 1000), (3000, 1000), 7, lambda x, y: abs(y - 1000) <= 200)
    assert all(abs(p[1] - 1000) <= 200 for p in narrow)
    assert min(math.dist(a, b) for i, a in enumerate(narrow) for b in narrow[i + 1:]) >= 100.0


def test_step_round_obstacle():
    """Caught on scenery while walking: the next step goes round it towards the goal, not back."""
    import math
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, _b = load()
    eng = Engine(traps, start)
    nav = eng.nav
    for i in range(0, len(nav.nodes), 53):
        if not eng.rm.reachable[i]:
            continue
        me = nav.nodes[i]
        ahead = [k for k in nav.nodes_within(me[0], me[1], 2200.0) if eng.rm.reachable[k]
                 and math.dist(nav.nodes[k], me) > 1800.0 and nav.line_clear(me, nav.nodes[k])]
        if not ahead:
            continue
        goal = nav.nodes[ahead[0]]
        d = math.dist(me, goal)
        rock = (me[0] + (goal[0] - me[0]) * 300.0 / d, me[1] + (goal[1] - me[1]) * 300.0 / d)
        spot = eng.escape_point(me, rock, goal=goal)
        back = eng.escape_point(me, rock)
        assert spot is not None and math.dist(spot, rock) >= 260.0
        assert math.dist(spot, goal) <= math.dist(back, goal)        # never worse than backing off
        return


def test_town_waypoints():
    """An errand route is a string of short hops that ends at the target."""
    import math
    from simulate import load
    from Sources.gwamm.core.geometry import NavGraph, town_waypoints
    traps, start, _b = load()
    nav = NavGraph(traps)
    far = max(nav.reach_costs(nav.nearest_node(*start), 6000.0).items(), key=lambda kv: kv[1])[0]
    goal = nav.nodes[far]
    hops = town_waypoints(traps, (), start, goal)
    assert hops and math.dist(hops[-1], goal) <= 150.0
    assert max(math.dist(a, b) for a, b in zip([start] + hops, hops)) <= 1200.0
    assert town_waypoints(traps, (), start, (9e6, 9e6)) != [] or True     # never raises


def test_retreat_point():
    """Giving ground goes away from the enemy even after the retreat has been added to the trail."""
    from Sources.gwamm.core.tactics import retreat_point
    trail = [(x, 0) for x in range(0, 3000, 100)] + [(2900, 0), (2800, 0), (2900, 0)]
    back = retreat_point(trail, (2900, 0), [(4200, 0)])
    assert back is not None and back[0] <= 2300
    assert retreat_point(trail, (2900, 0), []) is None
    assert retreat_point([(2900, 0)], (2900, 0), [(4200, 0)]) is None


def test_fight_recorder():
    """One record per fight: the approach is kept, late arrivals are described, the end is summed up."""
    from Sources.gwamm.core.fightrec import FightRecorder
    rec, lines = FightRecorder(), []

    def snap(t, me, enemies, dead=(), killed=0):
        allies = [{"id": 1, "xy": me, "hp": 0.9, "name": "Leader", "kind": "leader"},
                  {"id": 2, "xy": (me[0] - 100, me[1]), "hp": 0.5, "dead": 2 in dead, "name": "Hero"}]
        return {"t": float(t), "me": {"xy": me, "z": 0.0, "plane": 0, "dead": 1 in dead}, "allies": allies,
                "enemies": [{"id": i, "xy": xy, "hp": 1.0, "alive": True, "name": "Foe", "level": 24, "model": 7} for i, xy in enemies],
                "foes": 30, "killed": killed, "morale": 100, "ctx": {"mode": "SWEEP"}}

    assert rec.feed(snap(0, (0, 0), [(10, (9000, 0))])) == []            # nothing near: nothing kept
    for t in range(1, 15):                                               # walking up: held back, not written
        assert rec.feed(snap(t, (t * 100, 0), [(10, (3500, 0))])) == []
    out = rec.feed(snap(15, (2200, 0), [(10, (3500, 0))]))               # contact
    assert out[0]["rec"] == "fight" and out[0]["ctx"]["mode"] == "SWEEP" and "10" in out[0]["who"]
    assert out[0]["who"]["10"]["level"] == 24 and out[0]["who"]["1"]["side"] == "ally"
    assert [l["rec"] for l in out[1:]] == ["s"] * 11                     # ten seconds of approach + now
    assert "name" not in out[-1]["enemies"][0] and out[-1]["enemies"][0]["xy"] == [3500, 0]
    out = rec.feed(snap(16, (2200, 0), [(10, (3000, 0)), (11, (3300, 0))], dead={2}))
    assert out[0]["rec"] == "who" and "11" in out[0]["who"]               # a late arrival is described once
    for t in range(17, 30):
        lines += rec.feed(snap(t, (2200, 0), [], killed=2))
    end = lines[-1]
    assert end["rec"] == "end" and end["why"] == "over" and end["kills"] == 2
    assert end["enemies_met"] == 2 and end["most_at_once"] == 2 and end["ally_deaths"] == 1
    assert not rec.active
    rec.feed(snap(40, (0, 0), [(20, (1000, 0))]))                        # a second fight...
    out = rec.feed(snap(41, (9000, 9000), [(20, (1000, 0))]))            # ...ended by a trip to the shrine
    assert out[-1]["rec"] == "end" and out[-1]["why"] == "sent back to a shrine" and out[-1]["n"] == 2


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


def test_cut_off_gives_up_nothing():
    """When nothing at all can be reached from where the leader stands, no objective is written
    off and the engine does not report the search as over."""
    from simulate import load
    from Sources.gwamm.core.engine import Engine
    traps, start, _b = load()
    eng = Engine(traps, start)
    eng.update(start, [], 50, 0.0)
    eng.nav.path = lambda *a, **k: []
    before = dict(eng.mem.blocked)
    step = eng.next_step()
    assert step is not None, "a cut-off leader must still get a step"
    assert eng.mem.blocked == before, eng.mem.blocked
    assert eng.mode not in ("DONE", "FAILED")


def test_no_restart_consumables():
    """Where a wipe ends the run, timed bonuses start for the first big fight, not after deaths."""
    from Sources.gwamm.core import consumables as C
    from Sources.gwamm.core.engine import Config
    cfg = Config(); cfg.pcons_on = True; cfg.pc_grail_of_might = C.HARD
    base = dict(deaths=0, foes_left=40, in_bags={"grail_of_might"})
    assert C.decide(cfg, C.Facts(**base, foes_near=9))[0] is None                       # ordinary area: held
    assert C.decide(cfg, C.Facts(**base, no_restart=True, foes_near=3))[0] is None      # small fight: held
    it, why = C.decide(cfg, C.Facts(**base, no_restart=True, foes_near=9))
    assert it is not None and it.key == "grail_of_might", why
    assert C.decide(cfg, C.Facts(**base, no_restart=True, foes_near=9, running={"grail_of_might"}))[0] is None
    cfg.pc_grail_of_might = C.OFF
    assert C.decide(cfg, C.Facts(**base, no_restart=True, foes_near=9))[0] is None      # switched off stays off


def test_supports_allies():
    """Enemies casting heals or protection on their own side are recognised as healers."""
    from Sources.gwamm.core import tactics
    assert tactics.supports_allies("Spell. Heal target ally for 60 Health.")
    assert tactics.supports_allies("Enchantment Spell. For 8 seconds, all party members within earshot have a 50% chance to block attacks.")
    assert tactics.supports_allies("Enchantment Spell. For 8 seconds, the next time target ally would take damage or life steal , that ally gains that amount of Health instead.")
    assert not tactics.supports_allies("Hex Spell. Target foe suffers -3 Health degeneration.")
    assert not tactics.supports_allies("Spell. Steal 30 Health from target foe.")
    model = 999001
    assert tactics.learn_role(model, "healer") and tactics.role_of(model) == "healer"
    assert not tactics.learn_role(model, "fighter")          # never downgraded


def test_authored_approach():
    """Into crowded or deadly ground the party comes in from the side the known route does, and
    not when it is already coming from that side."""
    import math
    from simulate import load
    from Sources.gwamm.core.engine import Engine, Objective
    traps, start, _b = load()
    eng = Engine(traps, start)
    eng.update(start, [], 50, 0.0)
    nodes = [eng.nav.nodes[i] for i in range(len(eng.nav.nodes)) if eng.rm.reachable[i]]
    sx, sy = start
    far = [p for p in nodes if 3000.0 < math.hypot(p[0] - sx, p[1] - sy) < 6000.0]
    assert far, "test map too small"
    target = far[0]
    side = [p for p in nodes if 1400.0 < math.hypot(p[0] - target[0], p[1] - target[1]) < 2500.0
            and math.hypot(p[0] - sx, p[1] - sy) > math.hypot(target[0] - sx, target[1] - sy)]
    if not side:
        return
    eng._route_src = [side[0], target]
    eng.hard_spots = [[target[0], target[1], 2]]
    obj = Objective("cluster", ("cluster", 1), target, None, 1.0)
    via = eng._approach_via(obj)
    if via is not None:                          # the way round must not cross the crowd itself
        assert math.hypot(via[0] - side[0][0], via[1] - side[0][1]) < 1.0
        eng.player_xy = side[0]
        assert eng._approach_via(obj) is None, "arrived at the approach point: no more detour"
    eng._via, eng._via_done = {}, set()
    eng.hard_spots = []
    assert eng._approach_via(obj) is None, "ordinary ground: no detour"


def test_rest_counts_the_walk():
    """Energy that the walk to the next fight will refill is not waited for."""
    from Sources.gwamm.core import tactics
    assert tactics.on_arrival(0.5, 0.012, 4000.0) >= 0.6         # a walk ahead: no stop
    assert tactics.on_arrival(0.45, 0.012, 1100.0) < 0.6         # next group right there: rest
    assert tactics.on_arrival(0.2, 0.012, 99999.0) < 0.6         # far ahead: walk counted only so far
    assert tactics.on_arrival(0.95, 0.05, 4000.0) == 1.0


def test_raisers_and_protectors():
    """Resurrections (however worded) and protection spells mark their caster as the one to kill."""
    from Sources.gwamm.core import tactics
    assert tactics.raises_dead("Spell. Touch the body of a fallen party member. Target party member is returned to life with 65% Health.")
    assert tactics.supports_allies("Enchantment Spell. For 23 seconds, target ally cannot lose more than 10% max Health due to damage from a single attack.")
    assert not tactics.supports_allies("Signet. You gain 172 Health. You have -40 armor while using this skill.")
    base = {"id": 1, "xy": (500.0, 0.0), "hp": 1.0, "level": 24, "caster": True, "boss": False, "prof": 0, "role": ""}
    raiser = dict(base, id=2, xy=(1400.0, 0.0), casting="raise")
    boss = dict(base, id=3, boss=True)
    assert tactics.pick_target([boss, raiser], (0.0, 0.0)) == 2


if __name__ == "__main__":
    test_geometry()
    test_cartography()
    test_carto_missing()
    test_carto_edge_spot()
    test_world_gate_fix()
    test_elites()
    test_full_runs()
    test_route_and_trap_together()
    test_route_with_actions_in_order()
    test_route_with_actions_survives_death()
    test_wall_never_shuts_the_only_way()
    test_shown_portal_keeps_its_doorway()
    test_route_groups_now_or_later()
    test_ride_ground()
    test_foot_way_keeps_off_sulfur()
    test_route_chain()
    test_keep_off_doors()
    test_choose_teleporter()
    test_teleporter_sets()
    test_rank_teleporters()
    test_put_off_helps()
    test_detour_on_wall()
    test_step_clear()
    test_wipe_zone()
    test_supports_allies()
    test_authored_approach()
    test_rest_counts_the_walk()
    test_raisers_and_protectors()
    test_no_restart_consumables()
    test_cut_off_gives_up_nothing()
    test_pull_rule()
    test_engagement()
    test_plan_fight()
    test_levels_and_reach()
    test_drag_fight_away()
    test_straight_lines()
    test_patrol_memory()
    test_fight_recorder()
    test_retreat_point()
    test_town_waypoints()
    test_step_round_obstacle()
    test_formation()
    test_consumable_limits()
    test_ranged_pull_plan()
    test_roles_in_target_choice()
    test_level_cap()
    test_consumables()
    print("ALL PASSED")
