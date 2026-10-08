"""MapRunNode: one queued area from start to finish: travel, set up, walk in, vanquish, leave."""
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

    def _shrink_node(self, limit):
        """Dismiss heroes and henchmen and wait until the game agrees the party is small enough.
        A single dismiss right after arriving in an outpost was ignored (the party list was not
        ready yet), so this waits first, checks the count, and tries again up to five times."""
        st = {"t0": 0.0, "last": 0.0, "tries": 0}
        log = self.session.log

        def run():
            now = time.time()
            st["t0"] = st["t0"] or now
            if now - st["t0"] < 3.0:                       # let the outpost finish loading the party
                return BehaviorTree.NodeState.RUNNING
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
    def _plan_route(self):
        c = self.campaign
        if c.world is not None:
            legs = c.world.route(self.map_id, c.unlocked(refresh=True))
            if legs:
                return legs
        e = self.entry                                   # no world route: fall back to the recorded one
        if e and e["plain"] and game.map_unlocked(e["outpost"]):
            legs = [{"do": "travel", "outpost": e["outpost"]}]
            first = e["transit"][0]["map"] if e["transit"] else self.map_id
            legs.append({"do": "gate", "path": e["outpost_path"], "expect": first})
            for i, leg in enumerate(e["transit"]):
                nxt = e["transit"][i + 1]["map"] if i + 1 < len(e["transit"]) else self.map_id
                legs.append({"do": "gate", "path": leg["path"], "expect": nxt, "slow": True})
            return legs
        return None

    def _leg_tree(self, leg):
        """The behaviour for one leg of the journey (or of the walk into an outpost afterwards)."""
        here = game.map_id()
        if leg["do"] == "travel":
            self._home = leg["outpost"]
            travel = BT.Sequence(name=f"Outpost:{leg['outpost']}", map_id_or_name=leg["outpost"], hard_mode=True,
                                 children=[BT.Wait(duration_ms=1500)])
            limit, have = game.map_max_party(leg["outpost"]), game.party_count()
            if limit and have > limit and game.map_ready() and not game.is_explorable() and game.map_id() != leg["outpost"]:
                # The party is bigger than the destination allows: the game would put up its
                # "party too large" window and not travel. Go alone; the team for that area's size
                # is loaded on arrival anyway.
                self.session.log.event("campaign", map_id=self.map_id, phase="shrink_party", have=have,
                                       limit=limit, outpost=game.map_name(leg["outpost"]), multibox=self.session.cfg.multibox)
                if self.session.cfg.multibox:
                    # accounts cannot be sent home like heroes: this area is not for this party
                    self._too_many = f"party of {have} is too large for {game.map_name(leg['outpost'])} (limit {limit})"
                    return BehaviorTree(BehaviorTree.ActionNode(
                        name="Party too large", action_fn=lambda: BehaviorTree.NodeState.FAILURE))
                return BT.Sequence(name="ShrinkThenTravel", children=[self._shrink_node(limit), travel])
            return travel
        if leg["do"] == "gate":
            return BT.MoveAndExitMap([tuple(p) for p in leg["path"]], target_map_id=leg["expect"],
                                     timeout_ms=600_000 if leg.get("slow") else 60_000, log=True)
        cross = BehaviorTree(CrossNode(self.session, leg, here))
        if not game.is_explorable():
            return cross                                 # inside an outpost: nothing to fight, just walk out
        # in an area: cross it with the engine (pathing and fighting), then step through the door
        target = leg.get("portal") or leg["xy"]
        walk = BehaviorTree(AdaptiveNode(self.session, target_map_id=here, transit={"xy": target},
                                         arrive_map=leg.get("expect")))
        return BT.Sequence(name="CrossArea", children=[walk, cross])

    def _next_leg(self):
        """Start the next leg, or the vanquish once we are standing in the area."""
        if game.map_ready() and game.map_id() == self.map_id and game.is_explorable():
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
        self._set("travel" if leg["do"] == "travel" else "walking", self._leg_tree(leg))

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
            self.session.log.event("campaign", map_id=self.map_id, phase="route",
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
                self._outcome = f"door led to {game.map_name(here)}, expected {game.map_name(expect)}"
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
            self._finish()
            return S.RUNNING

        # phase == "return"
        self._conclude()
        return S.SUCCESS

    def _start_unlock(self):
        """The vanquish is complete, so leaving is safe: walk into a locked outpost that can be
        reached from here, which opens it for map travel from now on."""
        c = self.campaign
        if c.world is None:
            return False
        px, py = game.player_xy()
        options = [(o, leg) for o, leg in c.world.outposts_from(self.map_id) if not game.map_unlocked(o)]
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
