"""MapRunNode: one queued area from start to finish: travel, set up, walk in, vanquish, leave."""
import math
import json
import os
import time

import PySystem
from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Sources.ApoSource.ApoBottingLib import wrappers as BT

from ..core import builds as teambuilds
from ..core import elites
from . import game

from .node import AdaptiveNode, CrossNode
from .maprun_setup import SetupMixin

MAX_STARTS = 4           # times one area may be started over (after wipes that end in town) per run

LEG_RETRIES = 3          # times a leg of the journey is tried again before giving up on the area


class MapRunNode(SetupMixin, BehaviorTree.Node):
    """One area from start to finish: get there (map travel, then on foot through whatever lies
    between), vanquish, walk into a locked outpost if one is reachable, come back. It always
    reports success to the tree so one bad area cannot stop an overnight queue; the real
    outcome is recorded in the campaign results and the run log.

    Doors are only ever used on the way IN and after the vanquish is complete. During the
    vanquish every door is fenced off, because leaving the area would reset it."""

    def __init__(self, session, campaign, map_id, stay=False):
        super().__init__(name=f"Area {map_id}", node_type="MapRun", node_category="action")
        self.stay = stay                 # a single run started by hand: do not resign at the end
        self.session, self.campaign, self.map_id = session, campaign, map_id
        self.entry = campaign.travel.get(map_id)
        self._phase, self._child, self._started, self._outcome = "start", None, 0.0, ""
        self._legs, self._home = [], 0
        self._restore_secondary = False
        self._team = None

    def reset(self):
        super().reset()
        self._drop()
        self._phase = "start"

    def _drop(self):
        if self._child is not None:
            try:
                from .node import retire
                retire(self._child)          # parked, not destroyed: see AdaptiveNode._drop_child
            except Exception:
                pass
        self._child = None

    def get_children(self):
        return [self._child.root] if self._child is not None else []

    # ---- capture signets ----
    def _always(self, child, what, limit_s=None):
        """Run `child`; if it fails, log it and carry on. A failed errand (the skill trainer was
        not reached) must not leave the party in the wrong town with the rest of the plan skipped."""
        log, mid = self.session.log, self.map_id
        tree = BehaviorTree(child)

        began = [0.0]

        def run(node):
            tree.root.blackboard = node.blackboard
            began[0] = began[0] or time.time()
            if limit_s is not None and time.time() - began[0] > limit_s:
                # an errand that is going nowhere (pacing a room without reaching the trader) is
                # given up, and the run goes on without it
                log.event("campaign", map_id=mid, phase="errand_failed", what=what, in_map=game.map_id(), why=f"took over {int(limit_s)} s")
                self.session.errand_note = f"{what}: given up after {int(limit_s)} s"
                game.move_to(*game.player_xy())
                return BehaviorTree.NodeState.SUCCESS
            state = tree.root.tick()
            if state == BehaviorTree.NodeState.FAILURE:
                log.event("campaign", map_id=mid, phase="errand_failed", what=what, in_map=game.map_id())
                return BehaviorTree.NodeState.SUCCESS
            return state
        return BehaviorTree.ActionNode(name=f"Try: {what}", action_fn=run)

    def _shrink_node(self, limit, outpost=None):
        """Dismiss heroes and henchmen and wait until the game agrees the party is small enough.
        A single dismiss right after arriving in an outpost was ignored (the party list was not
        ready yet), so this waits first, checks the count, and tries again up to five times.
        With `outpost` (and no limit), first works out whether travelling there needs it at all."""
        st = {"t0": 0.0, "last": 0.0, "tries": 0, "limit": limit, "logged": False}
        log = self.session.log

        def run():
            now = time.time()
            st["t0"] = st["t0"] or now
            if st.get("stage") is not None:
                if not game.map_ready() or game.map_id() != st["stage"] or game.is_explorable():
                    if now - st["stage_t"] > 60.0:
                        log.event("campaign", map_id=self.map_id, phase="shrink_party", result="staging travel failed")
                        return BehaviorTree.NodeState.SUCCESS
                    if now - st["stage_t"] > 25.0 and not st.get("retried"):
                        st["retried"] = True
                        game.travel_to(st["stage"])
                    return BehaviorTree.NodeState.RUNNING
                st["stage"], st["t0"] = None, now           # there: let the party list load, then shrink
                return BehaviorTree.NodeState.RUNNING
            if not game.map_ready():
                return BehaviorTree.NodeState.RUNNING if now - st["t0"] < 30.0 else BehaviorTree.NodeState.SUCCESS
            if outpost is not None and not st["logged"]:
                cap = game.map_max_party(outpost)
                if not cap or game.party_count() <= cap:
                    return BehaviorTree.NodeState.SUCCESS      # nothing to do: no wait
            if now - st["t0"] < 3.0:                       # let the outpost finish loading the party
                return BehaviorTree.NodeState.RUNNING
            if outpost is not None and not st["logged"]:
                st["logged"] = True
                cap, have = game.map_max_party(outpost), game.party_count()
                if not cap or have <= cap:
                    return BehaviorTree.NodeState.SUCCESS
                st["limit"] = cap
                if self.session.cfg.multibox:
                    # accounts cannot be sent home like heroes: this area is not for this party
                    self._too_many = f"party of {have} is too large for {game.map_name(outpost)} (limit {cap})"
                    return BehaviorTree.NodeState.FAILURE
                if game.is_explorable():
                    # Heroes cannot be sent home out here, and the game will not travel this party
                    # to a smaller outpost (Cliffs of Dohjok -> Blacktide Den stopped on the yes/no
                    # window). Travel first to an outpost that takes the whole party, shrink there.
                    stage = self._staging_outpost(have)
                    log.event("campaign", map_id=self.map_id, phase="shrink_party", have=have, limit=cap,
                              outpost=game.map_name(outpost), from_explorable=True,
                              staging=game.map_name(stage) if stage else None)
                    if stage is None:
                        return BehaviorTree.NodeState.SUCCESS   # nothing better: let the travel try
                    st.update(stage=stage, stage_t=now)
                    game.travel_to(stage)
                    return BehaviorTree.NodeState.RUNNING
                log.event("campaign", map_id=self.map_id, phase="shrink_party", have=have, limit=cap,
                          outpost=game.map_name(outpost), multibox=self.session.cfg.multibox)
            limit = st["limit"]
            have = game.party_count()
            if have <= limit:
                log.event("campaign", map_id=self.map_id, phase="shrink_party", result="ok", party=have,
                          tries=st["tries"])
                return BehaviorTree.NodeState.SUCCESS
            if now - st["last"] >= 2.5:
                if st["tries"] >= 5:
                    log.event("campaign", map_id=self.map_id, phase="shrink_party", result="still too large",
                              party=have, limit=limit)
                    return BehaviorTree.NodeState.SUCCESS   # let the travel try; it records the failure
                st["tries"] += 1
                st["last"] = now
                try:
                    game.shrink_party()
                except Exception as e:
                    log.event("campaign", map_id=self.map_id, phase="shrink_party", error=repr(e))
            return BehaviorTree.NodeState.RUNNING
        return BehaviorTree.ActionNode(name="Send the heroes home", action_fn=run)

    def _staging_outpost(self, have):
        """An unlocked outpost that takes a party of `have`: one beside where we are if possible."""
        c = self.campaign
        try:
            ok = [o for o in c.unlocked(refresh=True) if game.map_max_party(o) >= have and game.real_outpost(o)]
        except Exception:
            return None
        if not ok:
            return None
        near = []
        try:
            near = [o for o, _leg in c.world.outposts_from(game.map_id()) if o in ok]
        except Exception:
            pass
        return (near or sorted(ok))[0]

    def _action(self, name, fn):
        def run():
            try:
                fn()
            except Exception as e:
                self.session.log.event("campaign", map_id=self.map_id, phase="signets", error=repr(e), step=name)
            return BehaviorTree.NodeState.SUCCESS
        return BehaviorTree.ActionNode(name=name, action_fn=run)

    def _set(self, phase, child=None):
        self._drop()
        self._phase, self._child = phase, child
        self.campaign.phase = phase
        self.session.log.event("campaign", map_id=self.map_id, phase=phase)

    # ---- getting there ----
    def _route_options(self):
        """Every known way in, as (label, legs): the NPC entry, the recorded vanquish bots' walk
        out of their outpost (proven: people run these), and the world map's own route."""
        c, out = self.campaign, []
        try:
            from .npc_entries import entries
            ent = entries().get(self.map_id)
        except Exception:
            ent = None
        if ent:
            talk = {"do": "npc_entry", "npc": list(ent["npc"]), "dialogs": ent["dialogs"], "expect": self.map_id}
            here = game.map_id() if game.map_ready() else 0
            if ent.get("via") and here == ent["via"] and game.is_explorable():
                out.append(("npc", [talk]))          # already standing in the area the NPC is in
            elif game.map_unlocked(ent["outpost"]):
                legs = [{"do": "travel", "outpost": ent["outpost"], "hm": True}]
                if ent.get("via"):               # the NPC is out in a neighbouring area
                    legs.append({"do": "gate", "path": [list(p) for p in ent["via_path"]], "expect": ent["via"]})
                out.append(("npc", legs + [talk]))
        e = self.entry
        if e and e["plain"] and game.map_unlocked(e["outpost"]):
            legs = [{"do": "travel", "outpost": e["outpost"], "hm": True}]
            first = e["transit"][0]["map"] if e["transit"] else self.map_id
            legs.append({"do": "gate", "path": e["outpost_path"], "expect": first})
            for i, leg in enumerate(e["transit"]):
                nxt = e["transit"][i + 1]["map"] if i + 1 < len(e["transit"]) else self.map_id
                legs.append({"do": "gate", "path": leg["path"], "expect": nxt, "slow": True})
            out.append(("recorded", legs))
        if c.world is not None:
            legs = c.world.route(self.map_id, c.unlocked(refresh=True))
            if legs and not any(legs == l for _n, l in out):
                out.append(("world", legs))
        # the way that worked last time first
        good = (c.results.get(self.map_id) or {}).get("route")
        out.sort(key=lambda o: o[0] != good)
        return out

    def _plan_route(self):
        opts = self._route_options()
        if not opts:
            return None
        k = getattr(self, "_route_skip", 0)
        if k >= len(opts):
            return None
        self._route_label = opts[k][0]
        self._route_count = len(opts)
        return opts[k][1]

    def _retry_in_hard_mode(self):
        """Arrived in normal mode and no outpost leads straight in: go round the same way again,
        hard mode all the way (the only way into an area reached through another one)."""
        if getattr(self, "_force_hm", False):
            return False
        self._force_hm = True
        legs = self._plan_route() or []
        if not legs or legs[0]["do"] != "travel":
            return False
        self.session.log.event("campaign", map_id=self.map_id, phase="normal_mode",
                               note="no outpost leads straight in: going round again in hard mode the whole way")
        self._legs = legs
        self._next_leg()
        return True

    def _log_npcs_near(self, why, radius=2000.0):
        """Who stands near where a way in failed: a gate that needs a word with someone (the Key
        of Ahdashim) shows up here by name, so it can be added as an NPC entry."""
        try:
            px, py = game.player_xy()
            near = []
            for agent_id, model, x, y in game.npcs_in_sight():
                d = math.hypot(x - px, y - py)
                if d <= radius:
                    near.append({"name": game.agent_name(agent_id), "model": model, "pos": [round(x), round(y)],
                                 "dist": round(d)})
            near.sort(key=lambda n: n["dist"])
            self.session.log.event("campaign", map_id=self.map_id, phase="npcs_near", why=why, in_map=game.map_id(),
                                   player=[round(px), round(py)], npcs=near[:12])
        except Exception as e:
            self.session.log.event("campaign", map_id=self.map_id, phase="npcs_near", error=repr(e))

    def _switch_route(self, why):
        """This way in failed: start over from the next known way, if there is one."""
        k = getattr(self, "_route_skip", 0) + 1
        if k >= getattr(self, "_route_count", 1):
            return False
        self._route_skip = k
        legs = self._plan_route()
        if not legs:
            return False
        self.session.log.event("campaign", map_id=self.map_id, phase="route_switch", why=why, now=self._route_label,
                               legs=[{kk: v for kk, v in leg.items() if kk != "path"} for leg in legs])
        self._legs, self._leg_tries = legs, 0
        self._next_leg()
        return True

    def _leg_tree(self, leg):
        """The behaviour for one leg of the journey (or of the walk into an outpost afterwards)."""
        here = game.map_id()
        if leg["do"] == "travel":
            self._home = leg["outpost"]
            # Hard mode unless the trip walks through other areas (normal mode is safer there) -
            # but a recorded entry (an NPC, or the vanquish bots' own walk) is the way in: going
            # through it in normal mode only means arriving in normal mode with no way to switch.
            hm = (leg.get("hm") or getattr(self, "_force_hm", False)
                  or not self._crosses_other_areas(self._legs))
            if not hm:
                self.session.log.event("campaign", map_id=self.map_id, phase="normal_mode",
                                       note="walking through other areas to get there: normal mode until the last outpost")
            travel = BT.Sequence(name=f"Outpost:{leg['outpost']}", map_id_or_name=leg["outpost"], hard_mode=hm,
                                 children=[BT.Wait(duration_ms=1500)])
            # The party size is checked when the travel is about to happen, not when this leg is
            # planned: planned the moment the last area ended, the map was still loading, the
            # check was skipped, and the game's "party too large" yes/no window stopped
            # everything (Blacktide Den, after Cliffs of Dohjok).
            return BT.Sequence(name="ShrinkThenTravel", children=[self._shrink_node(None, leg["outpost"]), travel])
        if leg["do"] == "npc_entry":
            expect, t0 = leg["expect"], {}

            def arrived():
                t0.setdefault("t", time.time())
                if game.map_ready() and game.map_id() == expect:
                    return BehaviorTree.NodeState.SUCCESS
                if time.time() - t0["t"] > 45.0:
                    self.session.log.event("campaign", map_id=self.map_id, phase="npc_entry", note="no map change 45 s after the dialog")
                    return BehaviorTree.NodeState.FAILURE
                return BehaviorTree.NodeState.RUNNING
            d = leg["dialogs"]
            steps = []
            if game.is_explorable():
                # the NPC is out in an area: get to it with the engine (pathing, fighting) first
                steps.append(BehaviorTree(AdaptiveNode(self.session, target_map_id=game.map_id(),
                                                       transit={"xy": list(leg["npc"])})))
            steps.append(BT.MoveAndDialog(pos=tuple(leg["npc"]), dialog_id=d[0]))
            for dialog in d[1:]:
                steps += [BT.Wait(duration_ms=700), BT.SendDialog(dialog_id=dialog)]
            steps.append(BehaviorTree(BehaviorTree.ActionNode(name="Wait for the area", action_fn=arrived)))
            self.session.log.event("campaign", map_id=self.map_id, phase="npc_entry", npc=leg["npc"], dialogs=d)
            return BT.Sequence(name="NpcEntry", children=steps)
        if leg["do"] == "leave_town":
            return BehaviorTree(BehaviorTree.SubtreeNode(name="LeaveTown", subtree_fn=lambda _n: self._leave_town_tree(leg)))
        if leg["do"] == "gate":
            # Walk the recorded path, then push on through the doorway. Its last point is often
            # just short of the trigger, and walking "to" it within the usual tolerance left the
            # party standing in the doorway (Yohlon Haven into Marga Coast). The push keeps going
            # the way the path was heading, and tries the other sides if that does not work.
            path = [tuple(p) for p in leg["path"]]
            if not path:
                return BT.MoveAndExitMap(path, target_map_id=leg["expect"], timeout_ms=60_000, log=True)
            last = path[-1]
            prev = path[-2] if len(path) > 1 else game.player_xy()
            dx, dy = last[0] - prev[0], last[1] - prev[1]
            n = math.hypot(dx, dy) or 1.0
            beyond = [last[0] + dx / n * 400.0, last[1] + dy / n * 400.0] if n > 1.0 else list(last)
            push = BehaviorTree(CrossNode(self.session, {"xy": list(last), "beyond": beyond, "expect": leg["expect"]},
                                          here, timeout_s=600.0 if leg.get("slow") else 75.0))
            if len(path) == 1:
                return push
            return BT.Sequence(name="GateWalk", children=[BT.Move(pos=path[:-1], tolerance=150.0), push])
        cross = BehaviorTree(CrossNode(self.session, leg, here))
        if not game.is_explorable():
            return cross                                 # inside an outpost: nothing to fight, just walk out
        # in an area: cross it with the engine (pathing and fighting), then step through the door
        target = leg.get("portal") or leg["xy"]
        walk = BehaviorTree(AdaptiveNode(self.session, target_map_id=here, transit={"xy": target},
                                         arrive_map=leg.get("expect")))
        return BT.Sequence(name="CrossArea", children=[walk, cross])

    def _bad_exits_path(self):
        return os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "town_exits.json")

    def _save_bad_town_exits(self):
        try:
            with open(self._bad_exits_path(), "w", encoding="utf-8") as f:
                json.dump(self.campaign.__dict__.get("_bad_town_exits", {}), f)
        except Exception:
            pass

    def _leave_town_tree(self, leg):
        if "_bad_town_exits" not in self.campaign.__dict__:
            try:
                with open(self._bad_exits_path(), encoding="utf-8") as f:
                    self.campaign.__dict__["_bad_town_exits"] = json.load(f)
            except Exception:
                self.campaign.__dict__["_bad_town_exits"] = {}
        """Walk out of this outpost through its portal (the one nearest where we stand)."""
        portals = []
        try:
            portals = game.travel_portals()          # real portals only (arrival points are not ways out)
        except Exception:
            pass
        px, py = game.player_xy()
        hint = None
        try:
            hint = self.campaign.world.town_exit_hint(leg.get("from") or game.map_id())
        except Exception:
            pass
        # Best guide: the arrival point tagged with the map we want sits right beside the portal
        # to it (a party coming from there appears at it). The world map's door is only a fallback
        # (for Seafarer's Rest it pointed at the Silent Surf portal).
        tagged = []
        try:
            tagged = [(x, y) for x, y, m in game.arrival_points() if m == int(leg["expect"])]
        except Exception:
            pass
        bad = set(map(tuple, self.campaign.__dict__.setdefault("_bad_town_exits", {}).get(str(game.map_id()), [])))
        portals = [p for p in portals if (round(p[0]), round(p[1])) not in bad] or portals
        ref = (tagged[0] if tagged else None) or hint or (px, py)
        self.session.log.event("campaign", map_id=self.map_id, phase="leave_town", portals=[list(p) for p in portals],
                               hint=list(hint) if hint else None, arrival_for_target=[list(t) for t in tagged],
                               player=[round(px), round(py)])
        if not portals:
            if hint is None:
                return BehaviorTree(BehaviorTree.ActionNode(name="No portal", action_fn=lambda: BehaviorTree.NodeState.FAILURE))
            portals = [hint]
        x, y = min(portals, key=lambda p: (p[0] - ref[0]) ** 2 + (p[1] - ref[1]) ** 2)
        self._town_exit = (game.map_id(), (round(x), round(y)))
        return BehaviorTree(CrossNode(self.session, {"xy": [x, y], "beyond": [x, y], "expect": leg["expect"]},
                                      game.map_id(), timeout_s=120.0))

    def _crosses_other_areas(self, legs):
        """True when these legs walk through an explorable area other than the one to vanquish
        (getting there through other areas, or opening an outpost on the way)."""
        c = self.campaign
        towns = c.world.all_outposts() if c.world is not None else set()
        for leg in legs:
            for m in (leg.get("expect"), leg.get("through")):
                if m and int(m) != self.map_id and int(m) not in towns:
                    return True
        return False

    def _next_leg(self):
        """Start the next leg, or the vanquish once we are standing in the area."""
        if game.map_ready() and game.map_id() == self.map_id and game.is_explorable():
            if not game.hard_mode() and not getattr(self, "_hm_retry", False):
                # got here in normal mode (no outpost on the way to switch in): go round by an
                # outpost now open, in hard mode, if there is one that leads straight in
                self._hm_retry = True
                legs = self._plan_route() or []
                if legs and legs[0]["do"] == "travel" and not self._crosses_other_areas(legs[1:]):
                    self.session.log.event("campaign", map_id=self.map_id, phase="normal_mode",
                                           note="arrived in normal mode: going back round in hard mode")
                    self._legs = legs
                    self._next_leg()
                    return
                # No such outpost open yet: walk into one from here that has its own way into this
                # area, then start again from it in hard mode.
                if self._start_unlock(need_gate=True):
                    self._rehome_hm = True
                    self.session.log.event("campaign", map_id=self.map_id, phase="normal_mode",
                                           note="arrived in normal mode: opening an outpost to come back in hard mode")
                    return
                if self._retry_in_hard_mode():
                    return
                self._outcome = "reached the area in normal mode, with no outpost to switch to hard mode from"
                self.session.log.event("campaign", map_id=self.map_id, phase="normal_mode", note=self._outcome)
                self._finish()
                return
            if getattr(self, "_route_label", ""):
                self.campaign.results.setdefault(self.map_id, {"attempts": 0})["route"] = self._route_label
            self._set("vanquish", BehaviorTree(AdaptiveNode(self.session, target_map_id=self.map_id)))
            return
        if not self._legs:
            self._outcome = f"route ended in {game.map_name(game.map_id())}, not the area"
            self._finish()
            return
        leg = self._legs.pop(0)
        if (leg["do"] != "travel" and self._home and game.map_ready() and not game.is_explorable()
                and game.map_id() != self._home and not getattr(self, "_rehomed", False)):
            # in the wrong town (an errand went wrong): go back before walking at a gate that is not here
            self._rehomed = True
            self._legs.insert(0, leg)
            self.session.log.event("campaign", map_id=self.map_id, phase="wrong_town", in_map=game.map_id(), home=self._home)
            leg = {"do": "travel", "outpost": self._home}
        self._leg = leg
        self._leg_tries, self._leg_from = 0, game.map_id()
        tree = self._leg_tree(leg)
        want_hm = (getattr(self, "_force_hm", False) or getattr(self, "_route_label", "") in ("npc", "recorded")
                   or not self._crosses_other_areas([leg] + self._legs))
        if leg["do"] != "travel" and game.map_ready() and not game.is_explorable() and not game.hard_mode() and want_hm:
            # an outpost on the way, and from here the walk leads straight into the area: back to hard mode
            self.session.log.event("campaign", map_id=self.map_id, phase="hard_mode", in_map=game.map_id())
            tree = BT.Sequence(name="HardModeThenWalk", children=[BT.SetHardMode(hard_mode=True),
                                                                  BT.Wait(duration_ms=800), tree])
        self._set("travel" if leg["do"] == "travel" else "walking", tree)

    def _tick_impl(self):
        from .guard import guarded_tick

        def give_up():
            self._outcome = "stopped by repeated errors (see log)"
            try:
                self._conclude()
            except Exception:
                self._phase = "done"
            return BehaviorTree.NodeState.SUCCESS
        return guarded_tick(self, self.session, give_up, BehaviorTree.NodeState.RUNNING)

    def _tick_core(self):
        S, c = BehaviorTree.NodeState, self.campaign
        if self._phase == "start":
            c.current = self.map_id
            self._started = self._started or time.time()
            # Each wipe that ends in town restarts this area from the top. Past a few of those
            # the area is beyond this team: record it and let the queue move on.
            starts = c.__dict__.setdefault("_starts", {})
            n, since = starts.get(self.map_id, (0, time.time()))
            if time.time() - since > 6 * 3600:
                n, since = 0, time.time()
            starts[self.map_id] = (n + 1, since)
            if n + 1 > MAX_STARTS and not self.session.cfg.keep_at_it and not (game.map_ready() and game.map_id() == self.map_id and game.is_explorable()):
                self.session.log.event("campaign", map_id=self.map_id, phase="gave_up", starts=n + 1)
                c.record(self.map_id, f"gave up after being sent back to town {n} times", self._started)
                c.record(      # twice on purpose: reaches the attempt limit, which takes it off the queue
self.map_id, f"gave up after being sent back to town {n} times", self._started)
                self._phase = "done"
                c.current, c.phase = None, ""
                return S.SUCCESS
            if self.map_id in game.vanquished_ids():
                c.record(self.map_id, "complete", self._started)       # done some other time
                return S.SUCCESS
            # Already standing in the area (campaign started here, or the tree restarted this step
            # after a wipe): carry on with the vanquish instead of travelling out and back in.
            if game.map_ready() and game.map_id() == self.map_id and game.is_explorable():
                self._set("vanquish", BehaviorTree(AdaptiveNode(self.session, target_map_id=self.map_id)))
                return S.RUNNING
            self._legs = self._plan_route() or []
            if not self._legs:
                self._outcome = "no route from your unlocked outposts"
                self._conclude()
                return S.SUCCESS
            self.session.log.event("campaign", map_id=self.map_id, phase="route", way=getattr(self, "_route_label", ""),
                                   ways_known=getattr(self, "_route_count", 1),
                                   legs=[{k: v for k, v in leg.items() if k != "path"} for leg in self._legs])
            # Standing in an area the route goes through (revived at a shrine after a wipe on the
            # way, or started from here): carry on from this area. Travelling back to the outpost
            # would bring back everything already killed.
            if game.map_ready() and game.is_explorable():
                here = game.map_id()
                at = [i for i, leg in enumerate(self._legs) if leg["do"] == "door" and leg.get("through") == here]
                if at:
                    home = next((leg["outpost"] for leg in self._legs if leg["do"] == "travel"), None)
                    self._home = home if home is not None else getattr(self, "_home", None)
                    self._legs = self._legs[at[-1]:]
                    self.session.log.event("campaign", map_id=self.map_id, phase="resume", from_map=here,
                                           legs_left=len(self._legs))
            self._next_leg()
            return S.RUNNING

        if self._phase == "done":
            return S.SUCCESS

        self._child.root.blackboard = self.blackboard
        state = self._child.root.tick()
        if state == S.RUNNING:
            return S.RUNNING

        if self._phase == "travel":                       # arrived in the starting outpost
            if state != S.SUCCESS:
                if not getattr(self, "_too_many", "") and self._switch_route("could not travel to the outpost"):
                    return S.RUNNING
                self._outcome = getattr(self, "_too_many", "") or "could not travel to the outpost"
                self._conclude()
                return S.SUCCESS
            if not c.capture_template:
                c.capture_template = game.bar_template()  # first run: the bar you started with
                c.save()
            tree = self._secondary_tree()
            if tree is not None:
                self._set("secondary", tree)
            else:
                self._after_secondary()
            return S.RUNNING

        if self._phase == "secondary":
            self._after_secondary()
            return S.RUNNING

        if self._phase == "signets":
            self._next_leg()
            return S.RUNNING

        if self._phase == "walking":
            expect, here = self._leg.get("expect"), game.map_id()
            if state != S.SUCCESS and game.map_ready():
                self._log_npcs_near("walk failed")
            if (state != S.SUCCESS and game.map_ready() and getattr(self, "_leg_tries", 0) >= 1
                    and not game.party_defeated() and not game.is_explorable()
                    and self._switch_route(f"stuck in {game.map_name(here)}")):
                return S.RUNNING                      # the same way failed twice: another way in
            if (state != S.SUCCESS and game.map_ready() and here == getattr(self, "_leg_from", None)
                    and not game.party_defeated() and getattr(self, "_leg_tries", 0) < LEG_RETRIES):
                # still in the same place and able to walk: try the leg again rather than leave
                self._leg_tries += 1
                self.session.log.event("campaign", map_id=self.map_id, phase="retry_leg", attempt=self._leg_tries,
                                       in_map=here, result=self.session.result)
                self._set("walking", self._leg_tree(self._leg))
            elif state != S.SUCCESS:
                self._outcome = f"got stuck on the way, in {game.map_name(here)}"
                self._finish()
            elif expect and here != expect:
                # the world map had this door wrong: remember, and do not vanquish the wrong place
                if self._leg.get("do") == "leave_town" and getattr(self, "_town_exit", None):
                    town, xy = self._town_exit
                    bad = self.campaign.__dict__.setdefault("_bad_town_exits", {}).setdefault(str(town), [])
                    if list(xy) not in bad:
                        bad.append(list(xy))
                    self._save_bad_town_exits()
                    self.session.log.event("campaign", map_id=self.map_id, phase="leave_town", wrong_exit=list(xy),
                                           led_to=here, town=town)
                self._outcome = f"door led to {game.map_name(here)}, expected {game.map_name(expect)}"
                if not game.is_explorable() and self._switch_route(self._outcome):
                    return S.RUNNING
                self._finish()
            else:
                self._next_leg()
            return S.RUNNING

        if self._phase == "vanquish":
            self._outcome = "complete" if state == S.SUCCESS else (self.session.result or "failed")
            if state == S.SUCCESS and self.session.cfg.unlock_outposts and self._start_unlock():
                return S.RUNNING
            self._finish()
            return S.RUNNING

        if self._phase == "unlock":
            self.session.log.event("campaign", map_id=self.map_id, phase="unlock_result",
                                   now_in=game.map_name(game.map_id()), ok=(state == S.SUCCESS))
            if getattr(self, "_rehome_hm", False):
                self._rehome_hm = False
                legs = self._plan_route() or []
                if state == S.SUCCESS and legs and not self._crosses_other_areas(legs[1:]):
                    self._legs = legs
                    self._next_leg()                 # travel (hard mode) and walk straight in
                    return S.RUNNING
                if self._retry_in_hard_mode():
                    return S.RUNNING
                self._outcome = "reached the area in normal mode, with no outpost to switch to hard mode from"
                self._conclude()
                return S.SUCCESS
            self._finish()
            return S.RUNNING

        # phase == "return"
        self._conclude()
        return S.SUCCESS

    def _start_unlock(self, need_gate=False):
        """The vanquish is complete, so leaving is safe: walk into a locked outpost that can be
        reached from here, which opens it for map travel from now on."""
        c = self.campaign
        if c.world is None:
            return False
        px, py = game.player_xy()
        # A map the game has no name for is not a real outpost (Gandara and Dejarin Estate both
        # "unlocked" 383, which is the mission map: the door led back to Pogahn Passage).
        locked = [(o, leg) for o, leg in c.world.outposts_from(self.map_id) if not game.map_unlocked(o)]
        options = [(o, leg) for o, leg in locked if game.real_outpost(o)]
        if len(options) < len(locked):
            self.session.log.event("campaign", map_id=self.map_id, phase="unlock_skipped",
                                   not_outposts=[o for o, _l in locked if (o, _l) not in options])
        if need_gate:                    # only an outpost with its own gate into this area will do
            options = [(o, leg) for o, leg in options
                       if any(g.get("to_map") == self.map_id for g in c.world.gates.get(o, []))]
        if not options:
            return False
        o, leg = min(options, key=lambda t: (t[1]["xy"][0] - px) ** 2 + (t[1]["xy"][1] - py) ** 2)
        self._leg = leg
        self.session.log.event("campaign", map_id=self.map_id, phase="unlock", outpost=game.map_name(o))
        if leg["do"] == "walk_in":
            # same walkable piece: head for the outpost's own arrival point; the gate is on the way
            walk = BehaviorTree(AdaptiveNode(self.session, target_map_id=self.map_id,
                                             transit={"xy": leg["xy"]}, arrive_map=o))
            push = BehaviorTree(CrossNode(self.session, {"xy": leg["xy"], "beyond": leg["xy"], "expect": o}, self.map_id))
            self._set("unlock", BT.Sequence(name="WalkIntoOutpost", children=[walk, push]))
        else:
            self._set("unlock", self._leg_tree(leg))
        return True

    def _finish(self):
        """Leave the area (if we are in one) before the next item in the queue."""
        if self.stay and self._outcome != "complete":
            self._conclude()                 # a hand-started run that did not finish: stay put, keep the progress
        elif game.map_ready() and game.is_explorable():
            # Never resign: that kills the party and leaves it on the "return to outpost" prompt.
            # With more areas queued, map-travel out so the next one starts from an outpost
            # (bar, signets and team are set there). With nothing left, stay where we are.
            home = self._home or (self.entry["outpost"] if self.entry else 0)
            more = any(m != self.map_id for m in self.campaign.queue)
            if not more:
                home = self._nearest_outpost() or home       # nothing queued: the closest town will do
            if self.session.refresh_party():
                # Map-travelling out of an area takes only the one who travels. With other accounts
                # in the party everyone resigns and the leader returns the party to the outpost.
                self.session.log.event("campaign", map_id=self.map_id, phase="leave", how="resign", more_queued=more)
                self._set("return", self._resign_tree())
            elif home and game.map_unlocked(home):
                self.session.log.event("campaign", map_id=self.map_id, phase="leave", to=game.map_name(home), more_queued=more)
                self._set("return", BT.Sequence(name="BackToOutpost", children=[
                    BT.Travel(target_map_id=home, hard_mode=True), BT.Wait(duration_ms=2000)]))
            else:
                self._conclude()
        else:
            self._conclude()

    def _resign_tree(self):
        st = {"t0": 0.0, "last": 0.0}
        area = self.map_id

        def back():
            now = time.time()
            st["t0"] = st["t0"] or now
            if game.map_ready() and not game.is_explorable():
                return BehaviorTree.NodeState.SUCCESS
            if now - st["t0"] > 90.0:
                self.session.log.event("campaign", map_id=area, phase="leave", note="still in the area 90 s after resigning")
                return BehaviorTree.NodeState.SUCCESS
            if game.map_ready() and now - st["last"] > 4.0 and now - st["t0"] > 4.0:
                st["last"] = now
                try:
                    game.return_to_outpost()
                except Exception as e:
                    self.session.log.event("campaign", map_id=area, phase="leave", error=repr(e))
            return BehaviorTree.NodeState.RUNNING

        return BT.Sequence(name="ResignToOutpost", children=[
            self._always(BT.Resign(multi_account=True, timeout_ms=20000), "resigning the party"),
            BehaviorTree.ActionNode(name="Return to outpost", action_fn=back),
            BT.Wait(duration_ms=3000)])

    def _nearest_outpost(self):
        """The unlocked outpost closest to this area on the world map (same continent)."""
        try:
            here = game.map_world_pos(self.map_id)
            best, best_d = None, None
            for o in self.campaign.unlocked(refresh=True):
                p = game.map_world_pos(o)
                if not here or not p or p[2] != here[2]:
                    continue
                d = (p[0] - here[0]) ** 2 + (p[1] - here[1]) ** 2
                if best_d is None or d < best_d:
                    best, best_d = o, d
            return best
        except Exception:
            return None

    def _conclude(self):
        self.campaign.record(self.map_id, self._outcome, self._started)
        self.session.log.event("campaign", map_id=self.map_id, phase="finished", result=self._outcome,
                               minutes=round((time.time() - self._started) / 60.0, 1))
        self._drop()
        self._phase = "done"
        try:
            if self._team is None and self.campaign.capture_template and game.map_ready() and not game.is_explorable():
                saved = self.campaign.capture_template
                if self._restore_secondary:
                    orig = game.template_secondary(saved)
                    if orig is not None and game.player_professions()[1] != orig:
                        game.change_secondary_direct(orig)                  # your own secondary back
                game.load_bar_template(saved)                               # back in town: your own bar again
        except Exception:
            pass
        self.campaign.current, self.campaign.phase = None, ""
