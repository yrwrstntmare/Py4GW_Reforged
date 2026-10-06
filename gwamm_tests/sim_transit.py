"""Offline check of crossing an area: the party should go round groups or fight them one at a time."""
import sys, os, math, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from simulate import load
from Sources.gwamm.core.engine import Engine, Config, DONE

traps, start, bounds = load()
goal = (-5525.0, -27146.0)                 # where the live run died
eng = Engine(traps, start, Config(), None, [], goto=goal)
nav = eng.nav
a = nav.nearest_node(*start, allowed=eng.rm.reachable); b = nav.nearest_node(*goal, allowed=eng.rm.reachable)
direct = nav.path(a, b)
# three groups of five standing on the direct path
enemies, eid = [], 0
for frac in (0.3, 0.55, 0.8):
    x, y = nav.nodes[direct[int(len(direct) * frac)]]
    for k in range(5):
        eid += 1
        enemies.append({"id": eid, "xy": (x + 120 * k, y + 90 * k), "alive": True})
pos, t, walked, fights, most, slow = start, 0.0, 0.0, 0, 0, 0.0
for tick in range(4000):
    t += 1.0
    seen = [(e["id"], e["xy"][0], e["xy"][1], True, False) for e in enemies if e["alive"] and math.dist(e["xy"], pos) < 4200]
    c0 = time.time()
    eng.update(pos, seen, None, t)
    step = eng.next_step(); slow = max(slow, time.time() - c0)
    close = [e for e in enemies if e["alive"] and math.dist(e["xy"], pos) < 1000]
    most = max(most, len({(e["id"] - 1) // 5 for e in close}))
    if close:
        if tick % 3 == 0:
            close[0]["alive"] = False; fights += 1
        continue
    if step is None:
        break
    d = math.dist(step[:2], pos)
    if d > 1:
        m = min(d, 300.0); pos = (pos[0] + (step[0] - pos[0]) * m / d, pos[1] + (step[1] - pos[1]) * m / d); walked += m
print({"mode": eng.mode, "ticks": tick, "walked": round(walked), "direct": round(sum(math.dist(nav.nodes[i], nav.nodes[j]) for i, j in zip(direct, direct[1:]))),
       "kills": fights, "most_groups_at_once": most, "left": round(math.dist(pos, goal)), "slowest_plan_s": round(slow, 3)})
assert eng.mode == DONE and math.dist(pos, goal) < 400 and most <= 1
print("TRANSIT OK")
