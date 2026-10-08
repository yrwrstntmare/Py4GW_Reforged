"""Fight recorder: turns a stream of once-a-second snapshots into one record per fight.

Pure logic, no game calls: `feed(snapshot)` returns the lines to write (dicts). The runtime
side (runtime/fightlog.py) takes the snapshots from the game and writes the lines to disk.

A snapshot is a dict:
  t        seconds (wall clock)
  me       {"xy": (x, y), "z": h, "plane": p, "dead": bool}
  allies   [{"id", "xy", "z", "hp", ...}]      the party and anything else on our side
  enemies  [{"id", "xy", "z", "hp", "alive", ...}]
  foes, killed, morale                          counters from the game (may be None)
  ctx      anything the bot wants kept with the fight (its plan, objective, settings)

What comes out, one JSON object per line:
  {"rec": "fight", ...}    once, when the fight starts: who is there (static facts, by id)
  {"rec": "s", ...}        every second: positions, health, what everyone is doing. The ten
                           seconds BEFORE first contact are included, so the approach and the
                           moment each enemy woke can be read back.
  {"rec": "who", ...}      static facts about anyone who turned up after the start
  {"rec": "end", ...}      once: how it went

Nothing here decides anything; it only keeps what happened so the numbers the bot plans with
(how far enemies notice us, which groups come together, how many of what the party can take)
can be measured instead of guessed.
"""
import math

CONTACT = 1400.0        # an enemy this close to anyone on our side: the fight is on
WATCH = 3200.0          # enemies kept in each sample
OVER = 1900.0           # nobody alive within this of the leader for QUIET_S: the fight is over
QUIET_S = 5.0
PRE_ROLL = 10           # samples kept from before contact
JUMP = 3500.0           # the leader moved this far in one sample: sent back to a shrine
MAX_S = 900.0

STATIC = ("model", "name", "level", "prof", "boss", "max_hp", "kind", "bar")


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _compact(agent):
    """One agent in one sample, without the facts that do not change."""
    out = {k: v for k, v in agent.items() if k not in STATIC and v not in (None, False, 0, [], "")}
    out["id"] = agent["id"]
    out["xy"] = [round(agent["xy"][0]), round(agent["xy"][1])]
    if agent.get("z") is not None:
        out["z"] = round(agent["z"])
    if agent.get("hp") is not None:
        out["hp"] = round(agent["hp"], 3)
    if agent.get("en") is not None:
        out["en"] = round(agent["en"], 3)
    if agent.get("vel"):
        out["vel"] = [round(agent["vel"][0]), round(agent["vel"][1])]
    return out


