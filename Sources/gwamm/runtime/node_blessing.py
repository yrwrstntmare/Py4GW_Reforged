"""Shrine blessings and bounties for the area runner."""
import math
import time

from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Sources.ApoSource.ApoBottingLib import wrappers as BT

from ..core import elites, tactics
from ..core.engine import DONE
from . import game


class BlessingMixin:
    def _has_blessing(self):
        try:
            return game.has_blessing()
        except Exception:
            return None

    def _log_npcs(self):
        """Note each friendly NPC the first time its name is known, so an unrecognised blessing
        giver can be found in the log and added."""
        seen = self.__dict__.setdefault("_npc_seen", {})
        try:
            known = {m for ids in game.BLESSING_NPCS.values() for m in ids}
            for agent_id, model, x, y in game.npcs_in_sight():
                if seen.get(agent_id, 0) > 0 or len(seen) > 150:
                    continue
                name = game.agent_name(agent_id)
                seen[agent_id] = seen.get(agent_id, 0) - 1          # negative: still waiting for the name
                if name or seen[agent_id] < -3:
                    seen[agent_id] = 1
                    self.session.log.event("npc", agent=agent_id, model=model, name=name, pos=[round(x), round(y)],
                                           blessing_giver=model in known)
        except Exception as e:
            if not seen.get("error"):
                seen["error"] = 1
                self.session.log.event("npc", error=repr(e))

    def _try_blessing(self, eng, now):
        """Start walking to a blessing giver if one is close, we have no blessing, and no known
        enemy is near us or it. True when a walk was started."""
        self._log_npcs()
        try:
            npcs = game.blessing_npcs()
            if not npcs or game.has_blessing():
                return False
        except Exception as e:
            if not getattr(self, "_bless_err", False):
                self._bless_err = True
                self.session.log.event("blessing", stage="error", error=repr(e))
            return False
        px, py = eng.player_xy
        foes = [e.xy for e in eng.mem.enemies.values() if e.alive and not e.lost]
        for agent_id, x, y, kind in sorted(npcs, key=lambda n: (n[1] - px) ** 2 + (n[2] - py) ** 2):
            if self._bless_tries.get(agent_id, 0) >= 2 or eng.nav.in_no_go(x, y):
                continue
            if math.hypot(x - px, y - py) > eng.cfg.blessing_reach or eng._walk_distance((x, y)) > 1.5 * eng.cfg.blessing_reach:
                continue
            if any(math.hypot(fx - x, fy - y) < 1600.0 or math.hypot(fx - px, fy - py) < 1600.0 for fx, fy in foes):
                continue
            self._bless_tries[agent_id] = self._bless_tries.get(agent_id, 0) + 1
            self._drop_child()
            self._child = BT.TakeBlessing(pos=(x, y), faction=kind if kind in ("kurzick", "luxon") else None,
                                          blessing_dialog_id=game.BLESSING_DIALOG[kind], multi_account=False, log=True)
            self._key, self._target, self._started = ("bless", agent_id), (x, y), now
            self._progress_at, self._progress_xy, self._progress_kills = now, None, -1
            self.session.log.event("blessing", stage="start", npc=agent_id, npc_kind=kind, pos=[round(x), round(y)],
                                   attempt=self._bless_tries[agent_id])
            return True
        return False

    # ---- elite capture ----
