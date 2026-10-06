"""Fighting for the area runner: calling targets, falling back from a pull that is too big,
walking to dead party members, and the (withdrawn) heroes-first positioning."""
import math
import time

from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Sources.ApoSource.ApoBottingLib import wrappers as BT

from ..core import elites, tactics
from ..core.engine import DONE
from . import game


class FightMixin:
    def _walk_to_the_dead(self, eng, now):
        """Resurrection only reaches so far. After backing off, the living are out of range of
        the fallen, so while resting, walk back to within reach of the nearest body - but only
        one that has no enemy standing over it."""
        st = self.__dict__.setdefault("_raise", {"moved": 0.0, "logged": 0.0})
        if now - st["moved"] < 1.0:
            return
        st["moved"] = now
        try:
            bodies = game.dead_ally_positions()
        except Exception:
            return
        px, py = eng.player_xy
        foes = [e.xy for e in eng.mem.enemies.values() if e.alive and not e.lost]
        safe = [b for b in bodies if not any(math.hypot(fx - b[0], fy - b[1]) < 1300.0 for fx, fy in foes)]
        if not safe:
            return
        bx, by = min(safe, key=lambda b: (b[0] - px) ** 2 + (b[1] - py) ** 2)
        d = math.hypot(bx - px, by - py)
        if d <= 800.0:
            return                                   # already within reach: stand and let them work
        tx, ty = px + (bx - px) * (d - 600.0) / d, py + (by - py) * (d - 600.0) / d
        if eng.nav.in_no_go(tx, ty) or eng.in_hazard(tx, ty):
            return
        game.move_to(tx, ty)
        if now - st["logged"] > 15.0:
            st["logged"] = now
            self.session.log.event("raise_dead", bodies=len(bodies), safe=len(safe), distance=round(d),
                                   player=[round(px), round(py)])

    # ---- heroes first ----
    def _unflag(self, why=""):
        """Take the hero flag down if we put one up. Called from every way out, so the heroes
        are never left standing at a flag."""
        st = self.__dict__.get("_lead")
        if st and st.get("flagged"):
            st["flagged"] = False
            st["released_at"] = time.time()
            self.session.lead_note = f"front line released ({why})   [{st.get('count', 0)} times this area]"
            try:
                game.unflag_heroes(st.get("agents", ()))
            except Exception:
                pass
            self.session.log.event("heroes_first", stage="unflag", why=why,
                                   seconds=round(time.time() - st.get("at", time.time()), 1))

    def _lead_tick(self, eng, now):
        """Before a fight starts, plant the heroes between us and the group and hold back for a
        few seconds, so the enemy meets them first. The party leader walking in ahead was taking
        every opening volley alone. True while holding back."""
        cfg, s = eng.cfg, self.session
        st = self.__dict__.setdefault("_lead", {"flagged": False, "at": 0.0, "done": {}, "flag": None})
        px, py = eng.player_xy
        live = [e for e in eng.mem.enemies.values() if e.alive and e.in_range and not e.lost]
        if st["flagged"]:
            fx, fy = st["flag"]
            # The fight has come to the front line when an enemy is on top of the flag, or has run
            # at us. Judged by where the ENEMY is - not by the gap to us, which also shrinks when
            # we are the ones still drifting forward (that released the heroes after 1.5 s).
            at_flag = any(math.hypot(e.xy[0] - fx, e.xy[1] - fy) < 600.0 for e in live)
            at_us = any(math.hypot(e.xy[0] - px, e.xy[1] - py) < 800.0 for e in live)
            joined = now - st["at"] > 2.0 and (at_flag or at_us)
            if now - st.get("held", 0.0) >= 1.0:      # stand still while the front line does its work
                st["held"] = now
                hx, hy = st.get("hold", (px, py))
                if math.hypot(px - hx, py - hy) > 120.0:
                    game.move_to(hx, hy)
            if joined or now - st["at"] > 7.0 or not live:
                self._unflag("fight joined" if joined else "nothing came" if live else "no enemies")
                return False
            return True
        obj = eng.objective
        if not cfg.heroes_first or cfg.multibox or obj is None or obj.kind != "cluster" or self._cap is not None:
            return False
        key = tuple(obj.key)
        if now - st["done"].get(key, 0.0) < 30.0:
            return False
        if not live:
            return False
        near = min(live, key=lambda e: (e.xy[0] - px) ** 2 + (e.xy[1] - py) ** 2)
        d = math.hypot(near.xy[0] - px, near.xy[1] - py)
        if not (1000.0 <= d <= 1400.0):          # only from stand-off range, before anything has started
            return False
        # The front line stands just inside the distance at which the group notices (about 1000),
        # and never more than 500 ahead of us: close enough to be healed, far enough to be met first.
        reach = min(d - 850.0, 500.0)              # leaves them about 850 from the group: inside its notice
        if reach < 120.0:
            return False
        fx, fy = px + (near.xy[0] - px) * reach / d, py + (near.xy[1] - py) * reach / d
        if not eng.nav.on_mesh(fx, fy) or eng.nav.in_no_go(fx, fy) or eng.in_hazard(fx, fy):
            st["done"][key] = now
            return False
        try:
            front = game.front_line_heroes()
            if not front:                          # a team of cloth casters: nobody is sent ahead
                st["done"][key] = now
                if not st.get("told"):
                    st["told"] = True
                    s.log.event("heroes_first", stage="skipped", note="no hero suited to going in first")
                return False
            # side by side, 250 apart, across the line of approach
            nx, ny = -(near.xy[1] - py) / d, (near.xy[0] - px) / d
            agents = []
            for i, (agent, _prof) in enumerate(front):
                off = (i - (len(front) - 1) / 2.0) * 250.0
                hx, hy = fx + nx * off, fy + ny * off
                if not eng.nav.on_mesh(hx, hy):
                    hx, hy = fx, fy
                game.flag_hero(agent, hx, hy)
                agents.append(agent)
        except Exception as e:
            st["done"][key] = now
            s.log.event("heroes_first", stage="error", error=repr(e))
            return False
        self._drop_child()
        game.move_to(px, py)                         # and stop: dropping the walk does not halt the character
        st.update(flagged=True, at=now, flag=(fx, fy), agents=agents, d0=d, hold=(px, py), held=now)
        st["count"] = st.get("count", 0) + 1
        s.lead_note = f"front line ({len(agents)} heroes) sent {round(reach)} ahead - holding back   [{st['count']} times this area]"
        st["done"][key] = now
        s.log.event("heroes_first", stage="flag", enemies=len(live), distance=round(d), ahead=round(reach), flag=[round(fx), round(fy)],
                    heroes=[{"agent": a, "profession": p} for a, p in front])
        return True

    # ---- fighting ----
    def _fight_tick(self, eng, now):
        """Called every tick. Returns True while a fall-back walk has the turn."""
        cfg, s = eng.cfg, self.session
        st = self.__dict__.setdefault("_fight", {"check": 0.0, "called": 0, "called_at": 0.0,
                                                 "fell_back_at": 0.0, "to": None, "until": 0.0, "moved": 0.0})
        px, py = eng.player_xy
        if st["to"] is not None:                              # walking back to the fall-back spot
            tx, ty = st["to"]
            if now >= st["until"] or math.hypot(tx - px, ty - py) < 200.0:
                s.log.event("fall_back", stage="done", player=[round(px), round(py)],
                            reached=math.hypot(tx - px, ty - py) < 200.0)
                st["to"] = None
                return False
            if now - st["moved"] >= 0.7:
                st["moved"] = now
                game.move_to(tx, ty)
            return True
        if now - st["check"] < 1.0:
            return False
        st["check"] = now
        live = [e for e in eng.mem.enemies.values() if e.alive and e.in_range and not e.lost]
        near = [e for e in live if math.hypot(e.xy[0] - px, e.xy[1] - py) <= 1600.0]
        if not near:
            return False
        closest = min(math.hypot(e.xy[0] - px, e.xy[1] - py) for e in near)
        busy = (self._key is not None and self._key[0] == "bless") or self._cap is not None

        def back_off(reason, distance, seconds):
            spot = tactics.fallback_point(eng.mem.route, (px, py), [e.xy for e in live], distance, clear=900.0)
            if spot is None or eng.nav.in_no_go(*spot) or eng.in_hazard(*spot):
                return False
            self._unflag(reason)
            self._drop_child()
            st.update(to=spot, until=now + seconds, moved=0.0)
            s.log.event("fall_back", stage="start", reason=reason, enemies=len(near), player=[round(px), round(py)],
                        to=[round(spot[0]), round(spot[1])])
            return True

        # brought back to life in the middle of it: get out from under them before doing anything
        # else, instead of standing up at a sliver of health where we fell
        if cfg.fall_back and not busy and now - self.__dict__.get("_revived_at", 0.0) < 2.0 and closest < 1300.0:
            self._revived_at = 0.0
            if back_off("revived among enemies", 1200.0, 6.0):
                return True
        # the fight is being lost: several of the party are down and the enemy is still here.
        # The living pull right back so it does not become a full wipe; the rest gate then
        # waits for the dead to be raised before going in again.
        cond = self.__dict__.get("_cond") or {}
        size = self.__dict__.get("_party_size") or 8
        if (cfg.retreat_when_losing and not busy and cond.get("dead_allies", 0) >= max(2, int(round(size * 0.5)))
                and len(near) >= 3 and now - st.get("retreated_at", 0.0) > 40.0):
            st["retreated_at"] = now
            if back_off(f"{cond.get('dead_allies')} of the party down", 2600.0, 12.0):
                return True
        # too many at once: back up along the ground already cleared, once per cooldown
        # only while the pull is still on its way: once they are on top of us, turning our back costs more
        if (cfg.fall_back and len(near) >= cfg.crowd_size and closest > 700.0
                and now - st["fell_back_at"] > cfg.fall_back_cooldown and not busy):
            st["fell_back_at"] = now                          # counted even if no spot, so we do not retry every second
            if back_off("big pull on its way", cfg.fall_back_distance, 5.0):
                return True
        if cfg.call_targets:
            area = elites.AREAS.get(s.map_id) or []
            infos = []
            for e in near:
                d = game.enemy_details(e.id)
                if d is None:
                    continue
                prof = 0
                if e.boss and area:
                    name = game.agent_name(e.id).lower()
                    prof = next((b.profession for b in area if name and (b.boss.lower() in name or name in b.boss.lower())), 0)
                infos.append({"id": e.id, "xy": e.xy, "hp": d[0], "level": d[1], "caster": d[2], "boss": e.boss, "prof": prof})
            target = tactics.pick_target(infos, (px, py), st["called"])
            if target and (target != st["called"] or now - st["called_at"] > 8.0):
                try:
                    if game.call_target(target):
                        if target != st["called"]:
                            t = next(i for i in infos if i["id"] == target)
                            s.log.event("call_target", agent=target, boss=t["boss"], caster=t["caster"], level=t["level"],
                                        hp=round(t["hp"], 2), enemies=len(near))
                        st["called"], st["called_at"] = target, now
                except Exception as e:
                    if not st.get("err"):
                        st["err"] = True
                        s.log.event("call_target", error=repr(e))
        return False

    # ---- blessings ----
