"""The behaviour-tree node that carries out the engine's decisions.

Movement, obstacle avoidance, stall recovery, pausing for fights and the fighting
itself are all Reforged's own routines (MoveAndKill inside a BottingTree with
HeroAI). This node only decides where the next walk goes.
"""
import math
import time

from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Sources.ApoSource.ApoBottingLib import wrappers as BT

from ..core import elites, tactics
from ..core.engine import DONE, FAILED
from ..core.mounts import RideGround
from . import game, ride_store

REPLAN_S = 1.0
STEP_TIMEOUT_S = 240.0       # one walking step, fights included
NO_PROGRESS_S = 1800.0      # foes remain and none has died for this long: give the area up
STUCK_GIVE_UP_S = 600.0     # pinned within 600 units for this long, nothing dying: the area cannot be finished
STALL_S = 45.0               # no movement and no kill for this long: give the step up
WALK_STALL_S = 5.0          # the same with no enemy anywhere near: we are caught on something, not fighting
LOOT_HOLD_S = 20.0          # longest the walk waits for HeroAI to pick up drops
STUCK_CMD_S = 30.0          # not moved at all this long, with nothing to fight: /stuck
STALL_RADIUS = 350.0         # "no movement" = still inside this circle (rocking back and forth on a tree counts as none)


from .guard import guarded_tick
from .node_blessing import BlessingMixin
from .node_capture import CaptureMixin, e_prof
from .node_fight import FightMixin
from .node_pull import PullMixin

_RETIRED = []                 # (time, behaviour tree) kept alive for a while after being abandoned
RETIRE_SECONDS = 90.0         # long past any route request (a wipe's loading screen included)


def retire(tree):
    now = time.time()
    _RETIRED.append((now, tree))
    while _RETIRED and now - _RETIRED[0][0] > RETIRE_SECONDS:
        _RETIRED.pop(0)


