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
from . import game

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
            self._loot_since = now
            self._drop_child()
            s.log.event("looting", player=[round(x), round(y)], wanted=game.loot_wanted())
        if now - self._loot_since < LOOT_HOLD_S:
            return True
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
        if (near < 1300.0 or self._child is None or self.__dict__.get("_loot_since")
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
        if bool(self.blackboard.get("COMBAT_ACTIVE", False)):
            return False
        nxt = eng.next_guide_src()
        if "_actions_done" not in self.__dict__:
            self._actions_done = set()
            if s.__dict__.pop("_resume_route", False):
                # picked up mid-route after a reload: what the route did before this point is done
                self._actions_done = {i for i, a in enumerate(acts) if a["at"] < nxt}
                s.log.event("route_action", stage="resumed", next_point=nxt, already_done=sorted(self._actions_done))
        done = self._actions_done
        for i, a in enumerate(acts):
            if a.get("save_from") is not None and i not in done and a["save_from"] <= nxt < 10 ** 9 and self._riding():
                self._save_slot(a, s, True)
        for i, a in enumerate(acts):
            if i in done or a["at"] > nxt:
                continue
            if a["do"] == "gadget" and game.is_junundu():
                done.add(i)                  # already riding: the burrow is not needed
                self._ride_from = tuple(a["value"])
                continue
            return self._start_route_action(i, a, now, s)
        # On foot where the wurm is needed: standing on ground we rode over (died, fell off), or
        # the walk in hand goes onto it (the game set us down on rock and the plan leads back).
        # Mount first, at the nearest burrow of the route, not the last one used. (Sulfurous
        # Wastes: set down by the second burrow, it ran for the first one across the sulfur.)
        if (ground is not None and ground.ridden and not ground.riding
                and now - self.__dict__.get("_remount_at", 0.0) > 30.0):
            walking = self._child is not None and self._key is not None and self._key[0] != "action"
            if ground.needs_mount(eng.player_xy, self._target if walking else None):
                s.log.event("route_action", stage="needs_mount",
                            why="standing on wurm ground" if ground.on_ridden(eng.player_xy) else "the step leads onto wurm ground",
                            player=[round(eng.player_xy[0]), round(eng.player_xy[1])],
                            step=[round(self._target[0]), round(self._target[1])] if walking and self._target else None,
                            ridden_cells=len(ground.ridden))
                failed = self.__dict__.setdefault("_mount_failed", {})
                mounts = [tuple(a["value"]) for a in acts
                          if a.get("do") == "gadget" and failed.get(tuple(a["value"]), 0) < 2]
                spot = ground.nearest_mount(eng.player_xy, mounts)
                if spot is not None:
                    self._remount_at = now
                    return self._start_route_action(-1, {"do": "gadget", "value": list(spot), "at": nxt}, now, s)
        return False

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
        Gives up after 120 s."""
        skill = int(a.get("value") or 0)
        st = self.__dict__.get("_skill_try")
        if st is None:
            st = self._skill_try = {"t": now, "next": now, "tries": 0, "hits": 0, "misses": 0, "peak": 0,
                                    "before": game.skill_state(skill), "targets": [], "charged_at": None}
        state = game.skill_state(skill)
        adr, need = state.get("adrenaline") or 0, state.get("need") or 0
        used = bool(st["tries"]) and (st["peak"] - adr >= max(1.0, need * 0.5) or (state.get("recharge") or 0) > 0)
        st["peak"] = max(st["peak"], adr)
        pos = a.get("pos")
        px, py = game.player_xy()
        if not used and state["slot"] and now - st["t"] < 120.0:
            if now < st["next"]:
                return True
            charged = state.get("charged")
            if charged is False and a.get("charge_with") == "attack" and st["misses"] < 8:
                foe = game.nearest_enemy(float(a.get("charge_reach", 3000.0)))
                st["next"] = now + 1.0
                if foe is None:
                    st["misses"] += 1                # nothing to hit: wait a little, then report
                else:
                    game.attack(foe[0])
                    st["hits"] += 1
                    if len(st["targets"]) < 5 and list(foe) not in st["targets"]:
                        st["targets"].append(list(foe))
                return True
            if charged and st["charged_at"] is None:
                st["charged_at"] = now
            if not (charged is False and a.get("charge_with")):
                if pos and math.hypot(px - pos[0], py - pos[1]) > 150.0 and st.get("walks", 0) < 30:
                    st["walks"] = st.get("walks", 0) + 1     # back onto the spot (charging took us off it)
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
                    seconds=round(now - st["t"], 1), player=[round(px), round(py)], riding=game.is_junundu(),
                    gadgets_near=self._gadgets_for_log(px, py))
        self._skill_try = None
        self._save_slot(a, s, False)
        return False

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
            ground = self._ride_ground = s.ride_ground = RideGround()
            ground.ridden.update(s.__dict__.pop("_resume_ridden", None) or ())     # kept across a reload
        s.ride_ground = ground               # where the session saves it from, for a reload
        if now - self.__dict__.get("_ride_seen", 0.0) >= 0.3:
            self._ride_seen = now
            fresh = now - self.__dict__.get("_revived_at", 0.0) < 15.0     # lost by dying, not set down
            if ground.note(eng.player_xy, game.is_junundu(), alive=not fresh) == "off":
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
            child = BT.Move(pos=tuple(v), tolerance=250.0)
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
                self.__dict__.setdefault("_actions_done", set()).add(i)
                s.log.event("route_action", stage="skipped", do=do, value=v, at=a.get("at"), why="not riding")
                self._save_slot(a, s, False)
                return False
            child = BT.Move(pos=tuple(a["pos"]), tolerance=120.0) if a.get("pos") else BT.Wait(duration_ms=100)
        elif do == "dropbundle":
            child = BT.DropBundle(log=True)
        else:
            self.__dict__.setdefault("_actions_done", set()).add(i)
            s.log.event("route_action", stage="unsupported", do=do, at=a.get("at"))
            return False
        self._drop_child()
        self._child = child
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
        self._gadget_phase = None
        i = self._key[1]
        acts = eng.guide_actions
        a = acts[i] if 0 <= i < len(acts) else {"do": "gadget"}
        if i >= 0:
            self.__dict__.setdefault("_actions_done", set()).add(i)
        riding = game.is_junundu()
        if a["do"] == "gadget":
            if riding:
                self._ride_from = tuple(a.get("value") or self._target or (0, 0))
                ground = self.__dict__.get("_ride_ground")
                if ground is not None:
                    ground.note(eng.player_xy, True)         # known at once, not at the next look
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
        if len(hist) < 12 or getattr(self, "_direct", None) is not None:
            return
        spots = {(h[1], h[2]) for h in hist}
        px = sum(h[3][0] for h in hist) / len(hist)
        py = sum(h[3][1] for h in hist) / len(hist)
        if (len(spots) > 4 or hist[0][4] != hist[-1][4]
                or any(math.hypot(h[3][0] - px, h[3][1] - py) > 900.0 for h in hist)):
            return
        hist.clear()
        obj = eng.objective
        loops = self.__dict__.setdefault("_loops", {})
        loops[key] = loops.get(key, 0) + 1
        self.session.log.event("step_loop", key=key, player=[round(px), round(py)], spots=len(spots),
                               times=loops[key], target=None if obj is None else [round(obj.pos[0]), round(obj.pos[1])])
        if eng.nav.stacked(px, py) and not any(math.hypot(px - wx, py - wy) < 300.0 for wx, wy in eng.walls):
            eng.walls.append((px, py))       # a climb the map allows and the game does not: route round it
            eng.player_node = None
        if loops[key] == 1 and obj is not None:
            self._direct = (now + 40.0, (obj.pos[0], obj.pos[1]), key)
        else:
            eng.mem.blocked[key] = eng.cfg.max_failures
            eng.objective = None
            self.session.log.event("step_loop", key=key, gave_up=True)

    def get_children(self):
        return [self._child.root] if self._child is not None else []

    def _start_step(self, step, key):
        x, y, radius = step
        # Map cells can be slivers of ground: stand on the exact spot, not somewhere near it.
        exact = self.session.engine.objective is not None and self.session.engine.objective.kind == "carto"
        self._child = BT.MoveAndKill(pos=(x, y), clear_area_radius=radius, move_tolerance=40.0 if exact else 150.0)
        self._key, self._target, self._started = key, (x, y), time.time()
        self.session.log.event("step_start", key=key, target=[round(x), round(y)], clear_radius=round(radius))
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
        if (walking or self._stuck_n >= 2) and self._target is not None and eng.nav.stacked(px, py):
            # Two levels on top of each other here (a bridge): the walk was aimed at the other
            # level, nothing is in the way. Marking it walled the bridge off for good in Bahdok
            # Caverns. Let the game's own pathing take us at the objective instead.
            obj = eng.objective
            # The map joins the two levels here but the party cannot climb between them: keep
            # paths off this spot for the rest of the visit (not saved: it is not scenery), so the
            # planner finds the real way up (Bahdok: the ramp round the west side).
            if not any(math.hypot(px - wx, py - wy) < 300.0 for wx, wy in eng.walls):
                eng.walls.append((px, py))
                if eng._wall_cuts((px, py), (px, py)):
                    eng.soft_walls.add((px, py))     # the only way through: care, not a wall
                eng.player_node = None
            if obj is not None and getattr(self, "_direct", None) is None:
                self._direct = (now + 25.0, (obj.pos[0], obj.pos[1]), self._key)
            s.log.event("wall", player=[round(px), round(py)], target=list(self._target), stacked=True,
                        times_here=self._stuck_n, direct_walk=obj is not None)
            return
        if (walking or self._stuck_n >= 2) and self._target is not None:
            spot = eng.add_wall((px, py), self._target, detour=self._stuck_n >= 2)
            if walking and eng.add_snag(spot):
                s.save_snags()               # scenery does not move: remember it for every later run here
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
            self._pull_reset("leader died")
            if not self._was_dead:
                self._was_dead = True
                px, py = eng.player_xy
                foes_near = sum(1 for e in eng.mem.enemies.values()
                                if e.alive and not e.lost and math.hypot(e.xy[0] - px, e.xy[1] - py) < 1800.0)
                hazard = foes_near == 0
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
                s.log.event("death", player=list(eng.player_xy), key=self._key, deaths=eng.deaths, foes_near=foes_near,
                            riding=game.is_junundu(),
                            enemies_in_view=sum(1 for e in eng.mem.enemies.values() if e.alive and e.in_range),
                            consumables=s.pcons.state(eng) if s.cfg.pcons_on else "off")
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
            self._recover_until = now + 14.0 if hot else 0.0
            s.log.event("revived", player=[round(eng.player_xy[0]), round(eng.player_xy[1])], riding=game.is_junundu())
        if now < self.__dict__.get("_hold_until", 0.0):
            self._idle_at = now              # standing still on purpose is not being stuck
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
            left = self.__dict__.get("_dead_left")
            if left is not None and cond["dead_allies"] < left:
                left = self._dead_left = None
            why = ("someone is dead" if cond["dead_allies"] and left is None else
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
                    self._dead_left = cond["dead_allies"]
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
            if now < until and not fighting and math.hypot(tx - px_, ty - py_) > 600.0:
                self._drop_child()
                if now - self.__dict__.get("_direct_move", 0.0) >= 1.5:
                    self._direct_move = now
                    game.move_to(tx, ty)
                return S.RUNNING
            self._direct = None
            s.log.event("step_loop", key=dkey, direct_walk="done", gap=round(math.hypot(tx - px_, ty - py_)),
                        fighting=fighting)

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
                return S.FAILURE
            key = eng.objective.key
            if self._escape is not None:
                # just marked an obstacle: first a short step clear of it, then back to the plan
                step, key = (self._escape[0], self._escape[1], self._escape_radius), ("escape", int(now))
                self._escape, self._escape_until = None, now + self._escape_secs
                self._escape_secs, self._escape_radius = 6.0, 900.0
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
            if miss > 400.0:
                eng.step_failed()            # could not get there: count it against the objective
            self._drop_child()
        return S.RUNNING


from .cross import CrossNode      # noqa: E402  (kept importable from here)
