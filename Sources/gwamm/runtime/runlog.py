"""Run log: one line of JSON per event, appended as the run goes.

Files land in <Py4GW folder>/gwamm_logs/run_<map id>_<start time>.jsonl and are safe to
read while the bot is running.
"""
import json
import os
import time

import PySystem

HEARTBEAT_S = 5.0

_PRIVATE = None


def scrub(line):
    """Take this computer's own details out of a line before it is written: the Windows user
    folder, user name and computer name (they turn up in error traces and file paths). Logs get
    shared; none of that is needed to read them."""
    global _PRIVATE
    if _PRIVATE is None:
        found = []
        home = os.path.expanduser("~")
        if home and home != "~":
            found.append((home, "~"))
            found.append((home.replace("\\", "\\\\"), "~"))        # as it appears inside JSON
        for var, shown in (("USERNAME", "<user>"), ("COMPUTERNAME", "<pc>")):
            v = os.environ.get(var, "")
            if len(v) >= 3:
                found.append((v, shown))
        _PRIVATE = found
    for secret, shown in _PRIVATE:
        if secret in line:
            line = line.replace(secret, shown)
    return line


class RunLog:
    def __init__(self):
        self.path = ""
        self._file = None
        self._last_beat = 0.0
        self._last_objective = None
        self._last_mode = None
        self._last_escalation = 0
        self._last_note = ""

    def open(self, map_id):
        self.close()
        try:
            folder = os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs")
            os.makedirs(folder, exist_ok=True)
            self.path = os.path.join(folder, f"run_{map_id}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
            self._file = open(self.path, "a", encoding="utf-8")
            self._last_objective, self._last_mode, self._last_escalation = None, None, 0
        except Exception as e:
            self.path, self._file = f"log disabled: {e!r}", None

    def close(self):
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
        self._file = None

    def event(self, kind, **data):
        if self._file is None:
            return
        try:
            data.update({"t": round(time.time(), 2), "clock": time.strftime("%H:%M:%S"), "event": kind})
            self._file.write(scrub(json.dumps(data, default=str)) + "\n")
            self._file.flush()
        except Exception:
            pass

    def observe(self, eng, extra=None):
        """Call often. Writes changes as they happen and a heartbeat every few seconds."""
        if self._file is None or eng is None:
            return
        o = eng.objective
        key = None if o is None else o.key
        if eng.mode != self._last_mode:
            self.event("mode", mode=eng.mode, previous=self._last_mode)
            self._last_mode = eng.mode
        if eng.mem.escalation != self._last_escalation:
            self._last_escalation = eng.mem.escalation
            self.event("search_tightened", level=eng.mem.escalation, radius=round(eng.mem.search_radius), note=eng.note)
        if key != self._last_objective:
            self._last_objective = key
            self.event("objective", objective_kind=None if o is None else o.kind, key=key,
                       pos=None if o is None else [round(o.pos[0]), round(o.pos[1])],
                       walk=None if o is None or o.walk is None or o.walk != o.walk or abs(o.walk) == float("inf") else round(o.walk),
                       player=[round(eng.player_xy[0]), round(eng.player_xy[1])])
        if eng.note != self._last_note:
            self._last_note = eng.note
            self.event("note", text=eng.note, player=[round(eng.player_xy[0]), round(eng.player_xy[1])])
        now = time.time()
        if now - self._last_beat >= HEARTBEAT_S:
            self._last_beat = now
            st = eng.status()
            near = [e for e in eng.mem.enemies.values() if e.alive and e.in_range]
            px, py = eng.player_xy
            closest = min((((e.xy[0] - px) ** 2 + (e.xy[1] - py) ** 2) ** 0.5 for e in near), default=None)
            beat = {"player": [round(px), round(py)], "mode": st["mode"], "objective": st["objective"],
                    "foes": st["foes_remaining"], "searched": st["searched"], "regions": st["regions"],
                    "groups": st["clusters"], "known_alive": st["known_alive"],
                    "in_view": len(near), "closest_enemy": None if closest is None else round(closest),
                    "fog": st["fog_cells"], "walked": st["walked"]}
            if eng.carto is not None and eng.grid is not None:
                cell = eng.carto.proj.cell(px, py)
                # Standing in a cell always credits it, so this should read true within seconds.
                # If it stays false, the cell mapping is wrong.
                beat["cell"] = list(cell)
                beat["cell_explored"] = eng.grid.explored(*cell)
            if extra:
                beat.update(extra)
            self.event("beat", **beat)