class AdaptiveNode(FightMixin, PullMixin, BlessingMixin, CaptureMixin, BehaviorTree.Node):
    def __init__(self, session, name="AdaptiveVanquish", target_map_id=None, transit=None, arrive_map=None):
        super().__init__(name=name, node_type="AdaptiveVanquish", node_category="action")
        self.session = session
        self.target_map_id = target_map_id   # campaign runs: the only map this node may act in
        self.transit = transit               # {"xy": ...}: cross the area to this point instead of vanquishing
        self.arrive_map = arrive_map         # walking into this map along the way counts as arriving
        self._child = None
        self._key = None
        self._target = None
        self._started = 0.0
        self._last_plan = 0.0
        self._progress_at, self._progress_xy, self._progress_kills = 0.0, None, -1
        self._left_exit_zone = False     # the party starts beside an exit; arm the guard once clear
        self._rest_since = None
        self._was_dead = False
        self._cap = None                 # elite capture in progress: dict(stage, boss, ...)
        self._cap_done = set()           # boss agent ids already dealt with
        self._bless_tries = {}           # blessing NPC agent id -> attempts
        self._bless_check = 0.0

    def reset(self):
        super().reset()
        self._unflag("reset")
        self._drop_child()
        try:
            self._leader_fights(True)
        except Exception:
            pass
        self._loot_casting_off, self._loot_since = False, 0.0

    def _loot_check(self, s, eng, now):
        """Once after each fight (nothing within 1500 for 4 s), log what is on the ground and what
        the Loot Filters want of it, with HeroAI's looting flags. Diagnostics only."""
        x, y = eng.player_xy
        close = any(e.alive and e.in_range and math.hypot(e.xy[0] - x, e.xy[1] - y) < 1500.0
                    for e in eng.mem.enemies.values())
        if close:
            self._loot_calm, self._loot_logged = 0.0, False
            return
        if not self.__dict__.get("_loot_calm"):
            self._loot_calm = now
            return
        if self.__dict__.get("_loot_logged", True) or now - self._loot_calm < 4.0:
            return
        self._loot_logged = True
        try:
            rep = game.loot_report()
        except Exception as e:
            rep = {"error": repr(e)}
        if rep.get("items"):
            s.log.event("loot_check", heroai_looting=self.blackboard.get("looting_enabled"),
                        looting_active=self.blackboard.get("LOOTING_ACTIVE"), **rep)

    def _looting(self, s, eng, now):
        """Reforged's HeroAI is picking up loot (by your Loot Filters, through the Messaging
        widget): hold still so the next walk does not drag the leader off the drops. Only with no
        enemy close, and never for more than LOOT_HOLD_S at a stretch. While holding, the leader's
        own HeroAI casting is paused: upkeep spells cast between pickups stopped the walk to a drop
        and the job hung."""
        held = self._loot_hold(s, eng, now)
        if held and not self.__dict__.get("_loot_casting_off"):
            self._loot_casting_off = True
            self._leader_fights(False)
        elif not held and self.__dict__.get("_loot_casting_off"):
            self._loot_casting_off = False
            self._leader_fights(True)
        return held

    def _loot_hold(self, s, eng, now):
        x, y = eng.player_xy
        if any(e.alive and e.in_range and math.hypot(e.xy[0] - x, e.xy[1] - y) < 1200.0
               for e in eng.mem.enemies.values()):
            self._loot_since = 0.0
            return False
        # HeroAI's own flag is only up for a moment when it hands the job to the Messaging widget;
        # the job itself (walking to each drop) runs on after it. Hold while the job is running,
        # or while anything the Loot Filters want still lies within reach.
        busy = bool(self.blackboard.get("LOOTING_ACTIVE", False))
        if not busy and now - self.__dict__.get("_loot_polled", 0.0) >= 0.5:
            self._loot_polled = now
            self._loot_busy = game.pickup_running() or game.loot_wanted() > 0
        busy = busy or self.__dict__.get("_loot_busy", False)
        if not busy:
            self._loot_since = 0.0
            return False
        if not self.__dict__.get("_loot_since"):
            gx, gy = self.__dict__.get("_loot_gave_up_xy", (1e9, 1e9))
            if math.hypot(x - gx, y - gy) < 1500.0:
                return False                 # gave up on the drops here: not again until we have moved on
            self._loot_since, self._loot_idle_since = now, None
            self._drop_child()
            s.log.event("looting", player=[round(x), round(y)], wanted=game.loot_wanted())
        # HeroAI's pick-up job can be cut short (a cast between pickups) and not start again:
        # gold lying 5 away was waited on for 20 s and left behind, with no job running. When the
        # job has been gone for 3 s and something wanted still lies here, pick it up ourselves.
        if now - self.__dict__.get("_loot_job_polled", 0.0) >= 0.5:
            self._loot_job_polled = now
            self._loot_job = game.pickup_running() or bool(self.blackboard.get("LOOTING_ACTIVE", False))
        if not self.__dict__.get("_loot_job", True):
            idle = self.__dict__.get("_loot_idle_since")
            if idle is None:
                idle = self._loot_idle_since = now
            if now - idle >= 3.0 and now - self.__dict__.get("_loot_own_t", 0.0) >= 1.5:
                self._loot_own_t = now
                got = game.pick_up_nearest_loot()
                if got and self.__dict__.get("_loot_own_id") != got:
                    self._loot_own_id = got
                    s.log.event("looting", own_pickup=got, player=[round(x), round(y)])
        else:
            self._loot_idle_since = now
        if now - self._loot_since < LOOT_HOLD_S:
            return True
        self._loot_idle_since = None
        self._loot_gave_up_xy, self._loot_since, self._loot_busy = (x, y), 0.0, False
        try:
            left = game.loot_left()
        except Exception as e:
            left = repr(e)
        s.log.event("looting", gave_up=True, left=left, pickup_running=game.pickup_running())
        return False

    def _unstick_check(self, s, eng, now):
        """The leader has not moved at all for a long while, though steps keep being given and
        no enemy is close: caught in the scenery (seen at the foot of a staircase, where every
        walk and every sidestep went nowhere for fifteen minutes). Use the game's own /stuck,
        which puts the character back on open ground nearby."""
        x, y = eng.player_xy
        last = self.__dict__.get("_still_at")
        if last is None or math.hypot(x - last[0], y - last[1]) > 40.0:
            self._still_at, self._still_since = (x, y), now
            return
        near = min((math.hypot(e.xy[0] - x, e.xy[1] - y) for e in eng.mem.enemies.values()
                    if e.alive and e.in_range), default=9e9)
        # "trying to walk" = a step was handed out in the last 10 s. (Was: a walk in hand at this
        # very tick. Steps given up every 5 s left a gap each time, which reset the clock: the
        # leader stood 1 h 40 min at one spot in map 114 and /stuck was never sent.)
        walking = self._child is not None or now - (self.__dict__.get("_started") or 0.0) < 10.0
        if (near < 1300.0 or not walking or self.__dict__.get("_loot_since")
                or self.blackboard.get("COMBAT_ACTIVE", False)):
            # standing still to fight, to loot or to wait is not being stuck: the clock only
            # counts time spent trying to walk with nothing else going on
            self._still_since = now
            return
        if (now - self._still_since < STUCK_CMD_S
                or now - self.__dict__.get("_stuck_sent", 0.0) < STUCK_CMD_S):
            return
        self._stuck_sent = now
        try:
            game.send_stuck()
            ok = True
        except Exception as e:
            ok = repr(e)
        s.log.event("stuck_command", player=[round(x), round(y)], still_for=round(now - self._still_since),
                    nearest_enemy=round(near) if near < 9e9 else None, sent=ok)
        self._still_since = now

    def _drop_child(self):
        # A walk that has just started has asked Py4GW's path planner for a route; the answer is
        # written on the game's own thread a moment later. If the walk is thrown away before
        # then, that write lands in freed memory and the game crashes (seen twice, both right
        # after a shrine revival). So an abandoned walk is not destroyed: it is parked, untouched,
        # until any planning it started is long finished.
        if self._child is not None:
            retire(self._child)
        self._child, self._key, self._target = None, None, None
        self._action_fin = None
        self._skill_step = False

    # ---- route actions ----
    def _route_action_tick(self, eng, s, now):
        """Do what the route does here before walking on: every action tied to a point of the
        route at or before the next one, not done yet. And while the route has the party riding
        a junundu wurm, get back on one (the last burrow) after falling off or dying. True when
        an action was started."""
        acts = getattr(eng, "guide_actions", None)
        if not acts:
            return False
        try:
            if not game.map_ready() or game.player_dead_safe():
                return False
        except Exception:
            return False
        ground = self._ride_watch(eng, s, acts, now)
        if self._riding() and now - self.__dict__.get("_charge_logged", 0.0) >= 10.0:
            nxt0 = eng.next_guide_src()
            for i, a in enumerate(acts):
                if (a.get("save_from") is not None and a["save_from"] <= nxt0 < 10 ** 9
                        and i not in self.__dict__.get("_actions_done", ())):
                    # how the fights on the way fill it (does a wurm's fight build adrenaline?)
                    self._charge_logged = now
                    st = game.skill_state(int(a["value"]))
                    s.log.event("skill_charge", skill=a["value"], adrenaline=st.get("adrenaline"),
                                need=st.get("need"), fighting=bool(self.blackboard.get("COMBAT_ACTIVE", False)),
                                point=nxt0, dist=round(math.hypot(eng.player_xy[0] - a["pos"][0],
                                                                  eng.player_xy[1] - a["pos"][1])) if a.get("pos") else None)
        if bool(self.blackboard.get("COMBAT_ACTIVE", False)):
            # in a fight on foot ON the sulfur: no fighting there, get to a burrow (it kills on foot)
            lvl = getattr(eng, "player_plane", None)
            if (ground is not None and not ground.riding and ground.ridden
                    and ground.on_ridden(eng.player_xy, lvl) and not game.is_junundu()):
                return self._mount_check(eng, s, acts, ground, now, eng.next_guide_src(), urgent=True)
            return False
        nxt = eng.next_guide_src()
        if not eng.__dict__.get("_spots_applied"):
            eng._spots_applied = True
            for a in acts:
                if a.get("do") == "skill" and a.get("at") is not None:
                    spot = ride_store.route_spot(game.map_id(), a["at"])
                    if spot is not None:
                        s.log.event("route_action", stage="spot_set_by_hand", at=a["at"], was=a.get("pos"), now=list(spot))
                        a["pos"] = [spot[0], spot[1]]
        if "_actions_done" not in self.__dict__:
            kept = eng.__dict__.get("_route_actions_done")
            if kept is not None:
                # the step was rebuilt (after a wipe) in the same visit: what was done stays done
                # (it went back for the first burrow, across the map, and died on the way)
                self._actions_done = kept
                self._mount_failed = eng.__dict__.setdefault("_route_mount_failed", {})
                s.log.event("route_action", stage="kept", next_point=nxt, already_done=sorted(kept))
            else:
                self._actions_done = set()
                known = s.__dict__.pop("_resume_actions", None)
                if s.__dict__.pop("_resume_route", False) or known is not None:
                    # picked up mid-route after a reload: what the route did before this point is
                    # done - as saved; from an older save, all but skill steps (a pillar passed by
                    # is not a pillar knocked down: 0.50.46 resumed past it and never smashed it)
                    self._actions_done = set(known) if known is not None else \
                        {i for i, a in enumerate(acts) if a["at"] < nxt and a["do"] != "skill"}
                    s.log.event("route_action", stage="resumed", next_point=nxt, already_done=sorted(self._actions_done))
                eng._route_actions_done = self._actions_done
                self._mount_failed = eng.__dict__.setdefault("_route_mount_failed", {})
        done = self._actions_done
        for i, a in enumerate(acts):
            if a.get("save_from") is not None and i not in done and a["save_from"] <= nxt < 10 ** 9 and self._riding():
                self._save_slot(a, s, True)
            elif a.get("save_from") is not None and not self._riding():
                self._save_slot(a, s, False)     # on foot that slot is the character's own skill
        deferred = eng.__dict__.setdefault("_route_deferred", {})
        for i, a in enumerate(acts):
            if (a["do"] == "skill" and i not in done and a.get("save_from") is not None
                    and a["save_from"] <= nxt and a.get("pos") and self._riding()
                    and now - self.__dict__.get("_charge_look", 0.0) >= 1.0):
                self._charge_look = now
                pos = a["pos"]
                near = math.hypot(eng.player_xy[0] - pos[0], eng.player_xy[1] - pos[1]) <= float(a.get("charged_go", 6000.0))
                if near and game.skill_state(int(a["value"])).get("charged"):
                    # charged in a fight on the way: go and use it now, before it drains
                    deferred.pop(i, None)
                    s.log.event("route_action", stage="charged_go", at=a.get("at"), skill=a["value"],
                                dist=round(math.hypot(eng.player_xy[0] - pos[0], eng.player_xy[1] - pos[1])))
                    return self._start_route_action(i, a, now, s)
        for i, a in enumerate(acts):
            if i in done or a["at"] > nxt or i in deferred:
                continue
            if a["do"] == "gadget" and game.is_junundu():
                done.add(i)                  # already riding: the burrow is not needed
                self._ride_from = tuple(a["value"])
                continue
            return self._start_route_action(i, a, now, s)
        return self._mount_check(eng, s, acts, ground, now, nxt)

    def _mount_check(self, eng, s, acts, ground, now, nxt, urgent=False):
        """Get back into a wurm when the way needs one; True when the walk to a burrow started.
        urgent: on foot on the sulfur in a fight; it is left for the nearest burrow at once."""
        # On foot where the wurm is needed: standing on ground we rode over (died, fell off), or
        # the walk in hand goes onto it (the game set us down on rock and the plan leads back).
        # Mount first, at the nearest burrow of the route, not the last one used. (Sulfurous
        # Wastes: set down by the second burrow, it ran for the first one across the sulfur.)
        if (ground is not None and ground.ridden and not ground.riding
                and now - self.__dict__.get("_remount_at", 0.0) > (10.0 if urgent else 30.0)):
            walking = self._child is not None and self._key is not None and self._key[0] != "action"
            lvl = getattr(eng, "player_plane", None)
            if ground.needs_mount(eng.player_xy, self._target if walking else None, plane=lvl):
                s.log.event("route_action", stage="needs_mount",
                            why="standing on wurm ground" if ground.on_ridden(eng.player_xy, lvl) else "the step leads onto wurm ground",
                            player=[round(eng.player_xy[0]), round(eng.player_xy[1])],
                            step=[round(self._target[0]), round(self._target[1])] if walking and self._target else None,
                            ridden_cells=len(ground.ridden), plane=lvl)
                failed = self.__dict__.setdefault("_mount_failed", {})
                mounts = list(dict.fromkeys(tuple(a["value"]) for a in acts
                                            if a.get("do") == "gadget" and failed.get(tuple(a["value"]), 0) < 2))
                self._remount_at = now
                avoid = [(hx, hy, eng.cfg.hazard_radius) for hx, hy in getattr(eng, "hazards", ())]
                pick = ground.safest_mount(eng.nav, eng.player_xy, mounts, avoid=avoid)
                if pick is None or (not pick[2] and not ground.on_ridden(eng.player_xy, lvl)):
                    # no burrow can be walked to without crossing the sulfur: do not go; keep the
                    # step that wanted it off the plan instead
                    s.log.event("route_action", stage="no_safe_burrow", burrows=[list(m) for m in mounts],
                                player=[round(eng.player_xy[0]), round(eng.player_xy[1])])
                    if walking and self._target is not None:
                        try:
                            eng.add_wall(eng.player_xy, self._target[:2])
                        except Exception:
                            pass
                        self._drop_child()
                    return False
                spot, way, safe = pick
                s.log.event("route_action", stage="burrow_way", burrow=list(spot), safe=safe, points=len(way))
                return self._start_route_action(-1, {"do": "gadget", "value": list(spot), "at": nxt, "way": way}, now, s)
        return False

    def _ground_watch(self, eng, s, now):
        """On foot, health falling fast with no enemy near and no condition or hex on us: the
        ground itself hurts (sulfur sand never ridden, a trap). Note the spot (kept off for good,
        and wurm ground where there are burrows) and go straight back to where it did not hurt.
        Keeps a trail of places on foot that were safe, for that way back."""
        if now - self.__dict__.get("_gw_t", 0.0) < 0.5:
            return
        self._gw_t = now
        try:
            hp, riding = float(game.my_health()), game.is_junundu()
        except Exception:
            return
        if hp <= 0.01 or self._was_dead or now - self.__dict__.get("_revived_at", 0.0) < 3.0:
            self.__dict__.get("_hp_hist", []).clear()   # dead, or just raised: not the ground
            return
        hist = self.__dict__.setdefault("_hp_hist", [])
        hist.append((now, hp))
        while hist and now - hist[0][0] > 2.5:
            hist.pop(0)
        px, py = eng.player_xy
        drop = max(h for _t, h in hist) - hp
        trail = self.__dict__.setdefault("_trail", [])
        if not riding and drop < 0.02 and hp > 0.5 and (not trail or math.hypot(px - trail[-1][0], py - trail[-1][1]) > 200.0):
            trail.append((px, py))
            del trail[:-80]
        ground = self.__dict__.get("_ride_ground")
        if ground is not None and not riding and drop < 0.02 and hp > 0.5:
            # on wurm ground on foot and not hurt for 3 s: it is not wurm ground after all
            lvl = getattr(eng, "player_plane", None)
            if ground.on_ridden((px, py), lvl):
                since = self.__dict__.setdefault("_onfoot_since", {})
                key = (int(px // 400), int(py // 400))
                since.setdefault(key, now)
                if now - since[key] >= 3.0 and ground.walked_safely((px, py), lvl):
                    self._ground_dirty = True
                    s.log.event("route_action", stage="walked_safely", player=[round(px), round(py)])
        if riding or drop < 0.10 or now - self.__dict__.get("_gw_alarm", 0.0) < 6.0:
            return
        if bool(self.blackboard.get("COMBAT_ACTIVE", False)) or game.leader_conditioned():
            return
        if any(e.alive and e.in_range and math.hypot(e.xy[0] - px, e.xy[1] - py) < 1500.0 for e in eng.mem.enemies.values()):
            return
        self._gw_alarm = now
        if not any(math.hypot(px - x, py - y) < 200.0 for x, y in eng.hazards):
            eng.hazards.append((px, py))
            s.save_hazards()
        if ground is not None:
            ground.mark((px, py), getattr(eng, "player_plane", None))
            self.__dict__.get("_onfoot_since", {}).pop((int(px // 400), int(py // 400)), None)
            self._ground_dirty = True
        s.log.event("ground_hurts", player=[round(px), round(py)], hp=round(hp, 2), drop=round(drop, 2),
                    wurm_ground=ground is not None)
        self._go_back(eng, s, now, (px, py), "the ground hurts")

    def _teleport_watch(self, eng, s, now):
        """Learn teleporters by watching: the leader jumps 1500+ in a moment, alive, with an object
        beside the spot he left - that object takes people to where he is now. Kept per area in
        gwamm_logs/teleporters.json, whoever used it (the bot or you by hand)."""
        px, py = eng.player_xy
        if now - self.__dict__.get("_tp_scan", 0.0) >= 1.0:
            self._tp_scan = now
            try:
                self._tp_gadgets = game.gadgets_near(px, py, 1000.0)
            except Exception:
                self._tp_gadgets = []
        if now - self.__dict__.get("_census_t", 0.0) >= 5.0:
            # every object the game shows, kept for the visit and logged once each: what a
            # teleporter pad and its switches are (and where) is learnt from this
            self._census_t = now
            mem = eng.__dict__.setdefault("_gadget_mem", {})
            try:
                seen = game.gadgets_near(px, py, 6000.0)
            except Exception:
                seen = []
            fresh = [g for g in seen if g[0] not in mem]
            for g in seen:
                mem[g[0]] = g
            if fresh:
                s.log.event("gadgets_seen", player=[round(px), round(py)], new=[list(g) for g in fresh[:40]],
                            known=len(mem))
        last = self.__dict__.get("_tp_last")
        self._tp_last = (now, px, py, list(self.__dict__.get("_tp_gadgets", [])))
        if last is None or now - last[0] > 3.0 or self._was_dead or now - self.__dict__.get("_revived_at", 0.0) < 6.0:
            return
        if math.hypot(px - last[1], py - last[2]) < 1500.0:
            return
        if self.__dict__.get("_tp_job") is not None:
            return                                       # the job records its own jump
        near = [g for g in last[3] if math.hypot(g[2] - last[1], g[3] - last[2]) < 900.0]
        if not near:
            return
        g = min(near, key=lambda g: math.hypot(g[2] - last[1], g[3] - last[2]))
        try:
            heroes = [round(math.hypot(x - px, y - py)) for x, y in game.hero_positions()]
        except Exception:
            heroes = []
        new = ride_store.add_teleporter(game.map_id(), (g[2], g[3]), (px, py), g[1])
        s.log.event("teleporter", learnt=new, at=[g[2], g[3]], gadget=g[1], to=[round(px), round(py)],
                    heroes_from_me=heroes)

    def _try_teleport(self, eng, s, now, goal):
        """The objective cannot be walked to: work a teleporter. Crystal Desert ones are a pad
        and four switches used in an order that changes every visit (Guild Wars Wiki,
        Teleporter): step on the pad (it may be on already), then try the orders one by one
        with the heroes flagged out of the way (a hero on the pad or a switch spoils it).
        The jump is learnt by `_teleport_watch`. True when the job was started."""
        from ..core.geometry import rank_teleporters, teleporter_sets
        if self.__dict__.get("_tp_job") is not None:
            return True
        px, py = eng.player_xy
        # every object seen this visit, not only those near: the pad that leads up to unreachable
        # ground can stand far from it (Prophet's Path: the north-west shelf is reached from a
        # pad in the south-east)
        mem = eng.__dict__.get("_gadget_mem") or {}
        try:
            near = game.gadgets_near(px, py, 6000.0)
        except Exception:
            near = []
        allg = {g[0]: g for g in list(mem.values()) + near}
        tried = self.__dict__.setdefault("_tp_sets_tried", set())
        hints = ride_store.pad_hints(game.map_id())
        sets = []
        for t in teleporter_sets(list(allg.values())):
            if t["pad"][0] == 0 and hints:
                # no pad object: the circle's centre is rough, a pad stood on before is exact
                h = min(hints, key=lambda h: math.hypot(h[0] - t["pad"][1], h[1] - t["pad"][2]))
                if math.hypot(h[0] - t["pad"][1], h[1] - t["pad"][2]) <= 600.0:
                    t["pad"] = (0, h[0], h[1])
            if (round(t["pad"][1] / 100), round(t["pad"][2] / 100)) not in tried:
                sets.append(t)
        # the right side: from below, a pad up on ground the pathing refused is out of reach;
        # once across, only a pad near us can be up here with us
        cut = ride_store.unreachable(game.map_id())
        across = bool(self.__dict__.get("_tp_across")) and self.__dict__.get("_revived_at", 0.0) < self._tp_across
        if across:
            sets = [t for t in sets if math.hypot(t["pad"][1] - px, t["pad"][2] - py) < 5000.0]
        if not sets:
            s.log.event("teleporter", none_near=True, goal=[round(goal[0]), round(goal[1])])
            return False
        learnt = ride_store.teleporters(game.map_id())
        cut = ride_store.unreachable(game.map_id())
        ranked = rank_teleporters(sets, (px, py), goal, learnt, hints, cut)
        if not ranked:
            s.log.event("teleporter", none_right_side=True, sets=len(sets), goal=[round(goal[0]), round(goal[1])])
            return False
        sets = ranked
        known = any(math.hypot(r["at"][0] - sets[0]["pad"][1], r["at"][1] - sets[0]["pad"][2]) < 400.0 for r in learnt)
        tp = sets[0]
        tried.add((round(tp["pad"][1] / 100), round(tp["pad"][2] / 100)))
        import itertools
        perms = list(itertools.permutations(range(4)))
        worked = self.__dict__.get("_tp_orders", {}).get((round(tp["pad"][1] / 100), round(tp["pad"][2] / 100)))
        # orders already tried at this pad this visit go last: the order holds for the visit
        # (wiki), so a job cut short (a fight, a death) carries on where it left off
        done = self.__dict__.setdefault("_tp_done_orders", {}).setdefault(
            (round(tp["pad"][1] / 100), round(tp["pad"][2] / 100)), set())
        if len(done) >= len(perms):
            done.clear()                                 # all tried, none took: start again
        perms.sort(key=lambda p: p in done)
        if worked in perms:
            perms.remove(worked)
            perms.insert(0, worked)                      # the order that took us last time, first
        self._tp_job = {"pad": tp["pad"], "sw": tp["switches"], "perms": perms, "ids": tp.get("ids"), "done": done,
                        "order_t": now,
                        "i": -1, "step": 0, "phase": "flag", "t0": now, "t": now, "goal": goal}
        s.log.event("teleporter", job="start", hinted=any(math.hypot(h[0] - tp["pad"][1], h[1] - tp["pad"][2]) < 300.0 for h in hints), pad=[round(tp["pad"][1]), round(tp["pad"][2])],
                    switches=[[round(x), round(y)] for _a, x, y in tp["switches"]], known=known)
        self._drop_child()
        return True

    def _tp_left_behind(self, eng, s, now):
        """Just across: wait up to 10 s for every living hero to arrive by us. Any still far away
        were left on the other side (off the pad when it fired): logged, so the pad routine can
        be put right, and the run goes on."""
        landed = self.__dict__.get("_tp_landed")
        if landed is None:
            return False
        t0, lx, ly, _pad = landed
        try:
            far = [h for h in game.hero_positions() if math.hypot(h[0] - lx, h[1] - ly) > 2000.0]
        except Exception:
            far = []
        if not far:
            s.log.event("teleporter", heroes_with_us=True, seconds=round(now - t0, 1))
            self._tp_landed = None
            return False
        if now - t0 < 10.0 and not bool(self.blackboard.get("COMBAT_ACTIVE", False)):
            self._idle_at = now
            if not self.__dict__.get("_tp_landed_unflag"):
                self._tp_landed_unflag = True
                try:
                    game.unflag_heroes(game.hero_agent_ids())
                except Exception:
                    pass
            return True
        s.log.event("teleporter", heroes_left_behind=len(far), at=[[round(x), round(y)] for x, y in far])
        self._tp_landed = None
        return False

    def _party_watch(self, eng, s, now):
        """The leader alive, most heroes far off (left at a flag, or run back to one from the
        shrine): take every flag down and wait for them, rather than walk on alone. Not while
        a pull, the front-line hold, the teleporter or a ride has them placed on purpose, and
        not in a fight close by. True while waiting (45 s at most, then a minute's rest)."""
        if (self._riding() or self.__dict__.get("_tp_job") is not None
                or (self.__dict__.get("_pull") or {}).get("phase")
                or (self.__dict__.get("_lead") or {}).get("flagged")
                or self.__dict__.get("_gather_since") is not None
                or now < self.__dict__.get("_regroup_off_until", 0.0)
                or now - self.__dict__.get("_regroup_look", 0.0) < 1.0 and not self.__dict__.get("_regroup_since")):
            return False
        self._regroup_look = now
        try:
            heroes = game.hero_positions()
        except Exception:
            heroes = []
        px, py = eng.player_xy
        far = [h for h in heroes if math.hypot(h[0] - px, h[1] - py) > 2500.0]
        close_fight = any(e.alive and e.in_range and math.hypot(e.xy[0] - px, e.xy[1] - py) < 1200.0
                          for e in eng.mem.enemies.values())
        if not heroes or len(far) * 2 < len(heroes) or close_fight:
            if self.__dict__.get("_regroup_since"):
                s.log.event("regroup", stage="done", seconds=round(now - self._regroup_since, 1),
                            far_left=len(far), fight=close_fight)
            self._regroup_since = None
            return False
        if not self.__dict__.get("_regroup_since"):
            self._regroup_since, self._regroup_unflag = now, 0.0
            s.log.event("regroup", stage="start", far=len(far), heroes=len(heroes),
                        dist=[round(math.hypot(h[0] - px, h[1] - py)) for h in heroes])
        if now - self._regroup_since > 45.0:
            s.log.event("regroup", stage="gave_up", far=len(far))
            self._regroup_since, self._regroup_off_until = None, now + 60.0
            return False
        if now - self._regroup_unflag >= 4.0:
            self._regroup_unflag = now
            try:
                game.unflag_heroes(())
            except Exception:
                pass
        self._drop_child()
        self._idle_at = now                              # waiting on purpose, not stuck
        return True

    def _teleport_first(self, eng, s, now):
        """The objective stands where the game's pathing refused us before (kept per area): go
        for the teleporter now instead of walking to the edge below and finding out again.
        (Prophet's Path, started on the pad: walked 10000 back to the edge first.) Not once
        across (after a jump, or standing near such ground); a death puts us back below."""
        obj = eng.objective
        if obj is None or obj.pos is None or self.__dict__.get("_tp_job") is not None or self.transit is not None:
            return False
        if now - self.__dict__.get("_tp_first_t", 0.0) < 5.0:
            return False
        self._tp_first_t = now
        if self.__dict__.get("_tp_across") and self.__dict__.get("_revived_at", 0.0) < self._tp_across:
            return False
        cut = ride_store.unreachable(game.map_id())
        if not any(math.hypot(u[0] - obj.pos[0], u[1] - obj.pos[1]) < 1500.0 for u in cut):
            return False
        px, py = eng.player_xy
        if any(math.hypot(u[0] - px, u[1] - py) < 3000.0 for u in cut):
            return False                                 # at the edge, or up there already
        if now - self.__dict__.get("_tp_first_none", 0.0) < 60.0:
            return False
        if self._try_teleport(eng, s, now, obj.pos):
            s.log.event("teleporter", first=True, goal=[round(obj.pos[0]), round(obj.pos[1])])
            return True
        self._tp_first_none = now
        return False

    def _tp_walk(self, eng, s, job, x, y, tol, now, what):
        """Get the leader to (x, y) within `tol`, for the teleporter job. The game's own walk
        first; when that stops getting closer for 3 s (it walks to the spot on the level we
        stand on, and the pad is up on raised ground: hung 527 under it, Prophet's Path 00:12),
        our own route in short hops onto the other level. True on arrival, "stuck" after 40 s
        with no gain, else False."""
        px, py = eng.player_xy
        d = math.hypot(px - x, py - y)
        w = job.get("walk")
        if w is None or w["to"] != (round(x), round(y)):
            # our own route got us here before: no 3 s on the game's walk first (it lost ~3 s
            # an order, every order, under the raised pad)
            first = what in job.setdefault("nav_first", set())
            w = job["walk"] = {"to": (round(x), round(y)), "best": d, "best_t": now - (3.1 if first else 0.0),
                               "mode": "game", "mover": None, "moved": 0.0, "planes_tried": []}
        if d <= tol:
            if w.get("used_nav"):
                job.setdefault("nav_first", set()).add(what)
            job["walk"] = None
            return True
        if d < w["best"] - 60.0:
            w["best"], w["best_t"] = d, now
        if now - w["best_t"] > 40.0:
            s.log.event("teleporter", walk_stuck=what, dist=round(d), modes_tried=w["planes_tried"])
            job["walk"] = None
            return "stuck"
        if w["mode"] == "game":
            if now - w["moved"] >= 0.8:
                w["moved"] = now
                game.move_to(x, y)
            if now - w["best_t"] > 3.0:
                from ..core.geometry import nav_waypoints, planes_near
                here = getattr(eng, "player_plane", None)
                planes = [p for p in planes_near(eng.nav, (x, y)) if p not in w["planes_tried"]]
                planes.sort(key=lambda p: p == here)     # another level than ours first
                plane = planes[0] if planes else None
                if plane is None and None in w["planes_tried"]:
                    pts = []                             # every level near tried already
                else:
                    pts = nav_waypoints(eng.nav, (px, py), (x, y), start_plane=here, goal_plane=plane)
                w["planes_tried"].append(plane)
                s.log.event("teleporter", walk_hops=what, dist=round(d), my_level=here, to_level=plane,
                            hops=len(pts))
                if pts:
                    w["used_nav"] = True
                    w["mode"], w["mover"], w["mover_t"] = "nav", BT.Move(pos=[tuple(p) for p in pts], tolerance=max(60.0, tol)), now
                else:
                    w["best_t"] = now                    # nothing better: keep at the game's walk
            return False
        mv = w["mover"]
        mv.root.blackboard = self.blackboard
        st = mv.root.tick()
        if st != BehaviorTree.NodeState.RUNNING or now - w.get("mover_t", now) > 30.0:
            w["mode"], w["mover"], w["best_t"] = "game", None, now   # done or no use: the game's walk again
        return False

    def _tp_job_tick(self, eng, s, now):
        """One tick of the teleporter job. True while it holds the party."""
        job = self.__dict__.get("_tp_job")
        if job is None:
            return False
        px, py = eng.player_xy
        pad = job["pad"]
        gap = math.hypot(px - pad[1], py - pad[2])

        def end(result):
            self._tp_job = None
            if result in ("jumped", "died"):
                # worked, or cut short: the same pad may be wanted again (a way back, after a wipe)
                self.__dict__.get("_tp_sets_tried", set()).discard((round(pad[1] / 100), round(pad[2] / 100)))
            try:
                game.unflag_heroes(game.hero_agent_ids())
            except Exception:
                pass
            s.log.event("teleporter", job=result, orders_tried=max(0, job["i"]), seconds=round(now - job["t0"]))
            return False

        if self.__dict__.get("_revived_at", 0.0) > job["t0"]:
            return end("died")                           # raised elsewhere: start over when needed
        last = job.get("last")
        job["last"] = (px, py)
        if last is not None and math.hypot(px - last[0], py - last[1]) > 1500.0:
            # taken: kept with the exact pad and the switch ids, for the next visit
            new = ride_store.add_teleporter(game.map_id(), (pad[1], pad[2]), (px, py),
                                            (job.get("ids") or [0])[0], pad=True)
            pk = (round(pad[1] / 100), round(pad[2] / 100))
            self._tp_across = 0.0 if self.__dict__.get("_tp_across") else now     # each jump crosses over
            self.__dict__.setdefault("_tp_orders", {})[pk] = job["perms"][max(0, job["i"])]
            self.__dict__.get("_tp_sets_tried", set()).discard(pk)   # usable again (way back, after a death)
            s.log.event("teleporter", learnt=new, at=[round(pad[1]), round(pad[2])], to=[round(px), round(py)],
                        order=list(job["perms"][max(0, job["i"])]))
            self._tp_landed, self._tp_landed_unflag = (now, px, py, pad), False
            return end("jumped")
        # no fixed time: 24 orders at ~26 s each is ~11 min (a 10 min limit quit at order 21 of
        # 24, the right one still to come: Prophet's Path 00:28). Given up only when no order
        # gets finished for 3 min, or after 30 min in all.
        if now - job.get("order_t", job["t0"]) > 180.0:
            return end("gave_up_stalled")
        if now - job["t0"] > 1800.0:
            return end("gave_up_time")
        close = [e for e in eng.mem.enemies.values()
                 if e.alive and math.hypot(e.xy[0] - px, e.xy[1] - py) < 900.0]
        if bool(self.blackboard.get("COMBAT_ACTIVE", False)) and close:
            if job["phase"] != "flag":                   # fight with the heroes, then start over
                try:
                    game.unflag_heroes(game.hero_agent_ids())
                except Exception:
                    pass
                job["phase"] = "flag"
            return False
        self._idle_at = now
        if now - job.get("said", 0.0) >= 5.0:
            job["said"] = now
            s.log.event("tp_job", phase=job["phase"], order=job["i"], switch=job.get("step"), pad_gap=round(gap),
                        walk=(job.get("walk") or {}).get("mode"), gather=job.get("gather"))
        def flag_away():
            # beyond the pad, on the side away from the switches: off the pad and the switches
            # while the order is tried (another body on them spoils it), yet close to the pad
            cx = sum(x for _a, x, _y in job["sw"]) / 4.0
            cy = sum(y for _a, _x, y in job["sw"]) / 4.0
            dx, dy = pad[1] - cx, pad[2] - cy
            d = math.hypot(dx, dy) or 1.0
            try:
                game.flag_all_heroes(pad[1] + dx / d * 450.0, pad[2] + dy / d * 450.0)
            except Exception:
                pass

        if job["phase"] == "flag":
            flag_away()
            job["phase"], job["t"], job["gather"] = "pad", now, False
            return True
        if job["phase"] == "pad":
            if gap > 1200.0:
                # far off (the edge below the shelf to the pad: 6000+): walk the way there
                mv = job.get("mover")
                if mv is None:
                    mv = job["mover"] = BT.Move(pos=(float(pad[1]), float(pad[2])), tolerance=300.0)
                    job["mover_t"] = now
                    job.setdefault("best", gap)
                    job.setdefault("best_t", now)
                mv.root.blackboard = self.blackboard
                st = mv.root.tick()
                if gap < job.get("best", 1e9) - 200.0:
                    job["best"], job["best_t"] = gap, now
                if st != BehaviorTree.NodeState.RUNNING and now - job.get("mover_t", now) < 1.5 and gap > 1500.0:
                    job["refused"] = job.get("refused", 0) + 1
                    if job["refused"] >= 3:
                        return end("pad_not_walkable")   # the wrong side of the gap: try another
                if st != BehaviorTree.NodeState.RUNNING or (now - job.get("best_t", now) > 25.0
                                                            and now - job.get("mover_t", now) > 25.0):
                    job["mover"] = None                  # finished or stuck: start the walk again
                    if now - job.get("best_t", now) > 90.0:
                        return end("cannot_reach_pad")
                return True
            job["mover"] = None
            got = self._tp_walk(eng, s, job, pad[1], pad[2], 60.0, now, "pad")
            if got == "stuck":
                return end("cannot_get_on_pad")
            if not got:
                return True
            job["phase"], job["t"], job["on_pad"] = "wait", now, True
            return True
        if job["phase"] == "wait":
            if now - job["t"] < 3.0:
                return True
            if job.get("gather") and now - job["t"] < 12.0:
                # the order is in: everyone onto the pad with the leader, so the heroes go too
                try:
                    far = [h for h in game.hero_positions() if math.hypot(h[0] - pad[1], h[1] - pad[2]) > 350.0]
                except Exception:
                    far = []
                if far:
                    return True
            if job["i"] >= 0:
                job["done"].add(job["perms"][job["i"]])  # tried, did not take
            job["order_t"] = now
            job["i"] += 1                                # not taken: the next order
            if job["i"] >= len(job["perms"]):
                return end("gave_up_all_orders")
            if job.get("gather"):
                flag_away()                              # off the pad and switches again
                job["gather"], job["sw_after"] = False, now + 3.0
            job["phase"], job["step"], job["on_pad"] = "sw", 0, False
            return True
        if job["phase"] == "sw":
            # Click the switch the way a player does: the game walks up to it and uses it.
            # (Walking to within 180 first hung for good at switch 754, which cannot be stood
            # closer to than ~230: Prophet's Path 00:01.) Done when the leader has stopped
            # beside it after a click, or after 8 s whatever happened.
            if now < job.get("sw_after", 0.0):
                return True                              # heroes still walking off
            k = job["perms"][job["i"]][job["step"]]
            agent, sx, sy = job["sw"][k]
            here = [g for g in self.__dict__.get("_tp_gadgets", []) if math.hypot(g[2] - sx, g[3] - sy) < 150.0]
            if here:
                agent = min(here, key=lambda g: math.hypot(g[2] - sx, g[3] - sy))[0]
            d = math.hypot(px - sx, py - sy)
            if job.get("sw_t") is None:
                job["sw_t"], job["sw_last"], job["sw_still"] = now, (px, py), now
            if d > 900.0:                                # far (fought away): walk back near first
                if self._tp_walk(eng, s, job, sx, sy, 600.0, now, "switch") == "stuck":
                    return end("cannot_reach_switch")
                job["sw_t"] = now
                return True
            if now - job.get("used", 0.0) >= 1.5:
                game.use_gadget(agent)                   # a click, no dialog
                job["used"] = now
                job["clicks"] = job.get("clicks", 0) + 1
            if math.hypot(px - job["sw_last"][0], py - job["sw_last"][1]) > 25.0:
                job["sw_last"], job["sw_still"] = (px, py), now
            stopped = now - job["sw_still"] >= 1.0 and d < 450.0 and job.get("clicks", 0) >= 2
            if stopped or now - job["sw_t"] > 8.0:
                if not stopped:
                    s.log.event("teleporter", switch_slow=k, dist=round(d))
                job["step"] += 1
                job["sw_t"], job["clicks"] = None, 0
                if job["step"] >= 4:
                    job["phase"], job["gather"] = "pad", True
                    try:
                        game.unflag_heroes(game.hero_agent_ids())   # follow the leader onto the pad
                    except Exception:
                        pass
            return True
        return False

    def _go_back(self, eng, s, now, xy, why):
        """Walk at once to the last place on foot that was safe, at least 700 from `xy`
        (else the engine's way clear). Uses the step-clear slot, so the plan resumes after."""
        pt = None
        for x, y in reversed(self.__dict__.get("_trail", [])):
            if math.hypot(x - xy[0], y - xy[1]) >= 700.0:
                pt = (x, y)
                break
        if pt is None:
            try:
                pt = eng.escape_point(eng.player_xy, xy, goal=None)
            except Exception:
                pt = None
        if pt is None:
            s.log.event("go_back", why=why, ok=False)
            return False
        self._drop_child()
        self._child = BT.Move(pos=(float(pt[0]), float(pt[1])), tolerance=150.0)
        self._key, self._target, self._started = ("escape", int(now)), (pt[0], pt[1]), now
        self._escape_until = now + 12.0
        self._progress_at, self._progress_xy, self._progress_kills = now, None, -1
        s.log.event("go_back", why=why, to=[round(pt[0]), round(pt[1])], frm=[round(xy[0]), round(xy[1])])
        return True

    def _hands_off(self, s, off, why):
        """HeroAI off the leader's fighting while the bot moves him itself (on: off=True), and
        back on. Kept on the session so a rebuilt step, or any way out, puts it back."""
        if bool(off) == bool(s.__dict__.get("_heroai_off")):
            return
        try:
            ok = game.leader_combat(not off)
        except Exception as e:
            ok = repr(e)
        s._heroai_off = bool(off)
        s.log.event("leader_combat", heroai=not off, why=why, ok=ok)

    def _hands_back_if_done(self, s, now):
        """Safety: HeroAI's hands off only while a pillar step or a step clear is under way."""
        if not s.__dict__.get("_heroai_off"):
            return
        k = self._key
        if k is not None and k[0] == "escape" and now < self._escape_until + 1.0:
            return
        if k is not None and k[0] == "action" and self.__dict__.get("_skill_step"):
            return
        self._hands_off(s, False, "done")

    def _riding(self):
        """Mounted (junundu wurms): the party is a pack of wurms that goes in together. The
        on-foot caution is switched off for as long as it rides: no parking the heroes while
        the leader fetches a group, no leader stepping back out of the fight, no standing still
        for the leader's own health (the wurms' skills see to that as they go). Those had the
        leader and the heroes fighting apart, and cost more waiting than fighting."""
        return bool(getattr(self.__dict__.get("_ride_ground"), "riding", False))

    def _save_slot(self, a, s, save):
        """A route step that uses an adrenaline skill needs it charged on arrival: from a few
        route points before, HeroAI is kept off that slot so the fights on the way fill it;
        given back when the step is over."""
        held = self.__dict__.setdefault("_slots_saved", {})
        skill = int(a.get("value") or 0)
        if save == (skill in held):
            return
        try:
            slot = held.get(skill) or game.skill_state(skill)["slot"]
            if not slot:
                return
            ok = game.heroai_use_slot(slot, not save)
            if save:
                held[skill] = slot
            else:
                held.pop(skill, None)
            s.log.event("route_action", stage="saving_skill" if save else "skill_released", skill=skill, slot=slot, ok=ok)
        except Exception as e:
            s.log.event("route_action", stage="save_failed", skill=skill, error=repr(e))

    def _skill_action(self, a, s, now, fin):
        """At the spot: use the skill, once a second, until the bar shows it was spent (its
        adrenaline fell or it is recharging). True while still at it.

        An adrenaline skill that is not charged is charged first. Adrenaline comes from weapon
        hits and from damage taken; a junundu's own skills (Strike, Bite, Siege) give none, and
        it drains after 25 s out of a fight (Guild Wars Wiki). So with `charge_with` "attack":
        go and hit the nearest enemy within `charge_reach` with plain attacks until the skill
        shows charged, then straight back onto the spot and use it, inside those 25 s.
        Gives up after `give_up` s (60); the route then holds the step until a fight on the way
        charges it (`deferred`, `charged_go`)."""
        skill = int(a.get("value") or 0)
        st = self.__dict__.get("_skill_try")
        if st is None:
            st = self._skill_try = {"t": now, "next": now, "tries": 0, "hits": 0, "misses": 0, "peak": 0,
                                    "before": game.skill_state(skill), "targets": [], "charged_at": None,
                                    "rises": [], "last_adr": 0, "progress": now, "kills0": self._kills_safe()}
        state = game.skill_state(skill)
        adr, need = state.get("adrenaline") or 0, state.get("need") or 0
        used = bool(st["tries"]) and (st["peak"] - adr >= max(1.0, need * 0.5) or (state.get("recharge") or 0) > 0)
        if adr > st["last_adr"]:
            # what raised it: which skill the leader was casting, or a plain attack (learning
            # which junundu actions build adrenaline, as no source we have says for sure)
            cast, attacking = game.leader_action()
            if len(st["rises"]) < 25:
                st["rises"].append([round(now - st["t"], 1), adr, cast, attacking])
            st["progress"] = now
        st["last_adr"] = adr
        kills = self._kills_safe()
        if kills is not None and st["kills0"] is not None and kills > st["kills0"]:
            st["kills0"], st["progress"] = kills, now      # killing the ones behind the pillar counts too
        st["peak"] = max(st["peak"], adr)
        pos = a.get("pos")
        px, py = game.player_xy()
        # Keep at it while it gets somewhere: adrenaline rising or foes falling (those stuck
        # behind the pillar can be fought from the sulfur below). 60 s with neither; 300 s at most.
        stalled = now - st["progress"] > float(a.get("give_up", 60.0))
        if not used and state["slot"] and not stalled and now - st["t"] < 300.0:
            if now < st["next"]:
                return True
            charged = state.get("charged")
            if charged is False and a.get("charge_with") == "attack":
                # Charge from the spot itself: plain attacks (each hit one strike, 25 of the 100;
                # being hit fills it too) on a foe in reach FROM HERE - with a bow or spear held,
                # the wurm attacks at range, so the foes up on the plateau will do (Guild Wars
                # Wiki). Never walked after: that dragged it off the spot into the obstruction.
                if "ranged" not in st:
                    st["ranged"] = bool(game.holds_ranged())
                reach = 1150.0 if st["ranged"] else 260.0
                st["next"] = now + 0.5
                if pos and math.hypot(px - pos[0], py - pos[1]) > 250.0:
                    if st.get("foe"):
                        st.setdefault("passed", []).append(st["foe"])   # it pulled us off: not that one
                        st["foe"] = None
                    game.move_to(pos[0], pos[1])
                    return True
                if st.get("foe") and now - st.get("foe_since", now) > 15.0 and now - st["progress"] > 15.0:
                    st.setdefault("passed", []).append(st["foe"])
                    st["foe"] = None
                foe = game.nearest_enemy(reach, skip=st.get("passed", ()))
                if foe is None and st.get("passed"):
                    st["passed"] = []
                    foe = game.nearest_enemy(reach)
                if foe is None:
                    st["misses"] += 1                # nothing in reach: hold the spot, the fights come to us
                elif foe[0] != st.get("foe") or not game.is_attacking() or now - st.get("ordered", 0.0) > 6.0:
                    # a fresh order restarts the swing: order once, again only when it stopped
                    if foe[0] != st.get("foe"):
                        st["foe_since"] = now
                    game.attack(foe[0])
                    st["foe"], st["ordered"] = foe[0], now
                    st["hits"] += 1
                    if len(st["targets"]) < 5 and list(foe) not in st["targets"]:
                        st["targets"].append(list(foe))
                return True
            if charged and st["charged_at"] is None:
                st["charged_at"] = now
            if not (charged is False and a.get("charge_with")):
                if pos and math.hypot(px - pos[0], py - pos[1]) > 120.0:
                    # never anywhere else: only right under the pillar does it break it. Back
                    # onto the spot first (charging took us off it); stalls end the try.
                    st["walks"] = st.get("walks", 0) + 1
                    game.move_to(pos[0], pos[1])
                    st["next"] = now + 0.7
                    return True
                if st["tries"] < 20:
                    st["next"], st["tries"] = now + 1.0, st["tries"] + 1
                    game.use_skill_id(skill)
                    return True
        s.log.event("route_action", stage="skill", skill=skill, used=used, tries=st["tries"], before=st["before"],
                    after=state, peak=st["peak"], attack_orders=st["hits"], nothing_to_hit=st["misses"],
                    targets=st["targets"], back_after=None if st["charged_at"] is None else round(now - st["charged_at"], 1),
                    rises=st["rises"], stalled=stalled, ranged=st.get("ranged"),
                    seconds=round(now - st["t"], 1), player=[round(px), round(py)], riding=game.is_junundu(),
                    gadgets_near=self._gadgets_for_log(px, py))
        self._skill_try = None
        self._skill_used = used
        self._save_slot(a, s, False)
        return False

    @staticmethod
    def _kills_safe():
        try:
            return int(game.foes_killed())
        except Exception:
            return None

    def _gadgets_for_log(self, px, py):
        """What stands near a place the party could not get past: [agent, gadget id, x, y]. A
        gap is sometimes a thing to be worked first (Sulfurous Wastes: a pillar a wurm knocks
        over into a bridge); this is how the log learns what and where."""
        try:
            return [list(g) for g in game.gadgets_near(px, py, 2500.0)[:20]]
        except Exception as e:
            return [repr(e)]

    def _wurm_heal(self, s, now):
        """Riding, nothing hostile near, someone hurt: have the hurt wurms heal themselves with
        Junundu Wail and stand the 3 s it takes to cast (walking on cancels the leader's).
        Each caster at most once per recharge. True while the party should hold still."""
        if bool(self.blackboard.get("COMBAT_ACTIVE", False)) or now - self.__dict__.get("_wail_look", 0.0) < 1.0:
            return False
        self._wail_look = now
        told = self.__dict__.setdefault("_wail_told", {})
        try:
            cast = game.junundu_wail_heal(skip=[who for who, t in told.items() if now - t < 11.0])
        except Exception as e:
            if not self.__dict__.get("_wail_broken"):
                self._wail_broken = True
                s.log.event("wail_heal", stage="failed", error=repr(e))
            return False
        if not cast:
            return False
        for who, _hp in cast:
            told[who] = now
        self._hold_until = now + 3.4
        s.log.event("wail_heal", who=[list(c) for c in cast])
        return True

    def _ride_watch(self, eng, s, acts, now):
        """Keep the record of ground crossed as a wurm (core/mounts.py). None on routes with no
        burrow."""
        ground = self.__dict__.get("_ride_ground")
        if ground is None:
            if not any(a.get("do") == "gadget" for a in acts):
                return None
            # One record per visit: kept on the engine, so a step rebuilt after a wipe (revived at
            # a shrine) still knows the sulfur. A fresh one forgot it and walked onto it on foot.
            ground = eng.__dict__.get("_ride_ground_kept")
            if ground is None:
                ground = eng._ride_ground_kept = RideGround()
                ground.ridden.update(s.__dict__.pop("_resume_ridden", None) or ())     # kept across a reload
                try:
                    # where the sulfur is, from earlier runs: known from the first step
                    cells, levels = ride_store.load(game.map_id())
                    ground.absorb(cells, levels)
                    s.log.event("route_action", stage="ground_known", cells=len(cells), levels=len(levels))
                except Exception as e:
                    s.log.event("route_action", stage="ground_known", error=repr(e))
                for c, planes in (s.__dict__.pop("_resume_levels", None) or ()):
                    ground.levels[tuple(c)] = set(planes)
            self._ride_ground = s.ride_ground = ground
        s.ride_ground = ground               # where the session saves it from, for a reload
        swap = s.__dict__.get("_weapon_swap")
        if swap is not None:
            if game.is_junundu():
                swap["rode"] = True
            elif swap["rode"]:
                try:
                    game.equip_items(swap["held"])
                    s.log.event("weapon_swap", restored=swap["held"])
                except Exception as e:
                    s.log.event("weapon_swap", restored=False, error=repr(e))
                s._weapon_swap = None
        if now - self.__dict__.get("_ride_seen", 0.0) >= 0.3:
            self._ride_seen = now
            fresh = now - self.__dict__.get("_revived_at", 0.0) < 15.0     # lost by dying, not set down
            changed = len(ground.ridden)
            off = ground.note(eng.player_xy, game.is_junundu(), alive=not fresh,
                              plane=getattr(eng, "player_plane", None))
            if len(ground.ridden) != changed or off:
                self._ground_dirty = True
            if self.__dict__.get("_ground_dirty") and (off or now - self.__dict__.get("_ground_saved", 0.0) >= 60.0):
                self._ground_dirty, self._ground_saved = False, now
                ride_store.save(game.map_id(), ground)
            if off == "off":
                s.log.event("route_action", stage="set_down", player=[round(eng.player_xy[0]), round(eng.player_xy[1])],
                            ridden_cells=len(ground.ridden))
        return ground

    def _start_route_action(self, i, a, now, s):
        do, v = a["do"], a.get("value")
        if do == "gadget":
            # First walk up to it and bring the heroes in (a hero still far behind when the
            # leader used a junundu burrow was left on foot); the using comes after that.
            self._gadget_phase = "walk"
            self._gather_since = None
            way = a.get("way")
            ground = self.__dict__.get("_ride_ground")
            if way is None and ground is not None and ground.ridden and not game.is_junundu():
                try:
                    eng_ = self.session.engine
                    pts, safe, _l = ground.foot_way(eng_.nav, game.player_xy(), tuple(v),
                                                    avoid=[(hx, hy, eng_.cfg.hazard_radius) for hx, hy in getattr(eng_, "hazards", ())])
                    way = pts if pts else None
                except Exception:
                    way = None
            self._gadget_way = way
            child = BT.Move(pos=[tuple(p) for p in way], tolerance=250.0) if way else BT.Move(pos=tuple(v), tolerance=250.0)
        elif do == "npc":
            child = BT.MoveAndInteract(pos=tuple(v), log=True)
        elif do == "dialog":
            child = BT.SendDialog(dialog_id=int(v), multi_account=False, log=True)
        elif do == "wait":
            child = BT.Wait(duration_ms=int(v))
        elif do == "skill":
            # use a skill of the bar at a spot (a wurm's Junundu Smash by the pillar that falls
            # into a bridge): walk right up to the spot first, the using is done on arrival
            if a.get("riding") and not game.is_junundu():
                # needs a wurm: go and get one first (nearest burrow reached on foot without
                # crossing sulfur), then this step comes round again. Skipped only after 3 tries.
                tries = self.__dict__.setdefault("_skill_mount_tries", {})
                tries[i] = tries.get(i, 0) + 1
                ground = self.__dict__.get("_ride_ground")
                eng_ = self.session.engine
                pick = None
                if ground is not None and tries[i] <= 3:
                    failed = self.__dict__.setdefault("_mount_failed", {})
                    mounts = list(dict.fromkeys(tuple(b["value"]) for b in eng_.guide_actions
                                                if b.get("do") == "gadget" and failed.get(tuple(b["value"]), 0) < 2))
                    avoid = [(hx, hy, eng_.cfg.hazard_radius) for hx, hy in getattr(eng_, "hazards", ())]
                    pick = ground.safest_mount(eng_.nav, eng_.player_xy, mounts, avoid=avoid)
                if pick is not None:
                    spot, way, safe = pick
                    s.log.event("route_action", stage="mount_for_skill", at=a.get("at"), burrow=list(spot),
                                safe=safe, attempt=tries[i])
                    self._remount_at = now
                    return self._start_route_action(-1, {"do": "gadget", "value": list(spot), "at": a.get("at"),
                                                         "way": way}, now, s)
                self.__dict__.setdefault("_actions_done", set()).add(i)
                s.log.event("route_action", stage="skipped", do=do, value=v, at=a.get("at"), why="not riding",
                            tries=tries[i])
                self._save_slot(a, s, False)
                return False
            child = BT.Move(pos=tuple(a["pos"]), tolerance=120.0) if a.get("pos") else BT.Wait(duration_ms=100)
            # the walk to the spot and all at it are the bot's: HeroAI's wurm build drags the
            # leader at the nearest foe in earshot, up on the plateau, off the spot
            self._hands_off(s, True, "pillar")
        elif do == "dropbundle":
            child = BT.DropBundle(log=True)
        else:
            self.__dict__.setdefault("_actions_done", set()).add(i)
            s.log.event("route_action", stage="unsupported", do=do, at=a.get("at"))
            return False
        self._drop_child()
        self._child = child
        self._skill_step = do == "skill"
        self._key, self._target, self._started = ("action", i), (tuple(v) if isinstance(v, (list, tuple)) else None), now
        self._progress_at, self._progress_xy, self._progress_kills = now, None, -1
        s.log.event("route_action", stage="start", do=do, value=v, at=a.get("at"), remount=(i == -1),
                    riding=game.is_junundu())
        return True

    def _gather_heroes(self, s, now):
        """Walk phase of a gadget step is over: hold the heroes on the spot until each living one
        is within reach of it (up to 12 s), then swap the walk for the using. Returns True."""
        spot = self._target
        if spot is None:
            self._use_gadget(s, now, 0.0, [])
            return True
        if self._gather_since is None:
            self._ranged_for_coming_skill(s)
            # the walk ended short of it: the using would let the game walk the rest its own
            # way, straight over whatever lies between. Walk our way again (twice), else stop.
            px, py = game.player_xy()
            short = math.hypot(px - spot[0], py - spot[1])
            if short > 600.0:
                n = self.__dict__.get("_walk_retries", 0)
                s.log.event("route_action", stage="walk_short", at=list(spot), short_by=round(short), retry=n)
                if n < 2:
                    self._walk_retries = n + 1
                    a = {"do": "gadget", "value": list(spot), "at": None}     # way worked out afresh from here
                    i = self._key[1]
                    self._start_route_action(i, a, now, s)
                    return True
                self._walk_retries = 0
                self._action_fin = (now, False)
                self._gadget_phase = None
                return True
            self._walk_retries = 0
            self._gather_since = now
            try:
                game.flag_all_heroes(spot[0], spot[1])
            except Exception:
                pass
        far = [xy for xy in game.hero_positions() if math.hypot(xy[0] - spot[0], xy[1] - spot[1]) > 450.0]
        if far and now - self._gather_since < 12.0:
            self._idle_at = now              # waiting on purpose, not stuck
            return True
        try:
            game.unflag_heroes(game.hero_agent_ids())
        except Exception:
            pass
        self._use_gadget(s, now, now - self._gather_since,
                         [round(math.hypot(xy[0] - spot[0], xy[1] - spot[1])) for xy in far])
        return True

    def _ranged_for_coming_skill(self, s):
        """About to become a wurm with a skill step ahead that charges by attacking (the pillar):
        take a bow or spear in hand first, so the wurm attacks at range (each hit a strike of
        adrenaline, foes up on a ledge in reach). Put back once on foot again."""
        if s.__dict__.get("_weapon_swap"):
            return
        eng = s.engine
        acts = getattr(eng, "guide_actions", None) or ()
        nxt = eng.next_guide_src()
        done = self.__dict__.get("_actions_done", set())
        soon = [a for i, a in enumerate(acts) if a.get("do") == "skill" and a.get("charge_with") == "attack"
                and i not in done and a["at"] - nxt <= 15]
        if not soon:
            return
        try:
            held, ranged = game.equip_ranged_weapon()
        except Exception as e:
            s.log.event("weapon_swap", ok=False, error=repr(e))
            return
        if held is None:
            s.log.event("weapon_swap", note="a bow or spear is held already")
            return
        if ranged is None:
            s.log.event("weapon_swap", ok=False, note="no bow or spear in the bags: the wurm attacks in melee "
                        "only and the pillar must be charged from the fights near it (carry any bow)")
            return
        s._weapon_swap = {"held": held, "rode": False}
        s.log.event("weapon_swap", ok=True, ranged=ranged, held_before=held)

    def _use_gadget(self, s, now, waited, far):
        spot = self._target
        if self._child is not None:
            retire(self._child)
        self._child = BT.MoveAndInteractWithGadget(pos=tuple(spot), interaction_count=1, multi_account=False, log=True)
        self._gadget_phase = "use"
        self._action_fin = None
        self._started = now
        s.log.event("route_action", stage="gathered", at=list(spot), waited=round(waited, 1),
                    still_far=far, retry=bool(self.__dict__.get("_remount_retry")))

    def _mount_stragglers(self, s, now, waited):
        """Some heroes are still on foot after the leader became a wurm. After 6 s send each
        of them to the burrow; at 18 s, once per burrow visit, leave the wurm and use the burrow
        again with them beside it (heroes take a wurm when the leader does, if close).
        Returns True while that is in hand."""
        spot = self._target
        if spot is None:
            return False
        walkers = game.heroes_on_foot()
        if waited >= 6.0 and not self.__dict__.get("_stragglers_flagged"):
            self._stragglers_flagged = True
            for agent, _xy in walkers:
                try:
                    game.flag_hero(agent, spot[0], spot[1])
                except Exception:
                    pass
            s.log.event("route_action", stage="stragglers", on_foot=[
                {"dist": round(math.hypot(xy[0] - spot[0], xy[1] - spot[1]))} for _a, xy in walkers])
        if waited >= 18.0 and walkers and not self.__dict__.get("_remount_retry"):
            near = all(math.hypot(xy[0] - spot[0], xy[1] - spot[1]) < 600.0 for _a, xy in walkers)
            if not near and waited < 30.0:
                return True                  # still walking up; give them a little longer
            self._remount_retry = True
            slot = game.use_skill_id(game.LEAVE_JUNUNDU)
            s.log.event("route_action", stage="remount_retry", left_wurm=bool(slot), on_foot=len(walkers))
            self._stragglers_flagged = False
            try:
                game.unflag_heroes(game.hero_agent_ids())
            except Exception:
                pass
            self._gather_since = now - 12.0  # already gathered
            self._use_gadget(s, now, 0.0, [])
            self._gadget_phase = "use"
            self._hold_until = now + 2.0      # let the leave finish before the burrow is used
            return True
        return False

    def _route_action_done(self, eng, s, now, ok):
        self._remount_retry = False
        self._skill_step = False
        self._gadget_phase = None
        i = self._key[1]
        acts = eng.guide_actions
        a = acts[i] if 0 <= i < len(acts) else {"do": "gadget"}
        deferred = eng.__dict__.setdefault("_route_deferred", {})
        unused = a["do"] == "skill" and self.__dict__.pop("_skill_used", None) is False
        if i >= 0 and unused and deferred.get(i, 0) < 3 and self._riding():
            # not charged at the spot (nothing in reach to hit): not given up. It is used as
            # soon as a fight on the way charges it while the spot is near (`charged_go`).
            deferred[i] = deferred.get(i, 0) + 1
            s.log.event("route_action", stage="deferred", at=a.get("at"), skill=a.get("value"), times=deferred[i])
            self._save_slot(a, s, True)
        elif i >= 0:
            deferred.pop(i, None)
            self.__dict__.setdefault("_actions_done", set()).add(i)
        riding = game.is_junundu()
        if a["do"] == "gadget":
            if riding:
                self._ride_from = tuple(a.get("value") or self._target or (0, 0))
                ground = self.__dict__.get("_ride_ground")
                if ground is not None:
                    ground.note(eng.player_xy, True, plane=getattr(eng, "player_plane", None))   # known at once
                if "_pull" in self.__dict__:
                    self._pull["phase"] = None
                try:
                    game.unflag_heroes(())                   # nobody is left parked from a pull on foot
                except Exception:
                    pass
                s.log.event("route_action", stage="pack_mode", note="riding: pulls, fall-backs and health rests are off")
            elif i == -1 and self._target is not None:
                failed = self.__dict__.setdefault("_mount_failed", {})   # twice no wurm: not a burrow
                failed[tuple(self._target)] = failed.get(tuple(self._target), 0) + 1
            try:
                game.refresh_combat_builds(riding)
            except Exception as e:
                s.log.event("route_action", stage="build_refresh_failed", error=repr(e))
        s.log.event("route_action", stage="done", do=a["do"], ok=ok, riding=riding,
                    seconds=round(now - self._started, 1))

    def _loop_check(self, key, x, y):
        """Steps that keep going round the same few points: the map has two levels stacked here
        and our path hops between them on paper while the party stays put (Bahdok Caverns, one
        foe left: three points, every second, for minutes). First let the game's own pathing
        walk at the target; if it loops again on the same objective, give that objective up."""
        now, eng = time.time(), self.session.engine
        hist = self.__dict__.setdefault("_step_hist", [])
        try:
            kills = game.foes_killed()
        except Exception:
            kills = 0
        if bool(self.blackboard.get("COMBAT_ACTIVE", False)):
            hist.clear()                     # stepping about in a fight is not a loop
            return
        hist.append((now, round(x / 150.0), round(y / 150.0), eng.player_xy, kills))
        while hist and now - hist[0][0] > 60.0:
            hist.pop(0)
        if getattr(self, "_direct", None) is not None:
            return
        # A wider circuit: the same objective for 90 s, no kill, and never 300 nearer to it than
        # before (map 13: seven points round a ridge, 11 minutes, the region never reached).
        obj = eng.objective
        if obj is not None:
            ox, oy = obj.pos[0], obj.pos[1]
            d = math.hypot(eng.player_xy[0] - ox, eng.player_xy[1] - oy)
            # Progress by the way still to walk, not the straight line: going round a loop that
            # swings 1,000 east and west of the line "got nearer" on every swing and the check
            # never fired (Sage Lands: up and down one ramp for two minutes, way left 43-45 k).
            ra = getattr(eng, "route_ahead", None) or []
            if len(ra) >= 2 and math.hypot(ra[-1][0] - ox, ra[-1][1] - oy) < 1500.0:
                d = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(ra, ra[1:]))
            prog = self.__dict__.get("_obj_prog")
            if prog is None or prog[0] != key or prog[4] != kills or d > prog[1] + 3000.0:
                # (a new plan much longer than the best so far - a join was cut, a wall put up -
                # starts the count again: measured against the old, shorter way, the objective
                # was given up 30 s after a cut, Sage Lands)
                prog = self._obj_prog = [key, d, now, now, kills]
            elif d < prog[1] - 300.0:
                prog[1], prog[2] = d, now
            if now - prog[2] > 90.0 and d > 900.0:
                self._obj_prog = None
                hist.clear()
                self._loop_found(eng, key, eng.player_xy[0], eng.player_xy[1], 0, now, why="no nearer in 90 s")
                return
        if len(hist) < 12:
            return
        spots = {(h[1], h[2]) for h in hist}
        px = sum(h[3][0] for h in hist) / len(hist)
        py = sum(h[3][1] for h in hist) / len(hist)
        if (len(spots) > 4 or hist[0][4] != hist[-1][4]
                or any(math.hypot(h[3][0] - px, h[3][1] - py) > 900.0 for h in hist)):
            return
        hist.clear()
        self._loop_found(eng, key, px, py, len(spots), now)

    @staticmethod
    def _doors(eng):
        """Every place a walk must keep its distance from, less the portal props nothing backs
        up as a door (eng.doubted: they keep a small ring only)."""
        doubted = getattr(eng, "doubted", None) or ()
        out = (list(getattr(eng, "portals", ())) + [tuple(e[:2]) for e in getattr(eng, "exits", ())]
               + [tuple(e[:2]) for e in getattr(eng, "removed_exits", ())]
               + [tuple(d[:2]) for d in (getattr(eng, "doors", None) or ())])
        if doubted:
            out = [d for d in out if not any(abs(d[0] - q[0]) < 50.0 and abs(d[1] - q[1]) < 50.0 for q in doubted)]
        return out

    def _direct_unsafe(self, eng, pos):
        """A walk left to the game's own pathing goes where the game likes, doors included: not
        at a spot near a door, nor along a line over one. (Ettin's Back: steps to a map cell were
        cut short at a doorway, the loop handed the walk to the game, and it walked out.)"""
        from ..core.geometry import segment_clear_of
        doors = self._doors(eng)
        if not doors or self.transit is not None:
            return False
        if any(math.hypot(pos[0] - d[0], pos[1] - d[1]) < 1000.0 for d in doors):
            return True
        return not segment_clear_of(eng.player_xy, pos, doors, 450.0)

    def _direct_start(self, eng, obj, key, now):
        """Let the game's own pathing walk at the objective. Where the area's known route runs
        from near here to near it, walk that stretch of the route point by point first (it is
        the way people go)."""
        if self._direct_unsafe(eng, obj.pos):
            eng.mem.blocked[key] = eng.cfg.max_failures
            if eng.objective is not None and eng.objective.key == key:
                eng.objective = None
            self.session.log.event("step_loop", key=key, gave_up=True, why="a walk left to the game would pass a door")
            return
        from ..core.geometry import route_chain
        chain = route_chain(getattr(eng, "_route_src", None), eng.player_xy, obj.pos)
        self._direct_chain = [tuple(p) for p in chain] + [(obj.pos[0], obj.pos[1])] if chain else None
        self._direct = (now + (30.0 if chain else 40.0), (obj.pos[0], obj.pos[1]), key)
        self._direct_hard = now + 60.0 + 30.0 * len(chain)      # the whole walk, fights and all
        self.session.log.event("step_loop", key=key, direct_walk="start", via_route=len(chain),
                               chain=[[round(x), round(y)] for x, y in chain][:8])

    def _loop_found(self, eng, key, px, py, spots, now, why="same few points"):
        """A step loop on this objective: first the game's own pathing at it for 40 s, the
        second time give the objective up."""
        obj = eng.objective
        loops = self.__dict__.setdefault("_loops", {})
        loops[key] = loops.get(key, 0) + 1
        self.session.log.event("step_loop", key=key, player=[round(px), round(py)], spots=spots, why=why,
                               times=loops[key], target=None if obj is None else [round(obj.pos[0]), round(obj.pos[1])])
        if eng.nav.stacked(px, py) and not any(math.hypot(px - wx, py - wy) < 300.0 for wx, wy in eng.walls):
            eng.walls.append((px, py))       # a climb the map allows and the game does not: route round it
            eng.player_node = None
        # A loop where the planned way changes level close by: the join is what cannot be
        # walked (a ramp the map joins at both ends, a branch the party cannot step off). Take
        # it out and plan again before anything else.
        try:
            cut = eng.cut_climb((px, py))
        except Exception:
            cut = None
        if cut is not None:
            self.session.log.event("climb_cut", player=[round(px), round(py)], at=[round(cut[0]), round(cut[1])],
                                   why=why, key=key)
            loops[key] -= 1                  # not counted against the objective: the map was wrong
            self._obj_prog = None
            self.session.note_climb_cut(cut)
            return
        if loops[key] == 1 and obj is not None:
            self._direct_start(eng, obj, key, now)
        else:
            eng.mem.blocked[key] = eng.cfg.max_failures
            eng.objective = None
            self.session.log.event("step_loop", key=key, gave_up=True)

    def get_children(self):
        return [self._child.root] if self._child is not None else []

    def _start_step(self, step, key):
        x, y, radius = step
        eng = self.session.engine
        # every step in the area we are working keeps its end off the doors, not only while foes
        # remain: walking for map cells after the last kill took the party out through a door
        # twice (53, 54), and the area was written down as left, not done
        # Escapes too: a "step clear" from the shrine in Diessa Lowlands walked the party out into
        # the outpost beside it.
        if (self.transit is None and eng is not None
                and not (eng.objective is not None and eng.objective.kind == "goto")):
            from ..core.geometry import keep_off_doors, segment_clear_of
            doors = self._doors(eng)
            nx, ny = keep_off_doors((x, y), eng.player_xy, doors)
            if (nx, ny) != (x, y):
                self.session.log.event("door_kept_off", key=key, wanted=[round(x), round(y)], now=[round(nx), round(ny)])
                x, y = nx, ny
            # The step end is clear of the doors, but the game walks to it in a straight line, and
            # that line can run over a doorway: 44, 26, 98 and 41 were all left that way, walking
            # to map cells across a door (the end 1,000-1,900 past it). Walk to the furthest point
            # of our own route that can be reached in a straight line clear of every door, or stop
            # short of the door on the line.
            px, py = eng.player_xy
            if doors and not segment_clear_of((px, py), (x, y), doors, 450.0):
                pick = None
                for q in (getattr(eng, "route_ahead", None) or [])[1:]:
                    if math.hypot(q[0] - px, q[1] - py) > math.hypot(x - px, y - py) + 200.0:
                        break
                    if segment_clear_of((px, py), q, doors, 450.0) and not any(
                            math.hypot(q[0] - d[0], q[1] - d[1]) < 700.0 for d in doors):
                        pick = q
                if pick is None or math.hypot(pick[0] - px, pick[1] - py) < 150.0:
                    L = math.hypot(x - px, y - py) or 1.0
                    t = 1.0
                    while t > 0.0 and not segment_clear_of((px, py), (px + (x - px) * t, py + (y - py) * t), doors, 450.0):
                        t -= 0.05
                    pick = (px + (x - px) * max(t, 0.0), py + (y - py) * max(t, 0.0))
                self.session.log.event("door_line_cut", key=key, wanted=[round(x), round(y)], now=[round(pick[0]), round(pick[1])])
                x, y = pick
            # Nothing left of the step: the way to this objective runs over a door from here.
            # Walking "to" our own spot was judged stalled at once and a wall was marked under
            # our feet, again and again (Frozen Forest: 3 minutes and 49 such steps by a door).
            # Put the objective aside instead; the plan picks another, and a later reset of the
            # given-up list brings it back.
            if (math.hypot(x - eng.player_xy[0], y - eng.player_xy[1]) < 150.0
                    and math.hypot(step[0] - eng.player_xy[0], step[1] - eng.player_xy[1]) > 300.0
                    and key and key[0] not in ("escape", "teleport", "step")):
                self.session.log.event("door_blocks_step", key=key, wanted=[round(step[0]), round(step[1])],
                                       player=[round(eng.player_xy[0]), round(eng.player_xy[1])])
                eng.mem.blocked[key] = eng.cfg.max_failures
                if eng.objective is not None and eng.objective.key == key:
                    eng.objective = None
                self._drop_child()
                self._key, self._target = None, None
                return
        # Map cells can be slivers of ground: stand on the exact spot, not somewhere near it.
        exact = self.session.engine.objective is not None and self.session.engine.objective.kind == "carto"
        tol = 150.0
        if exact:
            tol = 40.0
            try:
                depth = eng.carto.stand[tuple(eng.objective.key[1])][2]
                tol = max(10.0, min(40.0, depth * 0.7))   # a sliver at a cliff edge: stop right on it
            except Exception:
                pass
        self._child = BT.MoveAndKill(pos=(x, y), clear_area_radius=radius, move_tolerance=tol)
        self._key, self._target, self._started = key, (x, y), time.time()
        aim, way = None, None
        try:
            if eng is not None and eng.objective is not None and eng.objective.pos is not None:
                aim = [round(eng.objective.pos[0]), round(eng.objective.pos[1])]
                ra = getattr(eng, "route_ahead", None) or []
                way = round(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(ra, ra[1:])))
        except Exception:
            pass
        # where the objective is and how long the planned way to it is, at every step: a walk
        # back and forth (Kessex Peak, a minute short of the last groups) shows up here
        self.session.log.event("step_start", key=key, target=[round(x), round(y)], clear_radius=round(radius),
                               aim=aim, way=way)
        self._loop_check(key, x, y)
        # The stall clock restarts only for a different step. Restarting it for the same step
        # again and again hid a loop where each "walk" finished at once without moving.
        same = getattr(self, "_last_step", None) == (key, round(x), round(y))
        self._last_step = (key, round(x), round(y))
        self._same_steps = getattr(self, "_same_steps", 0) + 1 if same else 0
        if not same:
            self._progress_at, self._progress_xy, self._progress_kills = time.time(), None, -1

    # ---- raising the dead ----
    def _tick_impl(self):
        def give_up():
            self._drop_child()
            self.session.result = "stopped by repeated errors (see log)"
            return BehaviorTree.NodeState.FAILURE
        return guarded_tick(self, self.session, give_up)

    _stuck_since, _stuck_xy = 0.0, None

    _escape, _escape_until, _escape_secs, _escape_radius = None, 0.0, 6.0, 900.0

    def _pinned(self, eng, s, now, walking=False):
        """A step stalled. If the party keeps stalling in the same spot, something the ground data
        does not show is in the way (a shut gate): mark it so the route goes round."""
        px, py = eng.player_xy
        if self._stuck_xy is None or math.hypot(px - self._stuck_xy[0], py - self._stuck_xy[1]) > 600.0:
            self._stuck_xy, self._stuck_since, self._stuck_n = (px, py), now, 0
        self._stuck_n = getattr(self, "_stuck_n", 0) + 1
        # Caught while just walking (no enemy near): it is the scenery. Mark it at once and step
        # clear. In a fight, wait for a second stall in the same place before calling it a wall.
        # The known route is only brought in when the same place stops us twice.
        short = self._target is not None and math.hypot(px - self._target[0], py - self._target[1]) < 400.0
        if short and walking and self._stuck_n < 2:
            return                           # standing at the step's end, not caught on the way to it
        # The "two levels here" excuse holds twice. A third stall on the same spot is not the walk
        # aiming at the wrong level: it is scenery (Sage Lands: ten "stacked" stalls in one place,
        # the same step every 25 s, until the area was given up as pinned for 10 minutes).
        if (walking or self._stuck_n >= 2) and self._target is not None and self._stuck_n < 3 and eng.nav.stacked(px, py):
            # Two levels on top of each other here (a bridge): the walk was aimed at the other
            # level, nothing is in the way. Marking it walled the bridge off for good in Bahdok
            # Caverns. Let the game's own pathing take us at the objective instead.
            obj = eng.objective
            # First: is the planned way changing level right here? Then that join is the
            # trouble (the map joins a bridge or ramp to the ground beside it where no one can
            # climb): take it out and plan again, rather than send the walk at it once more.
            try:
                cut = eng.cut_climb((px, py))
            except Exception:
                cut = None
            if cut is not None:
                s.log.event("climb_cut", player=[round(px), round(py)], at=[round(cut[0]), round(cut[1])],
                            target=list(self._target), times_here=self._stuck_n)
                self._direct = None
                self._obj_prog = None
                self.session.note_climb_cut(cut)
                return
            # The map joins the two levels here but the party cannot climb between them: keep
            # paths off this spot for the rest of the visit (not saved: it is not scenery), so the
            # planner finds the real way up (Bahdok: the ramp round the west side).
            if not any(math.hypot(px - wx, py - wy) < 300.0 for wx, wy in eng.walls):
                eng.walls.append((px, py))
                if eng._wall_cuts((px, py), (px, py)):
                    eng.soft_walls.add((px, py))     # the only way through: care, not a wall
                eng.player_node = None
            if obj is not None and getattr(self, "_direct", None) is None and not self._direct_unsafe(eng, obj.pos):
                self._direct = (now + 25.0, (obj.pos[0], obj.pos[1]), self._key)
                self._direct_chain = None
            s.log.event("wall", player=[round(px), round(py)], target=list(self._target), stacked=True,
                        times_here=self._stuck_n, direct_walk=obj is not None)
            return
        if not walking and bool(self.blackboard.get("COMBAT_ACTIVE", False)) and self._stuck_n >= 2:
            # Held in a fight that goes nowhere: HeroAI keeps the leader on a foe it cannot reach
            # (one up on a ledge). That is not scenery: no wall (it walled the pillar spot off).
            # Step well clear with HeroAI's hands off, so the foe drops out of its sight.
            near = [(e.xy[0], e.xy[1]) for e in eng.mem.enemies.values()
                    if e.alive and e.in_range and math.hypot(e.xy[0] - px, e.xy[1] - py) < 1600.0]
            away = min(near, key=lambda q: math.hypot(q[0] - px, q[1] - py)) if near else None
            self._escape = eng.escape_point((px, py), away, goal=None) if away else None
            self._escape_secs, self._escape_radius, self._escape_hands_off = 10.0, 1500.0, True
            s.log.event("combat_stuck", player=[round(px), round(py)], times_here=self._stuck_n,
                        foes_near=len(near), step_clear=None if self._escape is None else
                        [round(self._escape[0]), round(self._escape[1])], riding=self._riding())
            return
        if (walking or self._stuck_n >= 2) and self._target is not None:
            spot = eng.add_wall((px, py), self._target, detour=self._stuck_n >= 2)
            if walking and eng.add_snag(spot):
                s.save_snags()               # scenery does not move: remember it for every later run here
            if self._stuck_n >= 3 and self._key is not None and self._key[0] not in ("escape", "step", "teleport"):
                # Stopped here three times on the way to the same thing, and the planner still
                # routes through (the spot is its only way): set that objective aside. Waiting
                # out the 10-minute "pinned" limit threw away the whole area instead.
                eng.mem.blocked[self._key] = eng.cfg.max_failures
                if eng.objective is not None and eng.objective.key == self._key:
                    eng.objective = None
                s.log.event("step_loop", key=self._key, gave_up=True, why="stopped 3 times at the same spot")
            away = spot or (eng.walls[-1] if eng.walls else None)
            self._escape = eng.escape_point((px, py), away, goal=self._target if walking else None)
            s.log.event("wall", player=[round(px), round(py)], target=list(self._target),
                        spot=None if spot is None else [round(spot[0]), round(spot[1])], walls=len(eng.walls),
                        walking=walking, times_here=self._stuck_n,
                        step_clear=None if self._escape is None else [round(self._escape[0]), round(self._escape[1])],
                        detour=bool(eng._detour), route_points=len(eng.guide) if eng._detour else 0,
                        only_way=spot is not None and spot in getattr(eng, "soft_walls", ()),
                        riding=self._riding(), gadgets_near=self._gadgets_for_log(px, py))

    def _energy_rate(self, now, energy):
        """Leader's energy refill per second (0-1 scale) out of combat, measured as he walks;
        0.012 (about 4 pips on 80 energy) until there is a measurement."""
        hist = self.__dict__.setdefault("_energy_hist", [])
        if bool(self.blackboard.get("COMBAT_ACTIVE", False)):
            hist.clear()                      # casting in a fight is not regeneration
        else:
            hist.append((now, energy))
            while hist and now - hist[0][0] > 12.0:
                hist.pop(0)
        rate = self.__dict__.get("_energy_rate_v", 0.012)
        if len(hist) >= 2 and hist[-1][0] - hist[0][0] >= 8.0 and hist[-1][1] > hist[0][1] and hist[-1][1] < 0.99:
            seen = (hist[-1][1] - hist[0][1]) / (hist[-1][0] - hist[0][0])
            rate = 0.7 * rate + 0.3 * max(0.003, min(0.05, seen))
            self._energy_rate_v = rate
        return rate

    def _tick_core(self):
        S, s = BehaviorTree.NodeState, self.session
        s.transit = self.transit
        if self.arrive_map is not None and game.map_ready() and game.map_id() == self.arrive_map:
            self._drop_child()
            return S.SUCCESS
        if self.target_map_id is not None and game.map_ready() and game.map_id() != self.target_map_id:
            self._drop_child()
            left = s.__dict__.get("last_left") or {}
            if left.get("map") != self.target_map_id:
                left = {}
            try:
                done_in_game = self.target_map_id in game.vanquished_ids()
            except Exception:
                done_in_game = False
            if self.transit is None and left.get("xy") and s.near_listed_door(left["xy"]):
                # walked out through a door mid-run: that point is a door, remember it. Only next
                # to a portal or arrival point the game lists: a map travel (by the queue or by
                # hand) also changes the map mid-run, and one such left a "door" in the middle of
                # Flame Temple Corridor whose ring cut the area in half (22 regions -> 9).
                try:
                    if s.learn_exit(self.target_map_id, left["xy"]):
                        s.log.event("door_learnt", map_id=self.target_map_id, at=[round(left["xy"][0]), round(left["xy"][1])])
                except Exception:
                    pass
            if getattr(self, "_vq_done", False) or done_in_game or (
                    self.transit is None and s.engine is not None and s.engine.cfg.do_vanquish
                    and s.engine.foes_remaining == 0) or (self.transit is None and left.get("foes") == 0
                                                          and getattr(s.cfg, "do_vanquish", True)):
                # stepped through a door while clearing the map edges after the vanquish: it counts
                s.result = "complete"
                s.log.event("finished", result=s.result, note="left the area after the vanquish was done")
                return S.SUCCESS
            s.result = "left the area (sent back to an outpost?)"
            s.log.event("finished", result=s.result)
            return S.FAILURE
        if not s.ensure_engine():
            self._drop_child()               # loading screen or map change: hold everything
            t = time.time()
            if game.map_ready() and getattr(s, "error", ""):
                # the map is up but its model cannot be built: not a loading screen. Give the
                # step up after a minute so the campaign moves on (it held 25 min in The Black
                # Curtain with nothing written down)
                self._build_fail_since = self.__dict__.get("_build_fail_since") or t
                if t - self._build_fail_since > 60.0:
                    s.result = s.error
                    s.log.event("finished", result=s.result)
                    return S.FAILURE
            else:
                self._build_fail_since = None
            self._no_map_since = getattr(self, "_no_map_since", 0.0) or t
            if t - self._no_map_since > 45.0 and not getattr(self, "_no_map_said", False):
                # far longer than any loading screen: the connection dropped. Reconnecting puts the
                # party back in the same instance and the run picks up from its saved state.
                self._no_map_said = True
                s.resumed = "no map for 45 s - disconnected? Reconnect and the run carries on from where it was."
                s.log.event("no_map", seconds=round(t - self._no_map_since), map_loading=game.map_loading_safe())
            return S.RUNNING
        # The game frozen with the map still up (connection dropped, the last frame stays):
        # the instance clock stops. Hold everything; stalls seen then are not walls (map 13:
        # 12 "walls" and 2 saved snags made while disconnected).
        t = time.time()
        up = game.instance_uptime()
        last = self.__dict__.get("_uptime_seen")
        if last is None or (up != last[0] and game.leader_connected()):
            if self.__dict__.get("_frozen_said"):
                s.log.event("connection_back", after=round(t - last[1]))
                self._frozen_said = False
            self._uptime_seen = (up, t)
        elif t - last[1] > 6.0:
            if not self.__dict__.get("_frozen_said"):
                self._frozen_said = True
                s.resumed = "the game stopped (connection lost?) - holding until it is back"
                s.log.event("connection_lost", frozen_for=round(t - last[1]), connected=game.leader_connected())
            self._idle_at = t
            self._progress_at = t
            return S.RUNNING
        if getattr(self, "_no_map_since", 0.0):
            if getattr(self, "_no_map_said", False):
                s.log.event("map_back", after=round(time.time() - self._no_map_since), map_id=game.map_id())
            self._no_map_since, self._no_map_said = 0.0, False
        if self.transit is not None and not getattr(self, "_door_checked", False):
            # The world map's door for this crossing must be on this map's own ground. Where the
            # world data is wrong (a door recorded in another area's coordinates) walking at it
            # sends the party across the map the wrong way: stop instead.
            self._door_checked = True
            eng = s.engine
            gx, gy = self.transit["xy"]
            i = eng.nav.nearest_node(gx, gy, allowed=eng.rm.reachable)
            gap = None if i is None else math.hypot(eng.nav.nodes[i][0] - gx, eng.nav.nodes[i][1] - gy)
            if gap is None or gap > 1500.0:
                s.result = "the route's door is not on this map's ground (world data wrong for this crossing)"
                s.log.event("finished", result=s.result, door=[gx, gy], nearest_ground=None if gap is None else round(gap))
                return S.FAILURE
        if game.party_defeated():
            self._drop_child()
            s.result = "party defeated"
            learnt = False
            try:
                from . import pcons as _pc
                learnt = _pc.note_defeat(s.map_id)
            except Exception:
                pass
            s.log.event("finished", result=s.result, no_restart_learnt=learnt)
            return S.FAILURE
        s.perceive()
        eng, now = s.engine, time.time()
        self._unstick_check(s, eng, now)
        self._loot_check(s, eng, now)
        if self._looting(s, eng, now):
            return S.RUNNING
        # every boss seen goes in the log once (name, profession), so missed elites can be traced
        seen = self.__dict__.setdefault("_boss_seen", set())
        for e in eng.mem.enemies.values():
            if e.boss and e.alive and e.in_range and e.id not in seen:      # only agents in view right now
                tries = self.__dict__.setdefault("_boss_tries", {})
                tries[e.id] = tries.get(e.id, 0) + 1
                name = game.agent_name(e.id)
                if name or tries[e.id] >= 8:
                    seen.add(e.id)
                    low = name.lower()
                    hit = next((x for x in (elites.AREAS.get(s.map_id) or ())
                                if low and (x.boss.lower() in low or low in x.boss.lower())), None)
                    s.log.event("boss", agent=e.id, name=name, profession=game.agent_primary(e.id),
                                pos=[round(e.xy[0]), round(e.xy[1])], mine=list(game.player_professions()),
                                signets=game.capture_signets(), listed=hit is not None,
                                elite=None if hit is None else hit.name, elite_profession=None if hit is None else hit.profession,
                                when=None if hit is None else hit.when,
                                a_target=hit is not None and hit in s.targets)
        seen = eng.__dict__.pop("wipe_seen", None)
        if seen is not None:
            s.log.event("wipe", at=[round(seen[0]), round(seen[1])], times_here=seen[2], zones=len(eng.wipes),
                        morale=game.my_morale(), party_low=game.party_low_morale())
        s.log.observe(eng, {"combat_flag": bool(self.blackboard.get("COMBAT_ACTIVE", False)),
                            "killed": game.foes_killed(), "running": True,
                            "riding": getattr(self.__dict__.get("_ride_ground"), "riding", None)})
        if not game.player_dead_safe():
            try:
                s.gear.tick(eng, self.transit, bool(self.blackboard.get("COMBAT_ACTIVE", False)))
            except Exception as e:
                if not getattr(self, "_gear_err", False):
                    self._gear_err = True
                    s.log.event("gear", error=repr(e))
        if self.transit is None and not game.player_dead_safe():
            try:
                s.pcons.tick(eng)
            except Exception as e:
                if not getattr(self, "_pcon_err", False):
                    self._pcon_err = True
                    s.log.event("pcon", error=repr(e))

        # --- death: note where, drop the walk, and let Reforged's wipe recovery do its part ---
        try:
            cond = game.party_condition()
        except Exception as e:
            cond = None
            if not self.__dict__.get("_cond_err"):
                self._cond_err = True
                s.log.event("error", where="party_condition", error=repr(e))
        # The dead flag alone missed a death: also no health, or not alive, held for 2 s (a
        # moment of 0 is read while the game swaps the leader's body at a shrine).
        down, hp_now = game.leader_down()
        if down:
            if self.__dict__.get("_down_since") is None:
                self._down_since = now
        else:
            self._down_since, self._down_said = None, False
        if down and (cond is None or not cond["player_dead"]) and now - (self._down_since or now) >= 2.0:
            cond = dict(cond or {"hp": 0.0, "energy": 0.0, "dead_allies": 0, "ally_hp": 1.0})
            cond["player_dead"] = True
            if not self.__dict__.get("_down_said"):
                self._down_said = True
                s.log.event("death_seen_otherwise", hp=hp_now, flag=False)
        self._cond = cond
        if "_party_size" not in self.__dict__:
            try:
                self._party_size = game.max_party_size()
            except Exception:
                self._party_size = 8
        if cond is not None and cond["player_dead"]:
            self._unflag("leader died")
            self._pull_reset("leader died")
            # whatever put a flag up, take every flag down now and again while lying dead, so
            # the heroes come and raise the leader (one was left parked: dead an hour)
            if now - self.__dict__.get("_dead_unflag_t", 0.0) >= 5.0:
                self._dead_unflag_t = now
                try:
                    game.unflag_heroes(())
                except Exception:
                    pass
                if not self.__dict__.get("_dead_unflag_said"):
                    self._dead_unflag_said = True
                    try:
                        far = [round(math.hypot(x - eng.player_xy[0], y - eng.player_xy[1])) for x, y in game.hero_positions()]
                    except Exception:
                        far = []
                    s.log.event("dead_unflag", heroes_alive_at=far)
            if not self._was_dead:
                self._was_dead = True
                px, py = eng.player_xy
                foes_near = sum(1 for e in eng.mem.enemies.values()
                                if e.alive and not e.lost and math.hypot(e.xy[0] - px, e.xy[1] - py) < 1800.0)
                # killed by enemies that then walked off, and the death read late (a leader alone,
                # no heroes: Dragon's Gullet), is not the ground: a foe near in the last minute
                hazard = foes_near == 0 and now - self.__dict__.get("_foes_near_t", 0.0) > 60.0
                self._hazard_death = (px, py) if hazard and not game.is_junundu() else None
                self._death_xy = (px, py) if not hazard else None
                eng.record_death(eng.player_xy, hazard=hazard)
                if hazard:
                    try:
                        near = game.gadgets_near(px, py)
                    except Exception as e:
                        near = [repr(e)]
                    s.save_hazards()
                    s.log.event("hazard", player=[round(px), round(py)], known=len(eng.hazards), gadgets_near=near[:30])
                if isinstance(foes_near, int) and foes_near >= 7:
                    s.note_hard_spot(eng.player_xy)
                if (self._key is not None and self._key[0] == "action" and isinstance(foes_near, int)
                        and foes_near == 0):
                    # killed by the ground (sulfur, a trap) while doing a route step: twice for the
                    # same step and that step is given up, not walked into a third time
                    k = self._key[1] if self._key[1] >= 0 else ("burrow", tuple(self._target or ()))
                    bad = eng.__dict__.setdefault("_route_action_deaths", {})
                    bad[k] = bad.get(k, 0) + 1
                    if bad[k] >= 2:
                        if isinstance(k, int):
                            self.__dict__.setdefault("_actions_done", set()).add(k)
                        else:
                            failed = self.__dict__.setdefault("_mount_failed", {})
                            failed[k[1]] = 2
                        s.log.event("route_action", stage="given_up", why="died twice on the way, nothing near",
                                    action=k if isinstance(k, int) else list(k[1]))
                s.log.event("death", player=list(eng.player_xy), key=self._key, deaths=eng.deaths, foes_near=foes_near,
                            riding=game.is_junundu(),
                            enemies_in_view=sum(1 for e in eng.mem.enemies.values() if e.alive and e.in_range),
                            consumables=s.pcons.state(eng) if s.cfg.pcons_on else "off")
            self._drop_child()
            return S.RUNNING
        if self._was_dead:
            self._was_dead = False
            self._revived_at = now
            self._dead_unflag_said = False
            try:
                game.unflag_heroes(())       # raised (or back at a shrine): nobody stays parked
            except Exception:
                pass
            px_, py_ = eng.player_xy
            hot = any(e.alive and e.in_range and math.hypot(e.xy[0] - px_, e.xy[1] - py_) < 1300.0
                      for e in eng.mem.enemies.values())
            # at a shrine (nothing near): let things settle. Raised mid-fight: no standing about.
            self._hold_until = now if hot else now + 3.0
            spot = self.__dict__.pop("_hazard_death", None)
            if spot is not None and math.hypot(eng.player_xy[0] - spot[0], eng.player_xy[1] - spot[1]) < 600.0:
                # raised where the ground killed us (Joko's Domain: raised in the sulfur sand
                # and killed again, 12 times): no standing, straight back the way we came
                self._hold_until = now
                self._go_back(eng, s, now, spot, "raised where the ground killed us")
            self._recover_until = now + 14.0 if hot else 0.0
            s.log.event("revived", player=[round(eng.player_xy[0]), round(eng.player_xy[1])], riding=game.is_junundu())
            self._dead_left = None           # a new start: bodies given up on before are judged afresh
            # Raised with little energy (a shrine after a wipe): do not walk into the next fight
            # until it is back. (Eastern Frontier: raised by the entry shrine next to a camp,
            # straight back in at low energy, died 7 times there, morale 85 -> 40.)
            self._energy_wait_until = now + 30.0
        self._ground_watch(eng, s, now)
        self._teleport_watch(eng, s, now)
        px_, py_ = eng.player_xy
        if any(e.alive and not e.lost and math.hypot(e.xy[0] - px_, e.xy[1] - py_) < 1800.0
               for e in eng.mem.enemies.values()):
            self._foes_near_t = now
        if self._party_watch(eng, s, now):
            return S.RUNNING
        if now < self.__dict__.get("_energy_wait_until", 0.0):
            fighting = bool(self.blackboard.get("COMBAT_ACTIVE", False))
            energy = (cond or {}).get("energy", 1.0) if isinstance(cond, dict) else 1.0
            if not fighting and energy is not None and energy < 0.7:
                if not self.__dict__.get("_energy_wait_said"):
                    self._energy_wait_said = True
                    s.log.event("resting", reason="raised with low energy", energy=round(energy, 2))
                self._drop_child()
                self._idle_at = now
                return S.RUNNING
            self._energy_wait_until, self._energy_wait_said = 0.0, False
        if now < self.__dict__.get("_hold_until", 0.0):
            self._idle_at = now              # standing still on purpose is not being stuck
            return S.RUNNING
        self._hands_back_if_done(s, now)
        if self._tp_job_tick(eng, s, now):
            return S.RUNNING
        if self._tp_left_behind(eng, s, now):
            return S.RUNNING
        if self._riding() and self._wurm_heal(s, now):
            return S.RUNNING

        if s.cfg.capture_elites and self.transit is None and self._capture_tick(eng, now):
            return S.RUNNING

        # --- before the fight: bring the nearest group back to the party ---
        if not self._riding() and self._pull_tick(eng, now):
            return S.RUNNING
        # --- before the fight: heroes in first ---
        if not self._riding() and self._lead_tick(eng, now):
            return S.RUNNING
        # For a while after releasing the front line, make sure the release took: clear the flags
        # again every two seconds for ten seconds. (Asking the game "is a hero flagged?" proved
        # unreliable - it answered yes all run long - so this goes by our own record.)
        lead = self.__dict__.get("_lead") or {}
        if (not lead.get("flagged") and lead.get("released_at") and now - lead["released_at"] < 10.0
                and now - self.__dict__.get("_flag_check", 0.0) >= 2.0):
            self._flag_check = now
            try:
                game.unflag_heroes(lead.get("agents", ()))
            except Exception:
                pass

        # --- the fight: call the target that matters, and back off from a pull that is too big ---
        if self._fight_tick(eng, now):
            return S.RUNNING

        # --- blessing: a shrine NPC within reach and nothing hostile about -> take it ---
        if self._key is not None and self._key[0] == "bless":
            self._child.root.blackboard = self.blackboard
            state = self._child.root.tick()
            if state == S.RUNNING and now - self._started < 60.0:
                return S.RUNNING
            after = self._blessing_done(self._key[1], getattr(self, "_bless_before", set()))
            s.log.event("blessing", stage="done", npc=self._key[1], name=self._giver_name(self._key[1]),
                        got=bool(after), effects=sorted(after), before=sorted(getattr(self, "_bless_before", set())),
                        seconds=round(now - self._started, 1), finished=(state != S.RUNNING))
            self._drop_child()
            return S.RUNNING
        # --- things the area's route does along the way (a junundu burrow, a lever) ---
        if self._key is not None and self._key[0] == "action":
            self._child.root.blackboard = self.blackboard
            fin = self.__dict__.get("_action_fin")
            if fin is None:
                state = self._child.root.tick()
                if state == S.RUNNING and now - self._started < 100.0:
                    return S.RUNNING
                fin = self._action_fin = (now, state == S.SUCCESS)
            act = eng.guide_actions[self._key[1]] if 0 <= self._key[1] < len(eng.guide_actions) else {"do": "gadget"}
            if act["do"] == "gadget" and self.__dict__.get("_gadget_phase") == "walk":
                self._gather_heroes(s, now)
                return S.RUNNING
            # a burrow takes a few seconds to turn the party into wurms (Reforged's own bot waits
            # 3.5 s): judge "riding" only once it has had the time
            act = eng.guide_actions[self._key[1]] if 0 <= self._key[1] < len(eng.guide_actions) else {"do": "gadget"}
            if act["do"] == "gadget" and fin[1] and not game.is_junundu() and now - fin[0] < 5.0:
                return S.RUNNING
            # ...and the heroes each get into their own after the leader: stay by the burrow
            # until every living one rides (a hero was left on foot when the leader set off at
            # once). Up to 20 s; 8 s flat if their bars cannot be read.
            if act["do"] == "gadget" and fin[1] and game.is_junundu():
                rode, living, readable = game.heroes_riding()
                waited = now - fin[0]
                if readable and rode < living and self._mount_stragglers(s, now, waited):
                    return S.RUNNING
                if (rode < living and waited < 20.0) if readable else (living > 0 and waited < 8.0):
                    return S.RUNNING
                if self.__dict__.get("_stragglers_flagged"):
                    self._stragglers_flagged = False
                    try:
                        game.unflag_heroes(game.hero_agent_ids())
                    except Exception:
                        pass
                s.log.event("route_action", stage="party_mounted", heroes_riding=rode, heroes=living,
                            readable=readable, waited=round(waited, 1), bar_sample=game.hero_bar_sample())
            if act["do"] == "skill" and self._skill_action(act, s, now, fin):
                return S.RUNNING
            self._action_fin = None
            self._route_action_done(eng, s, now, fin[1])
            self._drop_child()
            return S.RUNNING
        if self.transit is None and self._route_action_tick(eng, s, now):
            return S.RUNNING
        if eng.cfg.take_blessings and self.transit is None and now - self._bless_check >= 3.0:
            self._bless_check = now
            if self._try_blessing(eng, now):
                return S.RUNNING

        # --- rest: before walking on, wait for health, energy and the dead to be back ---
        # (This used to wait for a moment with no walk in progress. Since walks are handed on
        # from one stretch to the next without stopping, there never was one: no rest was taken
        # from 0.46 on, and the party walked into fights with heroes dead and energy low.)
        # Rest gate. Walks hand over to the next stretch while still moving, so the old "only when no
        # step is running" test never came true and resting silently stopped (0.46-0.49.3). Now: rest
        # whenever nothing is fighting, no pull is under way and we are not escaping or blessing.
        pulling = (self.__dict__.get("_pull") or {}).get("phase") in ("go", "back", "hold", "shift")
        if cond is not None and not pulling and not (self._key is not None and self._key[0] in ("escape", "bless")) \
                and not bool(self.blackboard.get("COMBAT_ACTIVE", False)):
            px, py = eng.player_xy
            e_rate = self._energy_rate(now, cond["energy"])
            threatened = any(e.alive and e.in_range and math.hypot(e.xy[0] - px, e.xy[1] - py) < 1500.0
                             for e in eng.mem.enemies.values())
            cfg = eng.cfg
            # The dead are waited for once. If a full wait raised nobody, nobody here can raise
            # them (the one hero left has no such skill, or it is spent): they stay out of the
            # reckoning until one of them is up again. Before, the wait simply began again, and
            # the leader and one hero stood by six bodies for ten minutes (Resplendent Makuun).
            # Carrying on is also the way back: if the two of them fall, all return at a shrine.
            # The dead are counted only within 4500 of us, so walking on made the count drop, which
            # read as "one is up again": the wait began again and the party was walked 4300 back
            # to the same two bodies every two minutes for half an hour (Diessa 0.50.78). Now the
            # given-up bodies are remembered by where they lie; one is let go only when we stand
            # near that spot and it is no longer there. Only a body not on that list stops us.
            fresh_dead = cond["dead_allies"]
            left = self.__dict__.get("_dead_left")
            if left is not None:
                fresh_dead = self._dead_not_given_up(eng, px, py, cond["dead_allies"])
            why = ("someone is dead" if fresh_dead else
                   "health" if cond["hp"] < (0.4 if self._riding() else min(0.97, cfg.rest_hp + 0.04 * eng.caution())) else
                   "energy" if cond["energy"] < min(0.9, cfg.rest_energy + 0.08 * eng.caution()) else
                   "party health" if cond["ally_hp"] < min(0.95, cfg.rest_ally_hp + 0.05 * eng.caution()) else "")
            # Energy and party health come back on the way to the next fight: stop only for what
            # the walk there will not refill. (Own health and the dead still mean stopping.)
            if why in ("energy", "party health"):
                gap = min((math.hypot(e.xy[0] - px, e.xy[1] - py) for e in eng.mem.enemies.values()
                           if e.alive and not e.lost), default=4000.0)
                if why == "energy":
                    need = min(0.9, cfg.rest_energy + 0.08 * eng.caution())
                    if tactics.on_arrival(cond["energy"], e_rate, gap) >= need:
                        why = ""
                else:
                    need = min(0.95, cfg.rest_ally_hp + 0.05 * eng.caution())
                    if tactics.on_arrival(cond["ally_hp"], 0.015, gap) >= need:
                        why = ""
            if why and cond["hp"] <= 0.0 and cond["energy"] <= 0.0:
                why = ""                     # a blank read (loading, just raised): not a reason to stop
            if why and eng.in_hazard(px, py):
                why = ""                     # never stand and rest under a trap: move on first
            if why == "energy" and self.transit is not None and self._rest_since is None:
                why = ""                     # just passing through: energy refills on the walk
            if why and self._alone():
                why = ""                     # nobody left to raise anyone: waiting changes nothing
            if why and not threatened:
                if self._rest_since is None:
                    self._rest_since = now
                    s.resting = why
                    self._drop_child()
                    s.log.event("resting", reason=why, hp=round(cond["hp"], 2), energy=round(cond["energy"], 2),
                                dead=cond["dead_allies"], party_hp=round(cond["ally_hp"], 2),
                                energy_per_s=round(e_rate, 4))
                if now - self._rest_since < cfg.rest_max_seconds:
                    if cond["dead_allies"]:
                        self._walk_to_the_dead(eng, now)
                    self._idle_at = now          # resting is not being stuck: the first step after a
                    return S.RUNNING             # rest was "stalled" at once and a wall was marked ahead
                if cond["dead_allies"] and s.resting == "someone is dead":
                    try:
                        spots = [tuple(b) for b in game.dead_ally_positions(4500.0)]
                    except Exception:
                        spots = []
                    self._dead_left = list(self.__dict__.get("_dead_left") or []) + spots
                    s.log.event("dead_left", dead=cond["dead_allies"], waited=round(now - self._rest_since),
                                player=[round(px), round(py)],
                                note="nobody could raise them: carrying on without them until one is up again")
                why += " (gave up waiting)"
            if self._rest_since is not None:
                s.log.event("rested", seconds=round(now - self._rest_since, 1), reason=s.resting, ended=why or "ready",
                            hp=round(cond["hp"], 2), energy=round(cond["energy"], 2), dead=cond["dead_allies"])
                self._rest_since, s.resting = None, ""

        # Learn which listed exits are not doors: the party has walked all round one.
        if now - self.__dict__.get("_door_judged", 0.0) >= 30.0:
            self._door_judged = now
            try:
                from ..core.geometry import walked_round
                start = getattr(self, "_entry_xy", None) or eng.player_xy
                self.__dict__.setdefault("_entry_xy", start)
                trail = eng.mem.route[-3000:]
                for ex in getattr(s, "exit_candidates", ()):
                    if math.hypot(ex[0] - start[0], ex[1] - start[1]) < 1500.0:
                        continue                 # where we came in: a real door is right there
                    if walked_round((ex[0], ex[1]), trail) and s.note_not_door(ex):
                        pass
            except Exception as e:
                if not self.__dict__.get("_door_judge_err"):
                    self._door_judge_err = True
                    s.log.event("not_a_door", error=repr(e))

        # Looping between stacked levels: let the game's own pathing walk at the target for a bit.
        direct = self.__dict__.get("_direct")
        if direct is not None:
            until, (tx, ty), dkey = direct
            px_, py_ = eng.player_xy
            fighting = bool(self.blackboard.get("COMBAT_ACTIVE", False))
            chain = self.__dict__.get("_direct_chain") or []
            if chain and math.hypot(chain[0][0] - px_, chain[0][1] - py_) < 300.0:
                chain.pop(0)                 # reached this point of the known route: on to the next
                if chain and now < self.__dict__.get("_direct_hard", 0.0):
                    until = now + 30.0
                    self._direct = (until, (tx, ty), dkey)
            if chain:
                tx, ty = chain[0]
            near_foes = sum(1 for e in eng.mem.enemies.values()
                            if e.alive and e.in_range and math.hypot(e.xy[0] - px_, e.xy[1] - py_) < 2000.0)
            if near_foes >= 6:
                # A crowd ahead: the planner takes over (pulls, one group at a time). Walking on
                # by the game's pathing marched the party into 19 of them (Diessa, a wipe).
                self._direct, self._direct_chain = None, None
                s.log.event("step_loop", key=dkey, direct_walk="stopped", why="enemies near", foes=near_foes,
                            chain_left=len(chain))
            elif chain and fighting and now < self.__dict__.get("_direct_hard", 0.0):
                # a fight on the known route: fight it, then go on along the route
                self._direct = (max(until, now + 10.0), direct[1], dkey)
            elif now < until and not fighting and math.hypot(tx - px_, ty - py_) > (300.0 if chain else 600.0):
                self._drop_child()
                if now - self.__dict__.get("_direct_move", 0.0) >= 1.5:
                    self._direct_move = now
                    game.move_to(tx, ty)
                return S.RUNNING
            else:
                self._direct = None
                self._direct_chain = None
                s.log.event("step_loop", key=dkey, direct_walk="done", gap=round(math.hypot(tx - px_, ty - py_)),
                            fighting=fighting, chain_left=len(chain))

        # Backed off from a big crowd: hold here for it. If it does not come, it was not coming
        # for us; leave that ground for later rather than walking back into it.
        hold = self.__dict__.get("_crowd_hold")
        if hold is not None:
            until, (cx, cy), size = hold
            px_, py_ = eng.player_xy
            gap = min((math.hypot(e.xy[0] - px_, e.xy[1] - py_) for e in eng.mem.enemies.values()
                       if e.alive and e.in_range), default=None)
            if now < until and (gap is None or gap >= 1300.0):
                self._drop_child()
                return S.RUNNING
            self._crowd_hold = None
            if gap is None or gap >= 1300.0:
                eng.postpone((cx, cy), 300.0)
                s.log.event("crowd_left", at=[round(cx), round(cy)], size=size, gap=None if gap is None else round(gap))

        # Just raised in the middle of a fight, at a sliver of health: once clear of the enemy, stay
        # clear until health is back (or they come to us). Walking straight back in killed the
        # leader again one second after his retreat ended.
        if now < self.__dict__.get("_recover_until", 0.0):
            px_, py_ = eng.player_xy
            gap = min((math.hypot(e.xy[0] - px_, e.xy[1] - py_) for e in eng.mem.enemies.values()
                       if e.alive and e.in_range), default=None)
            if game.my_health() >= 0.6 or gap is None:
                self._recover_until = 0.0
            elif gap > 450.0:
                self._drop_child()
                return S.RUNNING

        # Watchdog: foes remain but none has died for half an hour. Bahdok Caverns searched the
        # same corner for nearly four hours (two thirds of the map fenced off by mistake); give
        # the area up and let the campaign move on instead.
        if self.transit is None and eng.foes_remaining:
            seen = self.__dict__.get("_progress_seen")
            if seen is None or eng.foes_remaining < seen[0]:
                self._progress_seen = (eng.foes_remaining, now)
            elif now - seen[1] > NO_PROGRESS_S and not bool(self.blackboard.get("COMBAT_ACTIVE", False)):
                s.result = ("no progress: %d foes left and none killed for %d minutes (out of reach?)"
                            % (eng.foes_remaining, NO_PROGRESS_S // 60))
                s.log.event("finished", result=s.result, status=eng.status())
                s.dump_failure(s.result)
                return S.FAILURE

        if self._child is None or now - self._last_plan >= REPLAN_S:
            self._last_plan = now
            step = eng.next_step()
            if step is None:
                self._unflag("finished")
                self._drop_child()
                if (eng.mode == DONE and self.transit is None and eng.cfg.do_cartography and eng.carto is not None
                        and eng.cfg.edge_pass and not eng.edge_pass and (eng.foes_remaining == 0 or not eng.cfg.do_vanquish)):
                    # Vanquish done: the exits no longer need a wide berth. Clear the map cells
                    # along the edges that could only be reached from beside a door.
                    self._vq_done = True
                    try:
                        left = eng.open_edges()
                    except Exception as e:
                        left = 0
                        s.log.event("edge_pass", error=repr(e))
                    s.log.event("edge_pass", fogged_cells=left, exit_radius=eng.exit_radius,
                                stand_cells=len(eng.carto.stand) if eng.carto else 0)
                    if left:
                        return S.RUNNING
                if eng.mode not in (DONE, FAILED):
                    return S.RUNNING          # no step this moment, but the search is not over
                if eng.mode == DONE:
                    s.result = "arrived" if self.transit else "complete"
                    s.log.event("finished", result=s.result, status=eng.status())
                    return S.SUCCESS
                s.result = ("blocked: %d foes remain behind %d place(s) the party could not get past"
                            % (eng.foes_remaining or 0, len(eng.walls))) if eng.walls else \
                    "stopped: nothing left to search but foes remain"
                s.log.event("finished", result=s.result, status=eng.status())
                s.dump_failure(s.result)
                return S.FAILURE
            # a step with no objective behind it (a way out just after a wipe): its own key.
            # (Diessa: eng.objective was None here, the error repeated and the area was dropped.)
            key = eng.objective.key if eng.objective is not None else ("step", round(step[0]), round(step[1]))
            if self._teleport_first(eng, s, now):
                return S.RUNNING
            if self._escape is not None:
                # just marked an obstacle: first a short step clear of it, then back to the plan
                step, key = (self._escape[0], self._escape[1], self._escape_radius), ("escape", int(now))
                self._escape, self._escape_until = None, now + self._escape_secs
                if self.__dict__.pop("_escape_hands_off", False):
                    self._hands_off(s, True, "combat_stuck")
                self._escape_secs, self._escape_radius = 6.0, 900.0
                self._drop_child()
                self._start_step(step, key)
            elif self._child is not None and self._key and self._key[0] in ("escape", "teleport") and now < self._escape_until:
                pass                         # let the step clear finish
            elif self._child is None:
                self._start_step(step, key)
            elif key != self._key:
                self._drop_child()           # the plan changed: abandon the walk in progress
                self._start_step(step, key)
            elif (self._target is not None and eng.objective is not None and eng.objective.kind != "carto"
                  and math.hypot(eng.player_xy[0] - self._target[0], eng.player_xy[1] - self._target[1]) < 700.0
                  and math.hypot(step[0] - self._target[0], step[1] - self._target[1]) > 400.0
                  and not any(e.alive and e.in_range
                              and math.hypot(e.xy[0] - eng.player_xy[0], e.xy[1] - eng.player_xy[1]) < 1500.0
                              for e in eng.mem.enemies.values())):
                # Nearly at the end of this stretch and the way goes on: hand over the next
                # stretch now, while still moving, instead of arriving, stopping and starting again.
                self._drop_child()
                self._start_step(step, key)

        # Progress = the party moved or something died. Standing still with neither usually means
        # the fight routine is waiting on an enemy it cannot reach (up a ledge, across a gap).
        kills, pxy = game.foes_killed(), eng.player_xy
        if self._stuck_xy is not None and (kills != self._progress_kills or math.hypot(
                pxy[0] - self._stuck_xy[0], pxy[1] - self._stuck_xy[1]) > 600.0):
            self._stuck_xy, self._stuck_since = None, 0.0      # got moving again
        if (self._progress_xy is None or kills != self._progress_kills
                or math.hypot(pxy[0] - self._progress_xy[0], pxy[1] - self._progress_xy[1]) > STALL_RADIUS):
            self._progress_at, self._progress_xy, self._progress_kills = now, pxy, kills
        walking = not bool(self.blackboard.get("COMBAT_ACTIVE", False)) and not any(
            e.alive and e.in_range and math.hypot(e.xy[0] - pxy[0], e.xy[1] - pxy[1]) < 2500.0
            for e in eng.mem.enemies.values())
        stalled = now - max(self._progress_at, self.__dict__.get("_idle_at", 0.0)) > (WALK_STALL_S if walking else STALL_S)
        if getattr(self, "_same_steps", 0) >= 8 and now - self._progress_at > 6.0:
            stalled = True                   # the same step keeps "finishing" and we have not moved
            eng.player_node = None           # we may have the party on the wrong level: look again
            self._same_steps = 0
        if stalled:
            self.session.stalls += 1

        if stalled or now - self._started > STEP_TIMEOUT_S:
            near = [(e.id, round(e.xy[0]), round(e.xy[1])) for e in eng.mem.enemies.values() if e.alive and e.in_range]
            s.log.event("step_abandoned", reason="stalled" if stalled else "timeout", key=self._key,
                        target=self._target, player=list(eng.player_xy), seconds=round(now - self._started),
                        combat_flag=bool(self.blackboard.get("COMBAT_ACTIVE", False)), walking=walking,
                        enemies_in_view=near[:25])
            if stalled:
                self._pinned(eng, s, now, walking)
            eng.step_failed()
            self._drop_child()
            if self._stuck_since and now - self._stuck_since > STUCK_GIVE_UP_S and self.transit is None:
                s.result = "stuck: pinned in one place for %d minutes" % (STUCK_GIVE_UP_S // 60)
                s.log.event("finished", result=s.result, status=eng.status())
                s.dump_failure(s.result)
                return S.FAILURE
            return S.RUNNING

        # Safety net: Reforged's own pathing chose a line that drifts toward an exit. Only a
        # step that leads further in is refused; one that leads away from the door is how the
        # party gets out of the doorway, and refusing those too left it standing there while
        # every objective was written off in turn (Resplendent Makuun).
        px, py = eng.player_xy
        taken_off = {(float(e[0]), float(e[1])) for e in getattr(eng, "removed_exits", ())}

        def zone(e):                         # a door whose ring was taken off still has a doorway
            return 0.6 * eng.ring(e, eng.cfg.edge_exit_radius if (float(e[0]), float(e[1])) in taken_off else eng.exit_radius)

        inside = [e for e in eng.exits if math.hypot(px - e[0], py - e[1]) < zone(e)]
        if self._left_exit_zone and inside:
            ex, ey = min(inside, key=lambda e: math.hypot(px - e[0], py - e[1]))
            gap = math.hypot(px - ex, py - ey)
            tx, ty = self._target if self._target is not None else (px, py)
            if math.hypot(tx - ex, ty - ey) < gap + 50.0:
                s.log.event("exit_guard", player=[round(px), round(py)], key=self._key, target=self._target,
                            door=[round(ex), round(ey)], gap=round(gap))
                if self._key and self._key[0] == "carto" and eng.carto is not None:
                    try:                     # that cell can only be had from inside a doorway: leave it
                        eng.carto.declined.add(tuple(self._key[1]))
                        eng.carto_given_up += 1
                    except Exception:
                        pass
                eng.step_failed()
                self._drop_child()
                # and step back out of the doorway, straight away from the door
                out = zone((ex, ey)) * 1.4 + 200.0
                self._escape = (ex + (px - ex) / (gap or 1.0) * out, ey + (py - ey) / (gap or 1.0) * out)
                self._left_exit_zone = False     # re-arm once we are clear again
                return S.RUNNING
        elif not inside:
            self._left_exit_zone = True

        if self._child is None:
            return S.RUNNING             # the step was not started (set aside at a door): plan again next tick
        self._child.root.blackboard = self.blackboard
        state = self._child.root.tick()
        if state == S.SUCCESS:
            s.log.event("step_done", key=self._key, seconds=round(now - self._started, 1))
            self._drop_child()
        elif state == S.FAILURE:
            px, py = game.player_xy()
            tx, ty = self._target
            miss = math.hypot(px - tx, py - ty)
            s.log.event("step_failed", key=self._key, target=self._target, player=[round(px), round(py)],
                        short_by=round(miss), seconds=round(now - self._started, 1),
                        riding=self._riding(), gadgets_near=self._gadgets_for_log(px, py))
            obj = eng.objective
            if (miss > 1000.0 and now - self._started < 1.5 and self.transit is None and obj is not None
                    and self._key and self._key[0] not in ("teleport", "escape")):
                # refused at once by the game's pathing: no way on foot. Twice for one objective
                # and a teleporter is tried (Prophet's Path: groups up on teleporter-only ground).
                inst = self.__dict__.setdefault("_instant_fails", {})
                inst[obj.key] = inst.get(obj.key, 0) + 1
                if inst[obj.key] == 2 and ride_store.add_unreachable(game.map_id(), obj.pos):
                    s.log.event("unreachable_on_foot", pos=[round(obj.pos[0]), round(obj.pos[1])])
                if inst[obj.key] == 2 and self._try_teleport(eng, s, now, obj.pos):
                    return S.RUNNING
            if miss > 400.0:
                eng.step_failed()            # could not get there: count it against the objective
                # the same leg failing again: our map shows a way the game does not walk (map 13:
                # five times short by 1000-1700 at one spot, while the game's own pathing got
                # there). Let the game path at the objective for a while.
                fails = self.__dict__.setdefault("_leg_fails", {})
                leg = (round(tx / 300.0), round(ty / 300.0))
                fails[leg] = fails.get(leg, 0) + 1
                obj = eng.objective
                if fails[leg] == 2 and obj is not None and self.__dict__.get("_direct") is None:
                    self._direct_start(eng, obj, self._key, now)
                    s.log.event("step_loop", key=self._key, why="same leg failed twice", leg=[round(tx), round(ty)],
                                direct_walk="start")
            self._drop_child()
        return S.RUNNING


from .cross import CrossNode      # noqa: E402  (kept importable from here)
