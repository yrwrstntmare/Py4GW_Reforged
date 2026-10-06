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
from ..core.engine import DONE
from . import game

REPLAN_S = 1.0
STEP_TIMEOUT_S = 240.0       # one walking step, fights included
STUCK_GIVE_UP_S = 600.0     # pinned within 600 units for this long, nothing dying: the area cannot be finished
STALL_S = 45.0               # no movement and no kill for this long: give the step up
WALK_STALL_S = 12.0          # the same with no enemy anywhere near: we are caught on something, not fighting
STALL_RADIUS = 350.0         # "no movement" = still inside this circle (rocking back and forth on a tree counts as none)


from .guard import guarded_tick
from .node_blessing import BlessingMixin
from .node_capture import CaptureMixin, e_prof
from .node_fight import FightMixin

_RETIRED = []                 # (time, behaviour tree) kept alive for a while after being abandoned
RETIRE_SECONDS = 20.0


def retire(tree):
    now = time.time()
    _RETIRED.append((now, tree))
    while _RETIRED and now - _RETIRED[0][0] > RETIRE_SECONDS:
        _RETIRED.pop(0)


class AdaptiveNode(FightMixin, BlessingMixin, CaptureMixin, BehaviorTree.Node):
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

    def _drop_child(self):
        # A walk that has just started has asked Py4GW's path planner for a route; the answer is
        # written on the game's own thread a moment later. If the walk is thrown away before
        # then, that write lands in freed memory and the game crashes (seen twice, both right
        # after a shrine revival). So an abandoned walk is not destroyed: it is parked, untouched,
        # until any planning it started is long finished.
        if self._child is not None:
            retire(self._child)
        self._child, self._key, self._target = None, None, None

    def get_children(self):
        return [self._child.root] if self._child is not None else []

    def _start_step(self, step, key):
        x, y, radius = step
        # Map cells can be slivers of ground: stand on the exact spot, not somewhere near it.
        exact = self.session.engine.objective is not None and self.session.engine.objective.kind == "carto"
        self._child = BT.MoveAndKill(pos=(x, y), clear_area_radius=radius, move_tolerance=40.0 if exact else 150.0)
        self._key, self._target, self._started = key, (x, y), time.time()
        self.session.log.event("step_start", key=key, target=[round(x), round(y)], clear_radius=round(radius))
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

    _escape, _escape_until = None, 0.0

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
        if (walking or self._stuck_n >= 2) and self._target is not None:
            spot = eng.add_wall((px, py), self._target, detour=self._stuck_n >= 2)
            away = spot or (eng.walls[-1] if eng.walls else None)
            self._escape = eng.escape_point((px, py), away)
            s.log.event("wall", player=[round(px), round(py)], target=list(self._target),
                        spot=None if spot is None else [round(spot[0]), round(spot[1])], walls=len(eng.walls),
                        walking=walking, times_here=self._stuck_n,
                        step_clear=None if self._escape is None else [round(self._escape[0]), round(self._escape[1])],
                        detour=bool(eng._detour), route_points=len(eng.guide) if eng._detour else 0)

    def _tick_core(self):
        S, s = BehaviorTree.NodeState, self.session
        s.transit = self.transit
        if self.arrive_map is not None and game.map_ready() and game.map_id() == self.arrive_map:
            self._drop_child()
            return S.SUCCESS
        if self.target_map_id is not None and game.map_ready() and game.map_id() != self.target_map_id:
            self._drop_child()
            if getattr(self, "_vq_done", False) or (
                    self.transit is None and s.engine is not None and s.engine.cfg.do_vanquish
                    and s.engine.foes_remaining == 0):
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
            self._no_map_since = getattr(self, "_no_map_since", 0.0) or t
            if t - self._no_map_since > 45.0 and not getattr(self, "_no_map_said", False):
                # far longer than any loading screen: the connection dropped. Reconnecting puts the
                # party back in the same instance and the run picks up from its saved state.
                self._no_map_said = True
                s.resumed = "no map for 45 s - disconnected? Reconnect and the run carries on from where it was."
                s.log.event("no_map", seconds=round(t - self._no_map_since), map_loading=game.map_loading_safe())
            return S.RUNNING
        if getattr(self, "_no_map_since", 0.0):
            if getattr(self, "_no_map_said", False):
                s.log.event("map_back", after=round(time.time() - self._no_map_since), map_id=game.map_id())
            self._no_map_since, self._no_map_said = 0.0, False
        if game.party_defeated():
            self._drop_child()
            s.result = "party defeated"
            s.log.event("finished", result=s.result)
            return S.FAILURE
        s.perceive()
        eng, now = s.engine, time.time()
        # every boss seen goes in the log once (name, profession), so missed elites can be traced
        seen = self.__dict__.setdefault("_boss_seen", set())
        for e in eng.mem.enemies.values():
            if e.boss and e.alive and e.in_range and e.id not in seen:      # only agents in view right now
                tries = self.__dict__.setdefault("_boss_tries", {})
                tries[e.id] = tries.get(e.id, 0) + 1
                name = game.agent_name(e.id)
                if name or tries[e.id] >= 8:
                    seen.add(e.id)
                    s.log.event("boss", agent=e.id, name=name, profession=game.agent_primary(e.id),
                                pos=[round(e.xy[0]), round(e.xy[1])], mine=list(game.player_professions()),
                                signets=game.capture_signets())
        s.log.observe(eng, {"combat_flag": bool(self.blackboard.get("COMBAT_ACTIVE", False)),
                            "killed": game.foes_killed(), "running": True})
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
        except Exception:
            cond = None
        self._cond = cond
        if "_party_size" not in self.__dict__:
            try:
                self._party_size = game.max_party_size()
            except Exception:
                self._party_size = 8
        if cond is not None and cond["player_dead"]:
            self._unflag("leader died")
            if not self._was_dead:
                self._was_dead = True
                px, py = eng.player_xy
                foes_near = sum(1 for e in eng.mem.enemies.values()
                                if e.alive and not e.lost and math.hypot(e.xy[0] - px, e.xy[1] - py) < 1800.0)
                hazard = foes_near == 0
                eng.record_death(eng.player_xy, hazard=hazard)
                if hazard:
                    try:
                        near = game.gadgets_near(px, py)
                    except Exception as e:
                        near = [repr(e)]
                    s.save_hazards()
                    s.log.event("hazard", player=[round(px), round(py)], known=len(eng.hazards), gadgets_near=near[:30])
                s.log.event("death", player=list(eng.player_xy), key=self._key, deaths=eng.deaths, foes_near=foes_near,
                            enemies_in_view=sum(1 for e in eng.mem.enemies.values() if e.alive and e.in_range))
            self._drop_child()
            return S.RUNNING
        if self._was_dead:
            self._was_dead = False
            self._revived_at = now
            px_, py_ = eng.player_xy
            hot = any(e.alive and e.in_range and math.hypot(e.xy[0] - px_, e.xy[1] - py_) < 1300.0
                      for e in eng.mem.enemies.values())
            # at a shrine (nothing near): let things settle. Raised mid-fight: no standing about.
            self._hold_until = now if hot else now + 3.0
            s.log.event("revived", player=[round(eng.player_xy[0]), round(eng.player_xy[1])])
        if now < self.__dict__.get("_hold_until", 0.0):
            return S.RUNNING

        if s.cfg.capture_elites and self.transit is None and self._capture_tick(eng, now):
            return S.RUNNING

        # --- before the fight: heroes in first ---
        if self._lead_tick(eng, now):
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
            if state == S.RUNNING and now - self._started < 45.0:
                return S.RUNNING
            s.log.event("blessing", stage="done", npc=self._key[1], got=self._has_blessing(),
                        seconds=round(now - self._started, 1), finished=(state != S.RUNNING))
            self._drop_child()
            return S.RUNNING
        if eng.cfg.take_blessings and self.transit is None and now - self._bless_check >= 3.0:
            self._bless_check = now
            if self._try_blessing(eng, now):
                return S.RUNNING

        # --- rest: before walking on, wait for health, energy and the dead to be back ---
        if self._child is None and cond is not None:
            px, py = eng.player_xy
            threatened = any(e.alive and e.in_range and math.hypot(e.xy[0] - px, e.xy[1] - py) < 1500.0
                             for e in eng.mem.enemies.values())
            cfg = eng.cfg
            why = ("someone is dead" if cond["dead_allies"] else
                   "health" if cond["hp"] < min(0.97, cfg.rest_hp + 0.04 * eng.caution()) else
                   "energy" if cond["energy"] < min(0.9, cfg.rest_energy + 0.08 * eng.caution()) else
                   "party health" if cond["ally_hp"] < min(0.95, cfg.rest_ally_hp + 0.05 * eng.caution()) else "")
            if why and eng.in_hazard(px, py):
                why = ""                     # never stand and rest under a trap: move on first
            if why and not threatened:
                if self._rest_since is None:
                    self._rest_since = now
                    s.resting = why
                if now - self._rest_since < cfg.rest_max_seconds:
                    if cond["dead_allies"]:
                        self._walk_to_the_dead(eng, now)
                    return S.RUNNING
                why += " (gave up waiting)"
            if self._rest_since is not None:
                s.log.event("rested", seconds=round(now - self._rest_since, 1), reason=s.resting, ended=why or "ready",
                            hp=round(cond["hp"], 2), energy=round(cond["energy"], 2), dead=cond["dead_allies"])
                self._rest_since, s.resting = None, ""

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
                if eng.mode == DONE:
                    s.result = "arrived" if self.transit else "complete"
                    s.log.event("finished", result=s.result, status=eng.status())
                    return S.SUCCESS
                s.result = ("blocked: %d foes remain behind %d place(s) the party could not get past"
                            % (eng.foes_remaining or 0, len(eng.walls))) if eng.walls else \
                    "stopped: nothing left to search but foes remain"
                s.log.event("finished", result=s.result, status=eng.status())
                return S.FAILURE
            key = eng.objective.key
            if self._escape is not None:
                # just marked an obstacle: first a short step clear of it, then back to the plan
                step, key = (self._escape[0], self._escape[1], 900.0), ("escape", int(now))
                self._escape, self._escape_until = None, now + 6.0
                self._drop_child()
                self._start_step(step, key)
            elif self._child is not None and self._key and self._key[0] == "escape" and now < self._escape_until:
                pass                         # let the step clear finish
            elif self._child is None:
                self._start_step(step, key)
            elif key != self._key:
                self._drop_child()           # the plan changed: abandon the walk in progress
                self._start_step(step, key)
            elif (self._target is not None and eng.objective.kind != "carto"
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
        stalled = now - self._progress_at > (WALK_STALL_S if walking else STALL_S)
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
                return S.FAILURE
            return S.RUNNING

        # Safety net: Reforged's own pathing chose a line that drifts toward an exit.
        px, py = eng.player_xy
        if self._left_exit_zone:
            guard = eng.exit_radius * 0.6
            if any(math.hypot(px - ex, py - ey) < guard for ex, ey in eng.exits):
                s.log.event("exit_guard", player=[round(px), round(py)], key=self._key, target=self._target)
                if self._key and self._key[0] == "carto" and eng.carto is not None:
                    try:                     # that cell can only be had from inside a doorway: leave it
                        eng.carto.declined.add(tuple(self._key[1]))
                        eng.carto_given_up += 1
                    except Exception:
                        pass
                eng.step_failed()
                self._drop_child()
                self._left_exit_zone = False     # re-arm once we are clear again
                return S.RUNNING
        elif not eng.nav.in_no_go(px, py):
            self._left_exit_zone = True

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
                        short_by=round(miss), seconds=round(now - self._started, 1))
            if miss > 400.0:
                eng.step_failed()            # could not get there: count it against the objective
            self._drop_child()
        return S.RUNNING


from .cross import CrossNode      # noqa: E402  (kept importable from here)
