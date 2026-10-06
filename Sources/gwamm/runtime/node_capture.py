"""Elite capture for the area runner: which bosses are wanted, and using the signet."""
import math
import time

from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Sources.ApoSource.ApoBottingLib import wrappers as BT

from ..core import elites, tactics
from ..core.engine import DONE
from . import game


class CaptureMixin:
    def _wanted_from(self, eng, boss):
        """Skill ids worth trying to capture from this dead boss ([] = leave it)."""
        mine = [p for p in game.player_professions() if p]
        area = elites.AREAS.get(self.session.map_id)
        name = game.agent_name(boss.id).lower()
        if area and name:
            hit = next((e for e in area if e.boss.lower() in name or name in e.boss.lower()), None)
            if hit is not None:
                # a boss we have on the list: take it only if it earns one of the signets we carry
                s = self.session
                if elites.should_capture(hit, s.targets, s.captured, game.capture_signets()):
                    return [hit.skill_id]
                return []
        prof = game.agent_primary(boss.id)
        if prof not in mine:
            return []
        return game.unlearnt_elites(prof)       # no data for this boss: offer every elite we lack

    def _capture_tick(self, eng, now):
        """Runs the capture of an elite from a freshly killed boss. True while it needs the turn."""
        c, s = self._cap, self.session
        if c is None:
            slot = game.signet_slot()
            if not slot:
                return False
            for e in eng.mem.enemies.values():
                if e.boss and not e.alive and e.id not in self._cap_done:
                    want = self._wanted_from(eng, e)
                    self._cap_done.add(e.id)
                    if want:
                        self._drop_child()
                        self._cap = {"stage": "wait", "boss": e.id, "xy": e.xy, "want": want, "t": now, "tries": 0,
                                     "name": game.agent_name(e.id)}
                        s.log.event("capture", stage="boss down", boss=self._cap["name"], candidates=len(want))
                        return True
            return False

        px, py = eng.player_xy
        near_enemy = any(e.alive and e.in_range and math.hypot(e.xy[0] - px, e.xy[1] - py) < 1300.0
                         for e in eng.mem.enemies.values())
        stage, age = c["stage"], now - c["t"]

        def go(stage_name):
            c["stage"], c["t"] = stage_name, now

        if stage == "wait":                              # let the fight around the corpse finish
            if not near_enemy or age > 45.0:
                self._cap_child = BT.Move(pos=c["xy"], tolerance=250.0)
                go("approach")
        elif stage == "approach":
            self._cap_child.root.blackboard = self.blackboard
            st = self._cap_child.root.tick()
            if st != BehaviorTree.NodeState.RUNNING or age > 40.0:
                go("cast")
        elif stage == "cast":
            slot = game.signet_slot()
            if not slot:
                return self._capture_end("no signet left")
            game.use_signet_on(slot, c["boss"])
            go("casting")
        elif stage == "casting":
            if age > 4.0:
                go("pick")
        elif stage == "pick":
            picked = game.capture_dialog_pick(c["want"])
            if picked:
                c["picked"] = picked
                go("confirm")
            elif age > 3.0:
                c["tries"] += 1
                if c["tries"] >= 3:
                    return self._capture_end("capture window did not offer a wanted elite")
                go("cast")
        elif stage == "confirm":
            if age > 1.3:
                game.capture_dialog_confirm()
                go("verify")
        elif stage == "verify":
            if game.skill_learnt(c["picked"]):
                return self._capture_end("captured", c["picked"])
            if age > 6.0:
                c["tries"] += 1
                if c["tries"] >= 3:
                    return self._capture_end("capture not confirmed")
                go("cast")
        return True

    def _capture_end(self, result, skill_id=0):
        c, s = self._cap, self.session
        s.log.event("capture", stage="finished", boss=c.get("name"), result=result, skill_id=skill_id)
        s.captures.append({"boss": c.get("name"), "result": result, "skill_id": skill_id})
        if skill_id:
            s.captured.add(skill_id)
        self._cap = None
        return False



def e_prof(skill_id):
    """Profession of a skill id, from our area data (falls back to 0)."""
    for area in elites.AREAS.values():
        for e in area:
            if e.skill_id == skill_id:
                return e.profession
    return 0
