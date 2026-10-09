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
            for agent_id, model, x, y in game.npcs_in_sight():
                if seen.get(agent_id, 0) > 0 or len(seen) > 150:
                    continue
                name = game.agent_name(agent_id)
                seen[agent_id] = seen.get(agent_id, 0) - 1          # negative: still waiting for the name
                if name or seen[agent_id] < -3:
                    seen[agent_id] = 1
                    self.session.log.event("npc", agent=agent_id, model=model, name=name, pos=[round(x), round(y)],
                                           blessing_giver=game.blessing_kind(agent_id, model))
        except Exception as e:
            if not seen.get("error"):
                seen["error"] = 1
                self.session.log.event("npc", error=repr(e))

    def _giver_name(self, agent_id):
        names = self.__dict__.setdefault("_giver_names", {})
        if agent_id not in names:
            n = game.agent_name(agent_id)
            if not n:
                return ""
            names[agent_id] = n
        return names[agent_id]

    def _skip_once(self, agent_id, why, x, y, eng):
        noted = self.__dict__.setdefault("_bless_skip_noted", set())
        if (agent_id, why) not in noted:
            noted.add((agent_id, why))
            px, py = eng.player_xy
            self.session.log.event("blessing", stage="skipped", npc=agent_id, name=self._giver_name(agent_id), why=why,
                                   pos=[round(x), round(y)], dist=round(math.hypot(x - px, y - py)))

    def _blessing_done(self, agent_id, before):
        """After a giver: note who blessed us, and whether a second blessing stacked."""
        try:
            after = game.blessing_effects()
        except Exception:
            after = set()
        name = self._giver_name(agent_id)
        if name:
            self.__dict__.setdefault("_blessed_by", set()).add(name)
            if before:                                   # we already had one: did this add to it?
                self.session.note_blessing_kind(name, "yes" if len(after) > len(before) else "no")
        return after

    def _try_blessing(self, eng, now):
        """Take a blessing or bounty from a giver once it is safe to. A giver with enemies round
        it is remembered and its enemies are fought first (the engine is told to favour them);
        once they are dead the party walks back for it. True when a walk was started."""
        self._log_npcs()
        if self.__dict__.get("_givers_eng") is not eng:      # new instance: agent ids mean something else now
            self._givers_eng, self._givers, self._bless_clearing, self._bless_tries = eng, {}, set(), {}
        givers = self._givers                                 # agent id -> (x, y, kind)
        try:
            for agent_id, x, y, kind in game.blessing_npcs():
                givers[agent_id] = (x, y, kind)
            active = game.blessing_effects()
            if active:
                # Already blessed. Another kind of giver may add a second blessing rather than
                # replace this one (Nightfall's Sunspear and Lightbringer bounties). Whether it
                # does is learnt per giver name and kept (blessings.json); unknown names are
                # tried once, names known to replace or to add nothing are walked past.
                stack = self.session.load_blessing_kinds()
                mine = set(self.__dict__.get("_blessed_by", ()))
                keep = {a: g for a, g in givers.items()
                        if self._giver_name(a) not in mine and stack.get(self._giver_name(a)) != "no"}
                if not keep:
                    eng.focus = None
                    for a, (x, y, _k) in givers.items():
                        self._skip_once(a, "already blessed (%s)" % ",".join(str(i) for i in sorted(active)), x, y, eng)
                    return False
                givers = keep
        except Exception as e:
            if not getattr(self, "_bless_err", False):
                self._bless_err = True
                self.session.log.event("blessing", stage="error", error=repr(e))
            return False
        if not givers:
            return False
        px, py = eng.player_xy
        foes = [e.xy for e in eng.mem.enemies.values() if e.alive and not e.lost]
        clearing = self.__dict__.setdefault("_bless_clearing", set())
        focus = None
        def skip(agent_id, why, x, y):
            # say once per giver and reason why it was passed over, so a skipped blessing can
            # always be traced in the log
            self._skip_once(agent_id, why, x, y, eng)

        for agent_id, (x, y, kind) in sorted(givers.items(), key=lambda g: (g[1][0] - px) ** 2 + (g[1][1] - py) ** 2):
            # Givers often stand right by an entrance, inside the keep-clear ring round it
            # (Hidden City of Ahdashim: the Whispers Informant beside the arrival point was
            # skipped). Walking to someone that close to a door is fine; only one standing
            # practically on the door itself is passed over.
            if self._bless_tries.get(agent_id, 0) >= 2:
                skip(agent_id, "tried twice", x, y)
                continue
            if any(math.hypot(x - ex, y - ey) < 300.0 for ex, ey in eng.exits):
                skip(agent_id, "standing on an exit", x, y)
                continue
            known = agent_id in clearing                 # seen before and waited on: worth a longer walk back
            if math.hypot(x - px, y - py) > (8000.0 if known else eng.cfg.blessing_reach):
                skip(agent_id, "too far", x, y)
                continue
            walk = eng._walk_distance((x, y))
            if walk is None or walk > (8000.0 if known else 1.5 * eng.cfg.blessing_reach):
                skip(agent_id, "too long a walk", x, y)
                continue
            if any(math.hypot(fx - x, fy - y) < 1600.0 for fx, fy in foes):
                if focus is None:
                    focus = (x, y)
                if agent_id not in clearing:
                    clearing.add(agent_id)
                    self.session.log.event("blessing", stage="clearing", npc=agent_id, npc_kind=kind, pos=[round(x), round(y)],
                                           foes_near=sum(1 for fx, fy in foes if math.hypot(fx - x, fy - y) < 1600.0))
                continue
            if any(math.hypot(fx - px, fy - py) < 1600.0 for fx, fy in foes):
                eng.focus = focus
                return False                             # fight what is on us first
            eng.focus = None
            self._bless_tries[agent_id] = self._bless_tries.get(agent_id, 0) + 1
            self._drop_child()
            self._child = BT.TakeBlessing(pos=(x, y), faction=kind if kind in ("kurzick", "luxon") else None,
                                          blessing_dialog_id=game.BLESSING_DIALOG[kind], multi_account=False, log=True)
            try:
                self._bless_before = game.blessing_effects()
            except Exception:
                self._bless_before = set()
            self._key, self._target, self._started = ("bless", agent_id), (x, y), now
            self._progress_at, self._progress_xy, self._progress_kills = now, None, -1
            self.session.log.event("blessing", stage="start", npc=agent_id, npc_kind=kind, pos=[round(x), round(y)],
                                   attempt=self._bless_tries[agent_id], came_back=known)
            return True
        eng.focus = focus
        return False

    # ---- elite capture ----