class FightRecorder:
    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.count = getattr(self, "count", 0)
        self._pre = []
        self._known = set()
        self._quiet_since = None
        self._last_xy = None
        self._sum = None

    # -- helpers --
    def _sample(self, snap):
        me = snap["me"]
        return {"rec": "s", "t": round(snap["t"], 2),
                "me": {"xy": [round(me["xy"][0]), round(me["xy"][1])], "z": None if me.get("z") is None else round(me["z"]),
                       "plane": me.get("plane"), "dead": bool(me.get("dead"))},
                "allies": [_compact(a) for a in snap["allies"]],
                "enemies": [_compact(e) for e in snap["enemies"] if _d(e["xy"], me["xy"]) <= WATCH],
                "foes": snap.get("foes"), "killed": snap.get("killed")}

    def _who(self, snap):
        """Static facts for anyone not described yet."""
        new = {}
        for side, agents in (("ally", snap["allies"]), ("enemy", snap["enemies"])):
            for a in agents:
                if a["id"] in self._known:
                    continue
                if side == "enemy" and _d(a["xy"], snap["me"]["xy"]) > WATCH:
                    continue
                self._known.add(a["id"])
                facts = {k: a[k] for k in STATIC if a.get(k) not in (None, "", [])}
                facts["side"] = side
                new[str(a["id"])] = facts
        return new

    def _contact(self, snap):
        ours = [snap["me"]["xy"]] + [a["xy"] for a in snap["allies"] if not a.get("dead")]
        for e in snap["enemies"]:
            if e.get("alive", True) and any(_d(e["xy"], p) <= CONTACT for p in ours):
                return True
        return False

    # -- the one entry point --
    def feed(self, snap):
        out = []
        me = snap["me"]["xy"]
        jumped = self._last_xy is not None and _d(me, self._last_xy) > JUMP
        self._last_xy = me
        if not self.active:
            if jumped:
                self._pre = []
            if not any(_d(e["xy"], me) <= WATCH for e in snap["enemies"]):
                self._pre = []
                return out
            if not self._contact(snap):
                self._pre = (self._pre + [snap])[-PRE_ROLL:]
                return out
            # first contact: open the fight, write the approach, then this sample
            self.active, self.count = True, self.count + 1
            self._known, self._quiet_since = set(), None
            first = (self._pre or [snap])[0]
            self._sum = {"t0": snap["t"], "killed0": snap.get("killed"), "foes0": snap.get("foes"),
                         "morale0": snap.get("morale"), "enemy_ids": set(), "max_at_once": 0,
                         "ally_deaths": 0, "dead_now": set(), "leader_deaths": 0, "leader_dead": False,
                         "lowest_ally_hp": 1.0, "start_xy": me}
            who = {}
            for s in self._pre + [snap]:
                who.update(self._who(s))
            out.append({"rec": "fight", "n": self.count, "t": round(first["t"], 2), "contact_t": round(snap["t"], 2),
                        "foes": snap.get("foes"), "killed": snap.get("killed"), "morale": snap.get("morale"),
                        "ctx": snap.get("ctx") or {}, "who": who})
            for s in self._pre:
                out.append(self._sample(s))
            self._pre = []
        else:
            new = self._who(snap)
            if new:
                out.append({"rec": "who", "t": round(snap["t"], 2), "who": new})

        sm = self._sum
        if jumped:
            out.append(self._end(snap, "sent back to a shrine"))
            return out
        out.append(self._sample(snap))
        close = [e for e in snap["enemies"] if e.get("alive", True) and _d(e["xy"], me) <= OVER]
        sm["enemy_ids"].update(e["id"] for e in close)
        sm["max_at_once"] = max(sm["max_at_once"], len(close))
        dead = {a["id"] for a in snap["allies"] if a.get("dead") and a.get("kind") in (None, "party", "leader")}   # not spirits or minions
        sm["ally_deaths"] += len(dead - sm["dead_now"])
        sm["dead_now"] = dead
        if snap["me"].get("dead") and not sm["leader_dead"]:
            sm["leader_deaths"] += 1
        sm["leader_dead"] = bool(snap["me"].get("dead"))
        for a in snap["allies"]:
            # the party only: spirits and minions sit near zero health as a matter of course
            if a.get("hp") is not None and not a.get("dead") and a.get("kind") in ("party", "leader"):
                sm["lowest_ally_hp"] = min(sm["lowest_ally_hp"], a["hp"])
        if close:
            self._quiet_since = None
        elif self._quiet_since is None:
            self._quiet_since = snap["t"]
        if self._quiet_since is not None and snap["t"] - self._quiet_since >= QUIET_S:
            out.append(self._end(snap, "over"))
        elif snap["t"] - sm["t0"] > MAX_S:
            out.append(self._end(snap, "too long"))
        return out

    def close(self, t, why="stopped"):
        """The run is ending (area left, bot stopped): finish an open fight."""
        if not self.active:
            return []
        return [self._end({"t": t, "me": {"xy": self._last_xy or (0, 0)}}, why)]

    def _end(self, snap, why):
        sm = self._sum
        kills = None
        if snap.get("killed") is not None and sm["killed0"] is not None:
            kills = snap["killed"] - sm["killed0"]
        rec = {"rec": "end", "n": self.count, "t": round(snap["t"], 2), "why": why,
               "seconds": round(snap["t"] - sm["t0"], 1), "kills": kills,
               "enemies_met": len(sm["enemy_ids"]), "most_at_once": sm["max_at_once"],
               "ally_deaths": sm["ally_deaths"], "leader_deaths": sm["leader_deaths"],
               "lowest_ally_hp": round(sm["lowest_ally_hp"], 2),
               "morale_before": sm["morale0"], "morale_after": snap.get("morale"),
               "foes_before": sm["foes0"], "foes_after": snap.get("foes"),
               "moved": round(_d(sm["start_xy"], snap["me"]["xy"]))}
        keep = self.count
        self.reset()
        self.count = keep
        return rec
