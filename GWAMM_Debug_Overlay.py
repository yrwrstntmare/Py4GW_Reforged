"""
GWAMM Debug Overlay  (Phase 1-2: observe only, no movement, no combat)

Shows, for the current map:
  - lifecycle state, map id/name, vanquish counters, player position
  - player skillbar, professions, Signet of Capture count
  - the pathing geometry (trapezoids per plane) read live from the game
  - the three spawn-point lists from the map data
  - enemies: alive now, remembered (seen earlier, out of range), dead
  - how far each enemy's first-seen position was from the nearest spawn point

"Dump snapshot" writes everything to a JSON file so it can be reviewed offline.

Written against Py4GW Reforged commit 0b05841 (2026-10-01).
Every call below was checked against that source, but none of it has been run
in a live client yet. Anything marked EXPERIMENTAL is off until you click it.
"""

import json
import math
import os
import time

import PyImGui
import PySystem

from Py4GWCoreLib.Agent import Agent
from Py4GWCoreLib.AgentArray import AgentArray
from Py4GWCoreLib.Context import GWContext
from Py4GWCoreLib.Map import Map
from Py4GWCoreLib.Party import Party
from Py4GWCoreLib.Player import Player
from Py4GWCoreLib.Routines import Routines
from Py4GWCoreLib.Skillbar import SkillBar

MODULE_NAME = "GWAMM Debug Overlay"

SIGNET_OF_CAPTURE = 3
POLL_INTERVAL_S = 0.25          # how often enemies are re-read
SPAWN_MATCH_RADIUS = 1500.0     # "an enemy first appeared near a spawn point"


def _col(r, g, b, a=255):
    # ImGui packs colours as ABGR (same as Utils.RGBToColor in Reforged).
    return (a << 24) | (b << 16) | (g << 8) | r


COL_PLANE0 = _col(95, 95, 105)
COL_PLANE_N = _col(120, 80, 140)
COL_PLAYER = _col(60, 255, 90)
COL_ENEMY = _col(255, 70, 70)
COL_BOSS = _col(255, 200, 40)
COL_STALE = _col(255, 150, 60, 170)
COL_DEAD = _col(110, 110, 110, 200)
COL_SPAWN = (_col(80, 170, 255), _col(80, 255, 230), _col(200, 120, 255))


def _log(msg, level=PySystem.Console.MessageType.Info):
    PySystem.Console.Log(MODULE_NAME, msg, level)


class TrackedEnemy:
    __slots__ = ("agent_id", "first_xy", "xy", "first_seen", "last_seen",
                 "alive", "boss", "in_range")

    def __init__(self, agent_id, xy, now):
        self.agent_id = agent_id
        self.first_xy = xy
        self.xy = xy
        self.first_seen = now
        self.last_seen = now
        self.alive = True
        self.boss = False
        self.in_range = True


