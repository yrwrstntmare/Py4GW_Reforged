"""Pulling for the area runner: bring one group back to the party instead of walking the party
into it and into whatever stands or walks beside it.

What starting a fight would bring is worked out first (core/tactics.engagement): the nearest
enemy, the group linked to it, bystanders close enough to come too, and patrols whose beat
passes by. Then one of:

  (nothing) a few enemies on their own: the usual fight
  wait   a patrol is about to walk into the fight: the party stands off, out of reach, and
         looks again each second. A patrol walking at the party: it gives ground.
  probe  several groups a single spot would wake, too many together: wait a little for them
         to separate, then wake the nearest edge
  avoid  one crowd, more than the party should take, that cannot be split: left for later
  pull   1. camp  - the heroes are flagged where the party stands, on ground already cleared
         2. go    - the leader walks at the nearest enemy until the group reacts (starts moving)
         3. back  - the leader walks back to the camp
         4. hold  - the leader STAYS at the camp with the heroes and the party fights what
                    arrives. Nobody goes forward again until the camp is quiet.
         5. done  - flags down; the normal plan carries on. More of the group still standing
                    where it was is pulled again on the next approach.

Not used in a party with other players in it: their accounts follow the leader and cannot be
parked with hero flags.
"""
import math
import time

from ..core import tactics
from ..core.memory import is_patrol
from . import game

GO_SECONDS = 12.0            # longest the leader walks forward looking for a reaction
BACK_SECONDS = 7.0
ARRIVE_SECONDS = 9.0         # how long the party waits at the camp for the group to arrive
HOLD_SECONDS = 60.0          # longest the party stays parked at the camp
PULL_RANGE = 1180.0          # wand, staff and most bows reach about 1250; enemies notice at about 1000 (recordings)
CAMP_REACH = 1300.0          # enemies this close to the camp are in the fight


