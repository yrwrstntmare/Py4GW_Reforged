"""Writes the fight records (core/fightrec.py) to gwamm_logs/fights/fights_<map>_<day>.jsonl.

Read-only towards the game: it looks, it never acts. Once a second while enemies are near.
"""
import json
import os
import time

import PySystem

from ..core.fightrec import FightRecorder
from . import game

SAMPLE_S = 1.0


class FightLog:
    def __init__(self):
        self.rec = FightRecorder()
        self.path = ""
        self._last = 0.0
        self._map = None
        self._names = {}
        self._at, self._settle = None, 0.0
        self.written = 0
        self.error = ""

    def _write(self, lines):
        if not lines:
            return
        try:
            folder = os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "fights")
            os.makedirs(folder, exist_ok=True)
            self.path = os.path.join(folder, f"fights_{self._map}_{time.strftime('%Y%m%d')}.jsonl")
            with open(self.path, "a", encoding="utf-8") as f:
                for line in lines:
                    f.write(json.dumps(line, default=str, separators=(",", ":")) + "\n")
            self.written += sum(1 for l in lines if l.get("rec") == "end")
        except Exception as e:
            self.error = repr(e)

    def tick(self, now, map_id, ctx=None):
        if now - self._last < SAMPLE_S:
            return
        self._last = now
        if map_id != self._map:
            self._write(self.rec.close(now, "left the area"))
            self.rec.reset()
            self._map, self._names = map_id, {}
        try:
            if not game.map_ready() or game.party_defeated():
                return                          # loading, or everyone down and about to be moved: do not touch agents
            here = game.player_xy()
            if self._at is not None and (here[0] - self._at[0]) ** 2 + (here[1] - self._at[1]) ** 2 > 3000.0 ** 2:
                self._settle = now + 4.0        # just moved to a shrine: the agent list is being rebuilt
                self._write(self.rec.close(now, "sent back to a shrine"))
            self._at = here
            if now < self._settle:
                return
            snap = game.fight_snapshot(known=self._names)
        except Exception as e:
            self.error = repr(e)
            return
        if snap is None:
            return
        for a in snap["allies"] + snap["enemies"]:      # names are asked for once, then remembered
            if a.get("name"):
                self._names[a["id"]] = a["name"]
            else:
                a["name"] = self._names.get(a["id"], "")
        snap["t"] = now
        if not self.rec.active:                 # the plan matters at the start; cheap to attach then
            snap["ctx"] = ctx() if callable(ctx) else (ctx or {})
        try:
            self._write(self.rec.feed(snap))
        except Exception as e:
            self.error = repr(e)
            self.rec.reset()

    def close(self):
        self._write(self.rec.close(time.time()))