class Overlay:
    def __init__(self):
        self.reset()
        # view
        self.zoom = 1.0
        self.pan = [0.0, 0.0]
        self.prev_mouse = None
        self.show_geometry = True
        self.show_spawns = True
        self.show_enemies = True
        self.window_sized = False
        # results of the opt-in reads
        self.vanquished_text = ""
        self.carto_text = ""
        self.last_dump = ""

    # ---- lifecycle -------------------------------------------------------

    def reset(self):
        """Drop everything tied to one map instance."""
        self.map_id = 0
        self.planes = []            # list of list of (xtl, xtr, yt, xbl, xbr, yb)
        self.plane_portals = []
        self.bounds = None          # (min_x, min_y, max_x, max_y)
        self.spawns = ([], [], [])
        self.enemies = {}           # agent_id -> TrackedEnemy
        self.last_poll = 0.0
        self.geometry_error = ""

    def state(self):
        if Map.IsMapLoading():
            return "MAP_LOADING"
        if not Routines.Checks.Map.MapValid():
            return "WAITING_FOR_MAP"
        return "MAP_READY"

    def load_geometry(self):
        """Copy the trapezoids into plain tuples so nothing points at game memory."""
        try:
            layers = Map.Pathing.GetPathingMaps()
            planes, portals = [], []
            min_x = min_y = float("inf")
            max_x = max_y = float("-inf")
            for layer in layers:
                traps = []
                for t in layer.trapezoids:
                    traps.append((t.XTL, t.XTR, t.YT, t.XBL, t.XBR, t.YB))
                    min_x = min(min_x, t.XTL, t.XBL)
                    max_x = max(max_x, t.XTR, t.XBR)
                    min_y = min(min_y, t.YB, t.YT)
                    max_y = max(max_y, t.YB, t.YT)
                planes.append(traps)
                portals.append(len(layer.portals))
            self.planes = planes
            self.plane_portals = portals
            if planes and min_x < max_x:
                self.bounds = (min_x, min_y, max_x, max_y)
            s1, s2, s3 = Map.Pathing.GetSpawns()
            self.spawns = tuple([(s.x, s.y, s.tag) for s in lst] for lst in (s1, s2, s3))
            _log(f"Map {self.map_id}: {len(planes)} planes, "
                 f"{sum(len(p) for p in planes)} trapezoids, "
                 f"spawns {[len(s) for s in self.spawns]}")
        except Exception as e:
            self.geometry_error = repr(e)
            _log(f"Geometry load failed: {e!r}", PySystem.Console.MessageType.Error)

    def poll_enemies(self, now):
        seen = set()
        for agent_id in AgentArray.GetEnemyArray():
            try:
                xy = Agent.GetXY(agent_id)
                alive = Agent.IsAlive(agent_id)
                boss = Agent.HasBossGlow(agent_id)
            except Exception:
                continue
            seen.add(agent_id)
            e = self.enemies.get(agent_id)
            if e is None:
                if not alive:
                    continue        # never saw it alive, don't track a corpse
                e = self.enemies[agent_id] = TrackedEnemy(agent_id, xy, now)
            e.xy = xy
            e.last_seen = now
            e.alive = alive
            e.boss = boss
        for agent_id, e in self.enemies.items():
            e.in_range = agent_id in seen

    def update(self):
        st = self.state()
        if st != "MAP_READY":
            if self.map_id:
                self.reset()
            return st
        map_id = Map.GetMapID()
        if map_id != self.map_id:
            self.reset()
            self.map_id = map_id
            self.load_geometry()
        now = time.time()
        if now - self.last_poll >= POLL_INTERVAL_S:
            self.last_poll = now
            self.poll_enemies(now)
        return st

    # ---- derived numbers -------------------------------------------------

    def enemy_counts(self):
        alive = stale = dead = 0
        for e in self.enemies.values():
            if not e.alive:
                dead += 1
            elif e.in_range:
                alive += 1
            else:
                stale += 1
        return alive, stale, dead

    def spawn_match(self):
        """Per spawn list: share of enemies first seen within SPAWN_MATCH_RADIUS of a point."""
        out = []
        firsts = [e.first_xy for e in self.enemies.values()]
        for lst in self.spawns:
            if not lst or not firsts:
                out.append(None)
                continue
            dists = []
            for fx, fy in firsts:
                dists.append(min(math.hypot(fx - sx, fy - sy) for sx, sy, _ in lst))
            dists.sort()
            near = sum(1 for d in dists if d <= SPAWN_MATCH_RADIUS)
            out.append((near, len(dists), dists[len(dists) // 2]))
        return out

    # ---- opt-in reads ----------------------------------------------------

    def read_vanquished(self):
        """EXPERIMENTAL. Reforged's own accessor for this is switched off
        (WorldContext.vanquished_areas returns None), so this reads the raw
        array the same way Toolbox does: one bit per map id."""
        try:
            from ctypes import c_uint32
            from Py4GWCoreLib.native_src.internals.gw_array import GW_Array_Value_View
            ctx = GWContext.World.GetContext()
            if not ctx:
                self.vanquished_text = "no world context"
                return
            words = GW_Array_Value_View(ctx.vanquished_areas_array, c_uint32).to_list() or []
            ids = [w * 32 + b for w, v in enumerate(words) for b in range(32) if (int(v) >> b) & 1]
            names = [f"{i} {Map.GetMapName(i)}" for i in ids]
            self.vanquished_text = f"{len(ids)} areas: " + "; ".join(names)
        except Exception as e:
            self.vanquished_text = f"failed: {e!r}"

    def read_cartography(self):
        """EXPERIMENTAL. Reports only the size of the explored-map grid."""
        try:
            ctx = GWContext.World.GetContext()
            if not ctx:
                self.carto_text = "no world context"
                return
            dims = list(ctx.h05B4)
            areas = ctx.cartographed_areas
            self.carto_text = f"grid {dims[0]} x {dims[1]}, array entries {len(areas) if areas else 0}"
        except Exception as e:
            self.carto_text = f"failed: {e!r}"

    def dump(self):
        try:
            bar = SkillBar.GetSkillbar()
            px, py = Player.GetXY()
            data = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "map_id": self.map_id,
                "map_name": Map.GetMapName(),
                "vanquishable": Map.IsVanquishable(),
                "foes_killed": Map.GetFoesKilled(),
                "foes_to_kill": Map.GetFoesToKill(),
                "hard_mode": Party.IsHardMode(),
                "party_size": Party.GetPartySize(),
                "max_party_size": Map.GetMaxPartySize(),
                "player_xy": [px, py],
                "player_professions": list(Agent.GetProfessionShortNames(Player.GetAgentID())),
                "skillbar": bar,
                "capture_signets": bar.count(SIGNET_OF_CAPTURE),
                "bounds": self.bounds,
                "planes": [{"trapezoids": len(p), "portals": n}
                           for p, n in zip(self.planes, self.plane_portals)],
                "trapezoids": self.planes,
                "spawns": self.spawns,
                "spawn_match": self.spawn_match(),
                "enemies": [{"id": e.agent_id, "first_xy": e.first_xy, "xy": e.xy,
                             "alive": e.alive, "boss": e.boss, "in_range": e.in_range,
                             "tracked_s": round(e.last_seen - e.first_seen, 1)}
                            for e in self.enemies.values()],
                "vanquished": self.vanquished_text,
                "cartography": self.carto_text,
                "geometry_error": self.geometry_error,
            }
            path = os.path.join(PySystem.Console.get_projects_path(),
                                f"gwamm_snapshot_{self.map_id}_{int(time.time())}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            self.last_dump = path
            _log(f"Snapshot written: {path}", PySystem.Console.MessageType.Success)
        except Exception as e:
            self.last_dump = f"failed: {e!r}"
            _log(f"Snapshot failed: {e!r}", PySystem.Console.MessageType.Error)

    # ---- drawing ---------------------------------------------------------

    def draw_info(self, st):
        PyImGui.text(f"STATE: {st}")
        if st != "MAP_READY":
            PyImGui.text("Waiting for a stable map. Nothing is being read.")
            return False

        PyImGui.text(f"MAP: {self.map_id}  {Map.GetMapName()}"
                     f"   explorable: {Map.IsExplorable()}   hard mode: {Party.IsHardMode()}")
        if Map.IsVanquishable():
            PyImGui.text(f"VANQUISH: killed {Map.GetFoesKilled()}   remaining {Map.GetFoesToKill()}")
        else:
            PyImGui.text("VANQUISH: not a vanquishable area")

        px, py = Player.GetXY()
        prim, sec = Agent.GetProfessionShortNames(Player.GetAgentID())
        bar = SkillBar.GetSkillbar()
        PyImGui.text(f"PLAYER: ({px:.0f}, {py:.0f})   {prim}/{sec}"
                     f"   party {Party.GetPartySize()}/{Map.GetMaxPartySize()}"
                     f"   capture signets: {bar.count(SIGNET_OF_CAPTURE)}")
        PyImGui.text(f"SKILLBAR IDS: {bar}")

        alive, stale, dead = self.enemy_counts()
        PyImGui.text(f"ENEMIES: in range {alive}   remembered {stale}   seen dead {dead}")

        total = sum(len(p) for p in self.planes)
        PyImGui.text(f"GEOMETRY: {len(self.planes)} planes, {total} trapezoids"
                     f"   spawn lists: {[len(s) for s in self.spawns]}")
        if self.geometry_error:
            PyImGui.text(f"GEOMETRY ERROR: {self.geometry_error}")

        for i, m in enumerate(self.spawn_match()):
            if m:
                near, n, median = m
                PyImGui.text(f"  spawn list {i + 1}: {near}/{n} enemies first seen within "
                             f"{SPAWN_MATCH_RADIUS:.0f}, median distance {median:.0f}")

        PyImGui.separator()
        self.show_geometry = PyImGui.checkbox("Geometry", self.show_geometry)
        PyImGui.same_line(0.0, -1.0)
        self.show_spawns = PyImGui.checkbox("Spawn points", self.show_spawns)
        PyImGui.same_line(0.0, -1.0)
        self.show_enemies = PyImGui.checkbox("Enemies", self.show_enemies)
        PyImGui.same_line(0.0, -1.0)
        if PyImGui.button("Reset view"):
            self.zoom, self.pan = 1.0, [0.0, 0.0]
        PyImGui.same_line(0.0, -1.0)
        if PyImGui.button("Dump snapshot"):
            self.dump()

        if PyImGui.button("Read vanquished areas (experimental)"):
            self.read_vanquished()
        PyImGui.same_line(0.0, -1.0)
        if PyImGui.button("Read cartography grid (experimental)"):
            self.read_cartography()
        if self.vanquished_text:
            PyImGui.text_wrapped(f"VANQUISHED: {self.vanquished_text}")
        if self.carto_text:
            PyImGui.text(f"CARTOGRAPHY: {self.carto_text}")
        if self.last_dump:
            PyImGui.text_wrapped(f"SNAPSHOT: {self.last_dump}")
        PyImGui.separator()
        return True

    def draw_canvas(self):
        if not self.bounds:
            PyImGui.text("No pathing geometry loaded for this map.")
            return
        avail_w, avail_h = PyImGui.get_content_region_avail()
        if avail_w < 50 or avail_h < 50:
            return
        flags = (PyImGui.WindowFlags.NoMove | PyImGui.WindowFlags.NoScrollbar
                 | PyImGui.WindowFlags.NoScrollWithMouse)
        if PyImGui.begin_child("GWAMMCanvas", (avail_w, avail_h), border=False, flags=flags):
            ox, oy = PyImGui.get_window_pos()
            w, h = PyImGui.get_window_size()

            # pan with left-drag, zoom with the wheel
            mouse = PyImGui.get_mouse_pos()
            if PyImGui.is_window_hovered():
                wheel = PyImGui.get_io().mouse_wheel
                if wheel:
                    self.zoom = max(0.2, min(40.0, self.zoom * (1.15 ** wheel)))
                if PyImGui.is_mouse_down(0) and self.prev_mouse:
                    self.pan[0] += mouse[0] - self.prev_mouse[0]
                    self.pan[1] += mouse[1] - self.prev_mouse[1]
            self.prev_mouse = mouse

            min_x, min_y, max_x, max_y = self.bounds
            scale = min(w / (max_x - min_x), h / (max_y - min_y)) * 0.95 * self.zoom
            cx = ox + w / 2 + self.pan[0]
            cy = oy + h / 2 + self.pan[1]
            mid_x = (min_x + max_x) / 2
            mid_y = (min_y + max_y) / 2

            def to_screen(x, y):
                # game Y grows north; screen Y grows down
                return cx + (x - mid_x) * scale, cy - (y - mid_y) * scale

            x1, y1, x2, y2 = ox, oy, ox + w, oy + h

            if self.show_geometry:
                for i, traps in enumerate(self.planes):
                    col = COL_PLANE0 if i == 0 else COL_PLANE_N
                    for xtl, xtr, yt, xbl, xbr, yb in traps:
                        ax, ay = to_screen(xtl, yt)
                        bx, by = to_screen(xtr, yt)
                        ex, ey = to_screen(xbr, yb)
                        dx, dy = to_screen(xbl, yb)
                        if max(ax, bx, ex, dx) < x1 or min(ax, bx, ex, dx) > x2:
                            continue
                        if max(ay, ey) < y1 or min(ay, ey) > y2:
                            continue
                        PyImGui.draw_list_add_quad_filled(ax, ay, bx, by, ex, ey, dx, dy, col)

            if self.show_spawns:
                for i, lst in enumerate(self.spawns):
                    for sx, sy, _tag in lst:
                        px_, py_ = to_screen(sx, sy)
                        if x1 <= px_ <= x2 and y1 <= py_ <= y2:
                            PyImGui.draw_list_add_circle(px_, py_, 4.0, COL_SPAWN[i], 8, 1.5)

            if self.show_enemies:
                for e in self.enemies.values():
                    px_, py_ = to_screen(*e.xy)
                    if not (x1 <= px_ <= x2 and y1 <= py_ <= y2):
                        continue
                    if not e.alive:
                        PyImGui.draw_list_add_circle_filled(px_, py_, 2.5, COL_DEAD, 6)
                    elif not e.in_range:
                        PyImGui.draw_list_add_circle(px_, py_, 4.0, COL_STALE, 8, 1.5)
                    else:
                        PyImGui.draw_list_add_circle_filled(
                            px_, py_, 5.5 if e.boss else 3.5, COL_BOSS if e.boss else COL_ENEMY, 8)

            ppx, ppy = to_screen(*Player.GetXY())
            PyImGui.draw_list_add_circle_filled(ppx, ppy, 5.0, COL_PLAYER, 10)
            # compass range (5000) for scale
            PyImGui.draw_list_add_circle(ppx, ppy, 5000.0 * scale, _col(60, 255, 90, 90), 48, 1.0)
        PyImGui.end_child()

    def draw(self):
        st = self.update()
        if not self.window_sized:
            PyImGui.set_next_window_size(820, 900)
            self.window_sized = True
        if PyImGui.begin(MODULE_NAME):
            try:
                if self.draw_info(st):
                    self.draw_canvas()
            except Exception as e:
                PyImGui.text(f"Overlay error: {e!r}")
        PyImGui.end()


_overlay = Overlay()


def main():
    try:
        _overlay.draw()
    except Exception as e:
        _log(f"main() error: {e!r}", PySystem.Console.MessageType.Error)


if __name__ == "__main__":
    main()