class PullMixin:
    def _pull_reset(self, why=""):
        st = self.__dict__.get("_pull")
        if st and st.get("phase") in ("go", "back", "hold", "shift"):
            try:
                game.unflag_heroes(())
            except Exception:
                pass
            self.session.log.event("pull", stage="end", why=why, forecast=st.get("total"), came=st.get("came"),
                                   seconds=round(time.time() - st.get("t0", time.time()), 1),
                                   kills=game.foes_killed() - st.get("kills0", game.foes_killed()))
        self._leader_fights(True)
        keep = (st or {}).get("aggro", 950.0)
        self._pull = {"phase": None, "aggro": keep,
                      "cool": time.time() + {"fight over": 3.0, "nothing came": 6.0}.get(why, 20.0)}

    def _leader_fights(self, on):
        """Switch the leader's own skill use on or off. While he walks out to wake a group and
        back to the party he must not stop to cast: the first spell rooted him at the spot he
        was noticed from, alone, with the heroes parked behind him. (Reforged reads this
        request from the blackboard on its next tick; the heroes are not affected.)"""
        if self.__dict__.get("_leader_on", True) != on:
            self._leader_on = on
            self.blackboard["combat_enabled_request"] = bool(on)

    def _flag_party(self, eng, centre, toward):
        """Park the heroes round `centre`, spread out and facing `toward`. Falls back to the one
        party flag when there are no heroes of our own to place one by one."""
        try:
            ok = lambda x, y: eng.nav.on_mesh(x, y) and eng.nav.line_clear(centre, (x, y))
            places = tactics.formation(centre, toward, 7, ok)
            if game.flag_heroes_spread(places) > 0:
                return
        except Exception:
            pass
        game.flag_all_heroes(centre[0], centre[1])

    def _toward_enemy(self, eng, xy):
        """A point in the direction of the nearest enemy from `xy` (straight ahead if none)."""
        near = [e.xy for e in eng.mem.enemies.values() if e.alive and not e.lost and e.in_range]
        if not near:
            return (xy[0] + 1.0, xy[1])
        return min(near, key=lambda p: (p[0] - xy[0]) ** 2 + (p[1] - xy[1]) ** 2)

    def _camp_here(self, st, xy, why):
        """The fight is where the leader is, not where it was planned: bring the heroes to him."""
        try:
            eng = self.session.engine
            self._flag_party(eng, xy, self._toward_enemy(eng, xy))
        except Exception:
            return
        self.session.log.event("pull", stage="regroup", why=why, moved=round(math.hypot(xy[0] - st["camp"][0], xy[1] - st["camp"][1])))
        st["camp"] = (xy[0], xy[1])

    def _pull_view(self, eng):
        """Every enemy worth counting, as plain dicts for the forecast."""
        return [{"id": e.id, "xy": e.xy, "patrol": is_patrol(e), "trail": e.trail, "vel": e.vel, "z": e.z,
                 "blocked": e.id in eng.walled}      # still counted (reach goes through walls), never the target
                for e in eng.mem.enemies.values() if e.alive and not e.lost]

    def _pull_tick(self, eng, now):
        """True while the pull is steering the leader (the caller does nothing else that tick)."""
        cfg, s = self.session.cfg, self.session
        st = self.__dict__.setdefault("_pull", {"phase": None, "cool": 0.0, "aggro": 950.0})
        px, py = eng.player_xy
        if st["phase"] not in ("go", "back", "shift") and (self.__dict__.get("_fight") or {}).get("to") is None:
            self._leader_fights(True)                   # never left switched off outside those walks
        if cfg.pull_groups and not cfg.multibox:
            # who is on another level or behind a wall, kept current while this routine is in
            # charge (the walking planner, which normally refreshes it, is not running then)
            eng.walled = eng.walled_off()
        live = [e for e in eng.mem.enemies.values()
                if e.alive and not e.lost and e.in_range]
        nearest = min((math.hypot(e.xy[0] - px, e.xy[1] - py) for e in live), default=None)
        fighting = bool(self.blackboard.get("COMBAT_ACTIVE", False))

        # "on us": close enough to be hitting, or closing on the party fast. (The combat flag alone
        # is not enough: it comes on with enemies merely in sight, and standing to fight then
        # started the fight we were waiting to avoid.)
        closing = [e for e in live if math.hypot(e.xy[0] - px, e.xy[1] - py) < 1500.0
                   and math.hypot(e.vel[0], e.vel[1]) >= 60.0
                   and (e.vel[0] * (px - e.xy[0]) + e.vel[1] * (py - e.xy[1])) / (math.hypot(px - e.xy[0], py - e.xy[1]) or 1.0) >= 60.0]
        on_us = (nearest is not None and nearest < 850.0) or (fighting and len(closing) >= 2 and nearest is not None and nearest < 1200.0)
        if st["phase"] == "wait" and on_us:
            # They came to us while we stood off. Fight it here, on the ground we chose to wait
            # on: park the heroes on this spot and hold, exactly as after a pull. Walking on at
            # the group we were waiting for would add it to the fight.
            try:
                self._flag_party(eng, (px, py), self._toward_enemy(eng, (px, py)))
            except Exception:
                pass
            st.update(phase="hold", camp=(px, py), t0=now, t2=now, quiet=now, last=0.0, came=0,
                      kills0=game.foes_killed(), total=None)
            s.log.event("pull", stage="stand", why="they came to us while waiting", nearest=None if nearest is None else round(nearest))
        if st["phase"] in (None, "wait"):
            waiting = st["phase"] == "wait"
            if self._alone():
                st["phase"] = None
                return False
            if (not cfg.pull_groups or cfg.multibox or self.transit is not None or self._cap is not None
                    or now < st["cool"] or eng.objective is None or eng.objective.kind != "cluster"
                    or (not waiting and nearest is not None and nearest < 1050.0)):   # (not the combat flag: it is on with enemies merely in sight)
                if waiting:
                    st["phase"] = None
                return False
            view = self._pull_view(eng)
            # a weakened party takes on less: the limit shrinks with death penalty
            limit = tactics.take_limit(cfg.pull_max_take, min(game.my_morale(), (game.my_morale() + game.party_low_morale()) / 2.0))
            # Once stopped to look, keep looking from further out than it took to stop: without
            # that, a group hovering at the edge of the range switched the wait off and on, and
            # every "off" let the normal walk carry the party a little closer.
            ranged = cfg.pull_ranged and game.leader_ranged()
            plan = tactics.plan_fight((px, py), view, eng.mem.route, limit, far=2600.0 if waiting else 1700.0,
                                      stand=PULL_RANGE if ranged else None)
            # A room the party takes comfortably in an ordinary fight is not pulled at all. The
            # recordings: with up to six at once almost nobody dies, and a third of all pulls
            # brought nothing and cost 10-15 s each, nearly all of them in rooms like that.
            small = plan is not None and sum(plan["groups"]) <= max(cfg.pull_min_group, int(limit * 0.67))
            if plan is None or small or (plan["total"] < cfg.pull_min_group and len(plan["groups"]) == 1 and not plan["incoming"]):
                if waiting and plan is None and now - st.get("lost", now) < 4.0:
                    st.setdefault("lost", now)
                    self._drop_child()
                    return True                        # lost sight for a moment: stay put
                if waiting:
                    st["phase"] = None
                return False                           # nothing near, or a few on their own: the usual fight
            st.pop("lost", None)

            if st["phase"] != "wait":
                # First sight of this room: stop, out of reach, and look before starting anything.
                self._drop_child()
                st.update(phase="wait", w0=now, last=0.0, said=None, spot=(px, py), held=0.0)
            waited = now - st["w0"]
            eng.last_plan = {"t": round(now, 1), "verdict": plan["verdict"], "groups": plan["groups"],
                             "would_wake": plan["woken_groups"], "total": plan["total"], "limit": limit,
                             "incoming": plan["incoming"], "late": plan["late"], "camp": plan["camp"],
                             "tag": plan["tag_xy"], "target": plan["target"], "members": plan["members"],
                             "likely": plan.get("likely"), "ranged": ranged}
            note = (plan["verdict"], tuple(plan["groups"]), plan["total"], bool(plan["incoming"]))
            if note != st.get("said"):
                st["said"] = note
                s.log.event("pull", stage="plan", verdict=plan["verdict"], groups=plan["groups"],
                            would_wake=plan["woken_groups"], total=plan["total"], limit=limit,
                            incoming=plan["incoming"], late=plan["late"], patrol_first=plan["patrol_first"],
                            nearest=round(plan["distance"]), camp_found=plan["camp"] is not None,
                            other_levels=len(eng.walled), my_plane=getattr(eng, "player_plane", None),
                            my_height=None if getattr(eng, "player_z", None) is None else round(eng.player_z))

            def give_ground(reach):
                if now - st["last"] > 1.0:
                    st["last"] = now
                    near = [v["xy"] for v in view if math.hypot(v["xy"][0] - px, v["xy"][1] - py) <= 3000.0]
                    back = tactics.retreat_point(eng.mem.route, (px, py), near, lo=450.0, hi=950.0)   # a step back, not a run
                    if back is not None and reach and any(math.hypot(x - back[0], y - back[1]) < reach for x, y in near):
                        back = None
                    if back is not None:
                        game.move_to(back[0], back[1])
                        st["spot"] = (back[0], back[1])

            # If everything in view together is still within what the party takes, there is
            # nothing to get wrong: no backing off from a patrol, no waiting for a better moment,
            # go almost at once. Only a room where the choice matters earns a proper look.
            everything = sum(plan["groups"])
            easy = everything <= limit
            # The same room, looked at for too long: approaching, backing off and approaching
            # again as a scattered patrol wandered. After 45 s on one objective, stop being clever.
            key = eng.objective.key
            if self.__dict__.get("_dither", (None, 0.0))[0] != key:
                self._dither = (key, now)
            force = False
            if now - self._dither[1] > 45.0:
                self._dither = (key, now)
                if not (easy or everything <= limit + 3 or cfg.keep_at_it):
                    eng.postpone(plan["target_xy"], 480.0)
                    s.log.event("pull", stage="postponed", total=plan["total"], waited=45)
                    st.update(phase=None, cool=now + 5.0)
                    return False
                # Stop looking, but do not stop pulling: walking the whole party into a room this
                # full is how it was lost (9 came at once). Take the pull the plan has now.
                s.log.event("pull", stage="enough looking", everything=everything, limit=limit, total=plan["total"])
                force = True
            if plan["incoming"] and not easy and not force:
                give_ground(0.0)                       # a patrol we are not ready for is walking at us
                return True
            look = 1.0 if easy else 4.0
            go = force or ((easy or plan["verdict"] == "pull") and waited >= look)
            if easy or force:
                pass
            elif plan["verdict"] == "avoid":
                if not cfg.keep_at_it:
                    # one crowd, more than the party should take, and it cannot be split
                    eng.postpone(plan["target_xy"], 480.0)
                    s.log.event("pull", stage="avoided", total=plan["total"], limit=limit)
                    st.update(phase=None, cool=now + 5.0)
                    return False
                go = waited > cfg.pull_wait_seconds     # testing: try it anyway, after a look
                if plan["distance"] < 1500.0:
                    give_ground(1500.0)
            elif plan["verdict"] == "probe":
                go = waited > 12.0
            elif plan["verdict"] == "wait" and waited > cfg.pull_wait_seconds:
                if cfg.keep_at_it:
                    go = True
                else:
                    eng.postpone(plan["target_xy"])
                    s.log.event("pull", stage="postponed", total=plan["total"], waited=round(waited))
                    st.update(phase=None, cool=now + 5.0)
                    return False
            if not go:
                # stand off and keep looking: no walk in progress, and back to the spot if nudged off it
                self._drop_child()
                spot = st.get("spot") or (px, py)
                if math.hypot(px - spot[0], py - spot[1]) > 200.0 and now - st.get("held", 0.0) > 1.0:
                    st["held"] = now
                    game.move_to(spot[0], spot[1])
                return True

            # --- pull: park the heroes at the camp, walk to the spot that wakes only what we want ---
            camp = plan["camp"] or (px, py)
            self._drop_child()
            # The heroes stand a little in front of where the leader will come back to, so the
            # group that follows him meets them first. (Recordings: the leader, a caster, was
            # the first of the party to be hurt in 4 fights out of 10 and died the most often.)
            tx, ty = plan["tag_xy"]
            gap = math.hypot(tx - camp[0], ty - camp[1])
            front = camp
            if gap > 500.0:
                cand = (camp[0] + (tx - camp[0]) * 250.0 / gap, camp[1] + (ty - camp[1]) * 250.0 / gap)
                if eng.nav.on_mesh(cand[0], cand[1]) and eng.nav.line_clear(camp, cand):
                    front = cand
            try:
                self._flag_party(eng, front, (tx, ty))
            except Exception as e:
                s.log.event("pull", stage="error", error=repr(e))
                st.update(phase=None, cool=now + 60.0)
                return False
            self._leader_fights(False)
            st["hp0"], st["ranged"], st["shot"] = game.my_health(), ranged, 0.0
            st["released"] = False
            st.update(phase="go", camp=camp, t0=now, last=0.0, kills0=game.foes_killed(), came=0,
                      total=plan["total"], target=plan["target"], tag=plan["tag_xy"], at_tag=0.0,
                      pos0={e.id: e.xy for e in live})
            s.log.event("pull", stage="start", verdict=plan["verdict"], camp=[round(camp[0]), round(camp[1])],
                        tag=[round(plan["tag_xy"][0]), round(plan["tag_xy"][1])], groups=plan["groups"],
                        would_wake=plan["woken_groups"], total=plan["total"], limit=limit,
                        patrol_first=plan["patrol_first"], waited=round(waited, 1), ranged=ranged, likely=plan.get("likely"))
            return True

        if now - st["t0"] > 150.0:
            self._pull_reset("took too long")
            return False
        camp = st["camp"]
        from_camp = math.hypot(px - camp[0], py - camp[1])

        if st["phase"] == "go":
            # "noticed" means the group is actually coming: someone has left its place and is
            # nearer than it was. (Being close is not enough: last time nobody followed.)
            pos0 = st["pos0"]
            moving = [e for e in live if e.id in pos0
                      and math.hypot(e.xy[0] - pos0[e.id][0], e.xy[1] - pos0[e.id][1]) > 200.0
                      and math.hypot(e.xy[0] - px, e.xy[1] - py) < 1700.0]
            running = [e for e in moving if math.hypot(e.vel[0], e.vel[1]) >= 200.0]   # a walk is a patrol on its beat
            hit = game.my_health() < st.get("hp0", 1.0) - 0.03      # casters do not come: they stand and shoot
            tgt = eng.mem.enemies.get(st["target"])
            if st.get("ranged") and tgt is not None and tgt.alive:
                d_t = math.hypot(tgt.xy[0] - px, tgt.xy[1] - py)
                info = game.enemy_details(tgt.id)
                if info is not None and info[0] < 0.995:
                    hit = True                                      # our shot landed: its group knows
                elif d_t <= PULL_RANGE + 500.0 and now - st.get("shot", 0.0) >= 1.5:
                    # Attack it: the game itself walks the leader to exactly his weapon's range
                    # and no nearer, and follows the target if it moves. (Walking to a spot
                    # worked out beforehand did not: the target, a patrol, had walked on, the
                    # leader stood out of range without shooting, then edged in until noticed.)
                    st["shot"] = now
                    try:
                        game.attack(tgt.id)
                    except Exception:
                        pass
                elif d_t > PULL_RANGE + 500.0:
                    st["tag"] = (tgt.xy[0] + (px - tgt.xy[0]) * PULL_RANGE / d_t, tgt.xy[1] + (py - tgt.xy[1]) * PULL_RANGE / d_t)
            if running or hit or (fighting and moving) or (nearest is not None and nearest <= 700.0):
                st.update(phase="back", t1=now, last=0.0)
                s.log.event("pull", stage="noticed", moving=len(moving), nearest=None if nearest is None else round(nearest),
                            from_camp=round(from_camp))
            elif nearest is None or now - st["t0"] > GO_SECONDS or from_camp > 1500.0:
                self._pull_reset("nothing came")
                return False
            elif now - st["last"] >= 0.4:
                st["last"] = now
                # Walk to the chosen spot, not onto the enemy: standing there is what decides who
                # wakes. If nobody has stirred a few seconds after arriving, the notice distance
                # was over-estimated: edge towards the target, a little at a time.
                tag = st["tag"]
                if st.get("shot") and now - st["shot"] < 3.0:
                    return True                                     # attacking: do not walk the attack off
                if math.hypot(px - tag[0], py - tag[1]) < 90.0:
                    st["at_tag"] = st["at_tag"] or now
                    tgt = eng.mem.enemies.get(st["target"])
                    if now - st["at_tag"] > 2.5 and tgt is not None and tgt.alive:
                        d = math.hypot(tgt.xy[0] - px, tgt.xy[1] - py) or 1.0
                        st["tag"] = (px + (tgt.xy[0] - px) * 120.0 / d, py + (tgt.xy[1] - py) * 120.0 / d)
                        st["at_tag"] = 0.0
                game.move_to(st["tag"][0], st["tag"][1])
            return True

        if st["phase"] == "back":
            if from_camp < 220.0 or now - st["t1"] > BACK_SECONDS:
                self._leader_fights(True)
                if from_camp >= 220.0:
                    self._camp_here(st, (px, py), "leader could not get back")
                    camp = st["camp"]
                st.update(phase="hold", t2=now, quiet=now, last=0.0)
                s.log.event("pull", stage="hold", at_camp=from_camp < 220.0,
                            following=sum(1 for e in live if math.hypot(e.xy[0] - px, e.xy[1] - py) < 1700.0))
            elif now - st["last"] >= 0.4:
                st["last"] = now
                game.move_to(camp[0], camp[1])
            return True

        if st["phase"] == "shift":
            # dragging the fight to the new camp: walk there, the enemy follows, the heroes are already flagged there
            if from_camp < 220.0 or now - st["t3"] > 7.0:
                self._leader_fights(True)
                if from_camp >= 220.0:
                    self._camp_here(st, (px, py), "leader could not reach the new spot")
                st.update(phase="hold", quiet=now, last=0.0)
            elif now - st["last"] >= 0.4:
                st["last"] = now
                game.move_to(camp[0], camp[1])
            return True

        # ---- hold: the leader stays at the camp with the heroes ----
        # Who is in this fight: at the camp, or running at it. An enemy merely standing at its
        # post inside the camp's reach is not: counting it kept the party parked for 110 s
        # "fighting" something 1150 away that the fight step was (rightly) not allowed to reach.
        def engaged(e):
            d = math.hypot(e.xy[0] - camp[0], e.xy[1] - camp[1])
            if d <= 750.0:
                return True
            speed = math.hypot(e.vel[0], e.vel[1])
            return d < CAMP_REACH and speed >= 60.0 and (
                e.vel[0] * (camp[0] - e.xy[0]) + e.vel[1] * (camp[1] - e.xy[1])) / (d or 1.0) >= 60.0
        here = [e for e in live if engaged(e)]
        fighting = fighting and nearest is not None and nearest < 900.0     # the flag alone is on with enemies merely in sight
        st["came"] = max(st.get("came", 0), len(here))
        # Another group walking into this fight: take the fight away from it. The heroes are
        # re-flagged further back along the ground already walked, off that group's path, and
        # the leader goes with them; what we are fighting follows, what was only passing does not.
        # ...but only while little has arrived. Once the party is properly in a fight, moving it
        # means the heroes turn their backs and trail after the leader under fire: a fight that
        # was level at 12 seconds was lost in the next 15 doing exactly that.
        if (cfg.pull_shift and (here or fighting) and len(here) <= 3
                and now - st.get("shift_at", 0.0) > 8.0 and st.get("shifts", 0) < 3):
            view = self._pull_view(eng)
            mine = {e.id for e in live if math.hypot(e.xy[0] - camp[0], e.xy[1] - camp[1]) <= 800.0}
            coming = tactics.incoming_to(camp, view, mine)
            if coming:
                st["shift_at"] = now
                spot = tactics.shift_camp(eng.mem.route, camp, view, mine)
                s.log.event("pull", stage="incoming", groups=[len(g) for g in coming], fighting=len(mine),
                            nearest=round(min(math.hypot(e["xy"][0] - camp[0], e["xy"][1] - camp[1]) for g in coming for e in g)),
                            moving_to=None if spot is None else [round(spot[0]), round(spot[1])], shifts=st.get("shifts", 0))
                if spot is not None:
                    try:
                        self._flag_party(eng, spot, camp)
                    except Exception:
                        pass
                    self._drop_child()
                    self._escape = None
                    self._leader_fights(False)
                    st["released"] = False
                    st.update(phase="shift", camp=spot, t3=now, last=0.0, shifts=st.get("shifts", 0) + 1)
                    return True
        if from_camp > 350.0 and nearest is not None and nearest < 500.0 and now - st.get("regroup_at", 0.0) > 4.0:
            st["regroup_at"] = now                      # pinned in a fight away from the heroes
            self._camp_here(st, (px, py), "leader is fighting away from the party")
            camp = st["camp"]
            self._escape = None
        if False and here and not st.get("released") and now - st["t2"] > 2.0:   # kept spread on their flags instead (see formation)
            # The group has arrived: let the heroes off the flag. Held on it they cannot close
            # with enemy casters who stop at their own range and shoot, and the party stood
            # there being worn down. The leader stays at the camp, so they stay round him.
            st["released"] = True
            try:
                game.unflag_heroes(())
            except Exception:
                pass
            s.log.event("pull", stage="released", here=len(here))
        if here or fighting:
            st["quiet"] = now
            if self._child is not None and self._key and self._key[0] == "escape":
                self._escape_until = now + 6.0       # still fighting: the step at the camp stays the plan
            else:
                # fight what has arrived, standing here: a clearing step aimed at the camp itself.
                # Its reach stops short of anything still standing at its post, so finishing
                # this fight does not start the next one.
                def in_it(e):
                    d = math.hypot(e.xy[0] - camp[0], e.xy[1] - camp[1])
                    if d <= 700.0:
                        return True
                    speed = math.hypot(e.vel[0], e.vel[1])
                    return speed >= 60.0 and (e.vel[0] * (camp[0] - e.xy[0]) + e.vel[1] * (camp[1] - e.xy[1])) / (d or 1.0) >= 60.0
                # anything not in the fight (standing at its post, or a patrol walking PAST) sets the limit
                posts = [math.hypot(e.xy[0] - camp[0], e.xy[1] - camp[1]) for e in live if not in_it(e)]
                reach = max(500.0, min(CAMP_REACH, min(posts) - 250.0)) if posts else CAMP_REACH
                self._drop_child()
                self._escape, self._escape_secs, self._escape_radius = camp, 6.0, reach
            return False
        if st["came"] == 0 and now - st["t2"] > ARRIVE_SECONDS:
            st["aggro"] = max(650.0, st.get("aggro", 950.0) - 100.0)      # go a little closer next time
            self._pull_reset("nothing came")
            return False
        if (st["came"] and now - st["quiet"] > 3.0) or now - st["t2"] > HOLD_SECONDS:
            self._pull_reset("fight over")
            return False
        # nobody here yet (or a lull): do not wander off, and do not walk forward again
        self._drop_child()
        if from_camp > 250.0 and now - st["last"] >= 0.5:
            st["last"] = now
            game.move_to(camp[0], camp[1])
        return True
