"""Offline simulation: the engine clears Snake Dance against made-up enemies.
Run from the folder that contains Sources/:  python tests/simulate.py"""
import json, math, os, random, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Sources.gwamm.core.engine import Engine, Config, DONE, FAILED
from Sources.gwamm.core.cartography import Projection, CartoGrid

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    d = json.load(open(os.path.join(HERE, "snake_dance.json")))
    return [(p, *t) for p, plane in enumerate(d["trapezoids"]) for t in plane], tuple(d["player_xy"]), d["bounds"]


def exits():
    """Stand-ins for the area exits: the snapshot's arrival points, which sit beside the portals."""
    d = json.load(open(os.path.join(HERE, "snake_dance.json")))
    return [(x, y) for x, y, _tag in d["spawns"][0] + d["spawns"][1]]


def run(seed=1, groups=60, hidden_share=0.3, patrol_share=0.15, carto=True, verbose=False, cfg=None, guide=None, near_route=None, hints=None):
    rnd = random.Random(seed)
    traps, start, bounds = load()
    proj = Projection(1000.0, 2000.0, bounds[0], bounds[3]) if carto else None
    eng = Engine(traps, start, cfg or Config(), proj, exits(), guide=guide, hints=hints)
    closest_exit = math.inf
    nav, rm = eng.nav, eng.rm
    reach = [i for i, ok in enumerate(rm.reachable) if ok]
    enemies, eid = [], 0
    for _ in range(groups):
        pool = reach
        if near_route:                       # enemies live where the known route goes, as in the real areas
            pool = [i for i in reach if any(math.dist(nav.nodes[i], p) < 1500 for p in near_route)] or reach
        home = rnd.choice(pool)
        while math.dist(nav.nodes[home], start) < 2500:
            home = rnd.choice(pool)
        hidden, patrol = rnd.random() < hidden_share, rnd.random() < patrol_share
        for _ in range(rnd.randint(3, 7)):
            x, y = nav.nodes[home]
            enemies.append({"id": eid, "node": home, "xy": (x + rnd.uniform(-250, 250), y + rnd.uniform(-250, 250)),
                            "alive": True, "hidden": hidden, "patrol": patrol, "boss": False})
            eid += 1
    explored = set()
    pos, t, walked, steps = start, 0.0, 0.0, 0
    target, dist_to = None, None
    here = nav.nearest_node(pos[0], pos[1], allowed=rm.reachable)
    vq_walk, stall, last_pos = None, 0, None
    while steps < 60000:
        steps += 1
        t += 1.0
        for e in enemies:                              # patrols drift along the graph
            if e["alive"] and e["patrol"] and steps % 4 == 0:
                e["node"] = rnd.choice(nav.adj[e["node"]])[0]
                e["xy"] = nav.nodes[e["node"]]
        seen = []
        for e in enemies:
            d = math.dist(e["xy"], pos)
            if e["hidden"] and e["alive"] and d < 1200:
                e["hidden"] = False                    # pop-up triggered
            if not e["hidden"] and d < 4800:
                seen.append((e["id"], e["xy"][0], e["xy"][1], e["alive"], e["boss"]))
        if carto:
            cx, cy = proj.cell(*pos)
            explored |= {(cx + dx, cy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
            words = [0] * 4096
            for (ax, ay) in explored:
                if 0 <= ax < 256 and 0 <= ay < 512:
                    words[ay * 8 + (ax >> 5)] |= 1 << (ax & 31)
            grid = CartoGrid(256, 512, words)
        else:
            grid = None
        remaining = sum(1 for e in enemies if e["alive"] and not e["hidden"])   # pop-ups only count once triggered
        if remaining == 0 and vq_walk is None:
            vq_walk = round(walked)
        eng.update(pos, seen, remaining, t, grid if steps % 5 == 0 or steps == 1 else None)
        fighting = [e for e in enemies if e["alive"] and not e["hidden"] and math.dist(e["xy"], pos) < 900]     # the party only fights what is this close
        if fighting:                                   # a fight: one kill every 3 ticks
            if steps % 3 == 0:
                fighting[0]["alive"] = False
            continue
        step = eng.next_step()
        if step is None:
            break
        goal = nav.nearest_node(step[0], step[1], allowed=rm.reachable)
        route = nav.path(here, goal)
        budget, k = 290.0, 0
        while budget > 0 and k + 1 < len(route):
            w = math.dist(nav.nodes[route[k]], nav.nodes[route[k + 1]])
            budget -= w
            walked += w
            k += 1
        here = route[k] if route else here
        pos = nav.nodes[here]
        if here == goal and math.dist(pos, step[:2]) < 400:
            pos = (step[0], step[1])          # the last few steps off the graph, onto the exact spot
        closest_exit = min(closest_exit, min(math.dist(pos, e) for e in eng.exits))
        stall = stall + 1 if pos == last_pos else 0
        last_pos = pos
        if stall > 50:
            o = eng.objective
            print('STALL', o.label(), o.pos, 'step', step, 'at', pos, eng.status())
            break
        if verbose and steps % 500 == 0:
            print(steps, eng.status())
    st = eng.status()
    return {"seed": seed, "mode": st["mode"], "ticks": steps, "walked": round(walked), "closest_exit": round(closest_exit), "vq_walk": vq_walk,
            "enemies": len(enemies), "left": remaining, "untriggered": sum(1 for e in enemies if e["hidden"]),
            "regions": st["regions"], "searched": st["searched"], "fog": st["fog_cells"],
            "escalation": st["escalation"], "blocked": len(st["blocked"])}


if __name__ == "__main__":
    t = time.time()
    for seed in range(1, 6):
        print(run(seed))
    print("no carto:", run(1, carto=False))
    print("seconds", round(time.time() - t, 1))
