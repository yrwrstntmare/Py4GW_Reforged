"""A small fight simulator: groups, patrols, walls, and a party that can be told how to behave.

It exists to compare ways of starting a fight across many situations, cheaply and repeatably,
before trying them in the game. It is a model, not the game: the numbers below (how far enemies
notice, how far they chase, how much a party can take) are stated assumptions. What it is good
for is ranking behaviours against each other and showing which situations break which rule.

Run:  python gwamm_tests/sim_combat.py            (table of results)
      python gwamm_tests/sim_combat.py -v         (plus one line per run)

Assumptions (all in units and seconds)
  AGGRO 1000      an idle group notices a party member this close; the WHOLE group then comes
  LEASH 3000      a group that has chased this far from where it was standing gives up, walks home
  speeds          party 290, chasing enemy 300, patrol on its beat 140
  the party       can out-heal about 5-6 enemies; kills one enemy every 4 s; 100 "health"
  the leader      alone (heroes parked elsewhere) has 30 "health" and no healing
  walls           an enemy flagged `walled` cannot notice or be reached except by a long walk
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Sources.gwamm.core import tactics as T                      # noqa: E402
from Sources.gwamm.core.memory import InstanceMemory, is_patrol   # noqa: E402

AGGRO, LEASH, DT = 1000.0, 3000.0, 0.5
V_PARTY, V_CHASE, V_BEAT = 290.0, 300.0, 140.0
KILL_EVERY, HEAL, HURT = 4.0, 9.0, 1.6
REACH = 900.0                    # an enemy this close to the party is hitting it
SHIFT = True                     # "pull next" drags a fight away from a group walking into it


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def toward(a, b, step):
    d = dist(a, b)
    return b if d <= step or d == 0 else (a[0] + (b[0] - a[0]) * step / d, a[1] + (b[1] - a[1]) * step / d)


class Group:
    """Enemies that notice together. `beat`: waypoints walked in a loop (a patrol), or None."""
    def __init__(self, gid, centre, n, rng, beat=None, walled=False, spread=220.0, start=0.0):
        self.id, self.beat, self.walled = gid, beat, walled
        self.offsets = [(rng.uniform(-spread, spread), rng.uniform(-spread, spread)) for _ in range(n)]
        self.centre, self.leg, self.mode = centre, 0, "idle"       # idle | chase | home
        self.alive = [True] * n
        self.pos = [(centre[0] + ox, centre[1] + oy) for ox, oy in self.offsets]
        self.origin = centre
        for _ in range(int(start / DT)):
            self.walk()

    def ids(self):
        return [self.id * 100 + k for k in range(len(self.pos)) if self.alive[k]]

    def left(self):
        return sum(self.alive)

    def walk(self):
        if self.beat:
            goal = self.beat[self.leg % len(self.beat)]
            self.centre = toward(self.centre, goal, V_BEAT * DT)
            if dist(self.centre, goal) < 1.0:
                self.leg += 1
        self.pos = [(self.centre[0] + ox, self.centre[1] + oy) for ox, oy in self.offsets]


class World:
    def __init__(self, groups, targets, start=(-6000.0, 0.0), morale=1.0):
        self.groups, self.targets = groups, set(targets)         # targets: group ids that must die
        self.leader = self.party = start                          # party = where the heroes are
        self.flag = None                                          # heroes parked here, if set
        self.hp, self.leader_hp, self.t = 100.0 * morale, 30.0, 0.0
        self.morale, self.kill_clock = morale, 0.0
        self.trail = [start]
        self.max_on = 0
        self.result = None
        self.mem = InstanceMemory.__new__(InstanceMemory)         # the bot's own enemy memory
        self.mem.enemies, self.mem.sight_radius = {}, 5000.0

    def enemies(self, only=None):
        out = []
        for g in self.groups:
            for k, p in enumerate(g.pos):
                if g.alive[k] and (only is None or g.mode == only):
                    out.append((g, k, p))
        return out

    def view(self):
        """What the bot knows: positions, and what its own memory has worked out about movement."""
        seen = [(g.id * 100 + k, p[0], p[1], True, False) for g, k, p in self.enemies()
                if dist(p, self.leader) < getattr(self, "sight", 5000.0) and not g.walled]
        self.mem.observe(seen, self.leader, self.t)
        return [{"id": e.id, "xy": e.xy, "patrol": is_patrol(e), "trail": e.trail, "vel": e.vel}
                for e in self.mem.enemies.values() if e.alive and e.in_range]

    def step(self, leader_goal, flag, attack_reach=None):
        """Advance half a second. `flag`: park the heroes at this point (or None: follow).
        `attack_reach`: the party also attacks idle enemies this close (the old fight routine)."""
        self.t += DT
        self.leader = toward(self.leader, leader_goal, V_PARTY * DT)
        self.flag = flag
        self.party = toward(self.party, flag if flag is not None else self.leader, V_PARTY * DT)
        if dist(self.trail[-1], self.leader) > 200.0:
            self.trail.append(self.leader)
        for g in self.groups:
            if not g.left():
                continue
            near = min(min(dist(p, self.leader), dist(p, self.party)) for k, p in enumerate(g.pos) if g.alive[k])
            if g.mode == "idle":
                g.walk()
                woken = near < AGGRO and not g.walled
                if attack_reach and near < attack_reach:
                    woken = True                                  # attacked: walls do not stop a spell
                if woken:
                    g.mode, g.origin = "chase", g.centre
            elif g.mode == "chase":
                who = self.leader if dist(g.centre, self.leader) < dist(g.centre, self.party) else self.party
                if dist(g.centre, g.origin) > LEASH and dist(g.centre, who) > REACH:
                    g.mode = "home"
                else:
                    g.centre = toward(g.centre, who, V_CHASE * DT) if dist(g.centre, who) > 250.0 else g.centre
                    g.pos = [(g.centre[0] + ox * 0.5, g.centre[1] + oy * 0.5) for ox, oy in g.offsets]
            else:
                g.centre = toward(g.centre, g.origin, V_BEAT * DT)
                g.walk() if dist(g.centre, g.origin) < 1.0 else None
                if dist(g.centre, g.origin) < 1.0:
                    g.mode = "idle"
        apart = dist(self.leader, self.party) > 700.0
        on_party = [(g, k) for g, k, p in self.enemies("chase") if dist(p, self.party) < REACH]
        on_leader = [(g, k) for g, k, p in self.enemies("chase") if dist(p, self.leader) < REACH] if apart else []
        self.max_on = max(self.max_on, len(on_party) + len(on_leader))
        if on_party:
            self.hp += (HEAL * self.morale - HURT * len(on_party)) * DT
            self.kill_clock += DT
            if self.kill_clock >= KILL_EVERY / self.morale:
                self.kill_clock = 0.0
                g, k = on_party[0]
                g.alive[k] = False
        else:
            self.hp = min(100.0 * self.morale, self.hp + HEAL * DT)
        if on_leader:
            self.leader_hp -= HURT * len(on_leader) * DT
        elif not apart:
            self.leader_hp = 30.0
        if self.hp <= 0 or self.leader_hp <= 0:
            self.result = "wipe"
        elif all(not g.left() for g in self.groups if g.id in self.targets):
            self.result = "win"
        return self.result


# ---------------------------------------------------------------- behaviours
def target_point(w):
    pts = [p for g, k, p in w.enemies() if g.id in w.targets]
    return min(pts, key=lambda p: dist(p, w.leader)) if pts else w.leader


def walk_in(w, st):
    """The old way: the whole party walks at the nearest target and fights whatever is in reach."""
    return target_point(w), None, 1200.0


def pull_v36(w, st):
    """The rule as shipped in 0.36: forecast, wait or pull, camp where the party stands."""
    return _pull(w, st, smart=False)


def pull_next(w, st):
    """The proposed rule: as above, plus a camp chosen off every known beat, a probe pull for a
    crowd that may be two groups, and backing right out when more comes than was bargained for."""
    return _pull(w, st, smart=True)


def _pull(w, st, smart):
    view = w.view()
    phase = st.setdefault("phase", None)
    here = w.leader
    chasing = [p for g, k, p in w.enemies("chase")]
    if phase == "bail":
        # more came than the party can take: everyone back along the way we came, out of their leash
        goal = st["bail_to"]
        if not chasing and dist(here, goal) < 300.0 or w.t - st["t"] > 40.0:
            st.update(phase=None, cool=w.t + 4.0, bailed=st.get("bailed", 0) + 1)
        return goal, None, None
    if phase == "shift":
        if dist(here, st["camp"]) < 200.0 or w.t - st["t3"] > 7.0:
            st.update(phase="hold")
        return st["camp"], st["camp"], None
    if phase == "hold":
        on = [p for p in chasing if dist(p, st["camp"]) < 1300.0]
        st["came"] = max(st.get("came", 0), len(chasing))
        if smart and SHIFT and chasing and w.t - st.get("shift_at", -99.0) > 8.0 and st.get("shifts", 0) < 3:
            mine = {g.id * 100 + k for g, k, p in w.enemies("chase")}
            if T.incoming_to(st["camp"], view, mine):
                st["shift_at"] = w.t
                spot = T.shift_camp(w.trail, st["camp"], view, mine)
                if spot is not None:
                    st.update(phase="shift", camp=spot, t3=w.t, shifts=st.get("shifts", 0) + 1)
                    return spot, spot, None
        if not chasing and w.t - st["t"] > 6.0:
            st.update(phase=None, cool=w.t + 2.0)
        return st["camp"], st["camp"], None
    if phase == "go":
        if chasing or w.t - st["t"] > 10.0:
            st.update(phase="back", t=w.t)
        return st["tag"], st["camp"], None
    if phase == "back":
        if dist(here, st["camp"]) < 200.0 or w.t - st["t"] > 12.0:
            st.update(phase="hold", t=w.t)
        return st["camp"], st["camp"], None
    if w.t < st.get("cool", 0.0):
        return here, None, None

    limit = 9
    if smart:
        # a weakened party takes on less: the limit shrinks with death penalty
        return _plan_next(w, st, view, T.take_limit(limit, 100.0 * w.morale), chasing)
    plan = T.engagement(here, view, 4, limit)
    if plan is None:
        if chasing:                                    # jumped on the way: stand and fight here
            st.update(phase="hold", camp=here, t=w.t, limit=limit, came=0)
            return here, here, None
        return target_point(w), None, None             # not close enough to decide yet: approach
    if plan["verdict"] == "fight":
        return plan["target_xy"], None, None
    waited = w.t - st.setdefault("w0", w.t)
    if plan["verdict"] == "wait" and waited < 40.0:
        back = T.fallback_point(w.trail, here, [v["xy"] for v in view], 600.0, clear=1300.0)
        return (back if back is not None and plan["distance"] < 1500.0 else here), None, None
    st.pop("w0", None)
    everyone = [v["xy"] for v in view]
    camp = here
    if T.fight_forecast(here, plan["target_xy"], everyone)[1]:
        camp = T.fallback_point(w.trail, here, everyone, 900.0, clear=1500.0) or here
    st.update(phase="go", camp=camp, tag=plan["target_xy"], t=w.t, limit=limit, came=0)
    return plan["target_xy"], camp, None


def _plan_next(w, st, view, limit, chasing):
    here = w.leader
    if chasing:                                        # something is already on us: stand here
        st.update(phase="hold", camp=here, t=w.t, limit=limit, came=0)
        return here, here, None
    plan = T.plan_fight(here, view, w.trail, limit)
    if plan is None:
        return target_point(w), None, None             # nothing close enough to plan for: approach
    if plan["incoming"]:
        # a patrol we do not mean to fight yet is walking at us: give ground, keep watching
        back = T.fallback_point(w.trail, here, [], 900.0, clear=0.0)
        return (back or here), None, None
    # look before starting anything: a room is watched for a few seconds so patrols show themselves
    if w.t - st.setdefault("seen_at", w.t) < 8.0:
        return here, None, None
    waited = w.t - st.setdefault("w0", w.t)
    if plan["verdict"] == "wait" and waited < 45.0:
        return here, None, None
    if plan["verdict"] == "probe" and waited < 12.0:
        return here, None, None
    if plan["verdict"] == "avoid":
        st["avoided"] = True                           # too big to take and cannot be split: keep clear of it
        back = T.fallback_point(w.trail, here, [v["xy"] for v in view], 700.0, clear=1800.0)
        return (back if back is not None and plan["distance"] < 1800.0 else here), None, None
    st.pop("w0", None)
    camp = plan["camp"] or here
    st.update(phase="go", camp=camp, tag=plan["tag_xy"], t=w.t, limit=limit, came=0)
    return plan["tag_xy"], camp, None


# ---------------------------------------------------------------- situations
def scenarios(rng):
    j = lambda v: v + rng.uniform(-150.0, 150.0)
    out = {}
    out["1 one group of 5"] = ([Group(1, (j(0), j(0)), 5, rng)], [1])
    out["2 two groups 1000 apart"] = ([Group(1, (j(0), 0), 5, rng), Group(2, (j(1000), j(300)), 5, rng)], [1, 2])
    ring = [(1500, 0), (0, 1500), (-1500, 0), (0, -1500)]
    out["3 group + patrol circling it"] = ([Group(1, (0, 0), 5, rng), Group(2, ring[0], 7, rng, beat=ring, start=rng.uniform(0, 60))], [1, 2])
    beat = [(-800, 1400), (1600, 1400), (1600, -1400), (-800, -1400)]
    out["4 two patrols walking as one"] = ([Group(1, beat[0], 6, rng, beat=beat, start=20.0),
                                           Group(2, beat[0], 6, rng, beat=beat, start=22.5)], [1, 2])
    out["4b two patrols, one 700 behind the other"] = ([Group(1, beat[0], 6, rng, beat=beat, start=20.0),
                                                       Group(2, beat[0], 6, rng, beat=beat, start=26.0)], [1, 2])
    line = [(0, 2600), (0, -2600)]
    out["5 boss group, patrol walks through it"] = ([Group(1, (0, 0), 6, rng), Group(2, line[0], 6, rng, beat=line, start=rng.uniform(0, 70))], [1, 2])
    out["6 group, another behind a wall 800 off"] = ([Group(1, (0, 0), 5, rng), Group(2, (j(0), 800), 6, rng, walled=True)], [1])
    out["7 three groups in a room"] = ([Group(1, (0, 0), 4, rng), Group(2, (1300, j(200)), 4, rng), Group(3, (600, j(-1250)), 5, rng)], [1, 2, 3])
    far = [(5200, 0), (-1500, 0)]
    out["8 patrol nobody has seen yet"] = ([Group(1, (0, 0), 5, rng), Group(2, far[0], 7, rng, beat=far, start=rng.uniform(0, 25))], [1, 2])
    out["9 one group of 12 (cannot be split)"] = ([Group(1, (0, 0), 12, rng, spread=300.0)], [1])
    out["11 three large groups close together"] = ([Group(1, (0, 0), 7, rng), Group(2, (j(900), j(500)), 7, rng),
                                                   Group(3, (j(300), j(-950)), 8, rng)], [1, 2, 3])
    out["12 two groups AND a patrol through both"] = ([Group(1, (0, 0), 5, rng), Group(2, (1100, 0), 5, rng),
                                                      Group(3, (500, 2200), 6, rng, beat=[(500, 2200), (500, -2200)], start=rng.uniform(0, 60))], [1, 2, 3])
    late = [(-1300, 7000 + rng.uniform(-900, 900)), (-1300, -7000)]
    out["13 patrol walks into the fight (seen late)"] = ([Group(1, (0, 0), 7, rng), Group(2, late[0], 7, rng, beat=late)], [1, 2], 1700.0)
    side = [(-900, 6200 + rng.uniform(-900, 900)), (-1700, 0), (-900, -6200)]
    out["14 second group wanders into the camp"] = ([Group(1, (0, 0), 8, rng), Group(2, side[0], 6, rng, beat=side)], [1, 2], 1700.0)
    out["10 patrol crossing our own path"] = ([Group(1, (0, 0), 5, rng), Group(2, (-2500, 2400), 6, rng, beat=[(-2500, 2400), (-2500, -2400)], start=rng.uniform(0, 30))], [1, 2])
    return out


def run(policy, name, seed, morale=1.0, limit_s=420.0):
    rng = random.Random(seed)
    spec = scenarios(rng)[name]
    groups, targets = spec[0], spec[1]
    w, st = World(groups, targets, morale=morale), {}
    w.sight = spec[2] if len(spec) > 2 else 5000.0
    while w.result is None and w.t < limit_s:
        goal, flag, reach = policy(w, st)
        w.step(goal, flag, reach)
    return w.result or ("avoided" if st.get("avoided") else "timeout"), round(w.t), w.max_on, st.get("bailed", 0)


POLICIES = (("walk in", walk_in), ("pull 0.36", pull_v36), ("pull next", pull_next))


def table(seeds=range(12), morale=1.0, verbose=False):
    names = list(scenarios(random.Random(0)))
    rows = []
    for name in names:
        row = [name]
        for label, pol in POLICIES:
            res = [run(pol, name, s, morale) for s in seeds]
            wins = sum(1 for r in res if r[0] == "win")
            wipes = sum(1 for r in res if r[0] == "wipe")
            secs = round(sum(r[1] for r in res if r[0] == "win") / max(1, wins))
            row.append((wins, wipes, len(res) - wins - wipes, secs, max(r[2] for r in res)))
            if "--assert" in sys.argv and label == "pull next" and morale == 1.0:
                expect_avoid = name.startswith(("4 ", "9 "))
                assert (wins == 0 if expect_avoid else wins >= 9), (name, wins, wipes)
            if verbose:
                print(f"  {name:42s} {label:10s} " + " ".join(f"{r[0][:2]}{r[1]}/{r[2]}" for r in res))
        rows.append(row)
    return rows


if __name__ == "__main__":
    for morale in (1.0, 0.6):
        print(f"\nmorale {morale:.0%}   wins/wipes/timeouts of 12 (seconds to win, most enemies on the party at once)")
        print(f"{'situation':42s} " + " ".join(f"{l:>22s}" for l, _ in POLICIES))
        for row in table(morale=morale, verbose="-v" in sys.argv):
            print(f"{row[0]:42s} " + " ".join(f"{w:>3d}/{x:d}/{t:d} ({s:>3d}s, {m:>2d})".rjust(22) for w, x, t, s, m in row[1:]))
