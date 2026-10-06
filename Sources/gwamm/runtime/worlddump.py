"""One-off export of what the game knows about every map: where it sits on the world map,
its portals, arrival points and walkable bounds. Used to work out which areas connect to
which, so the bot can walk to places it cannot map-travel to and chain neighbouring areas.
Spread over many frames so the game does not freeze."""
import json
import os

import PySystem
from Py4GWCoreLib.Map import Map
from Py4GWCoreLib.native_src.methods.FfnaMapMethods import FfnaMapMethods
from Py4GWCoreLib.native_src.methods.MapMethods import MapMethods

from ..core.geometry import NavGraph

LAST_MAP_ID = 900


class WorldDump:
    def __init__(self):
        self.progress, self.done, self.error, self.path = 0, False, "", ""
        self._gen = None

    def start(self):
        self.progress, self.done, self.error = 0, False, ""
        self._gen = self._run()

    def running(self):
        return self._gen is not None

    def tick(self):
        if self._gen is None:
            return
        try:
            next(self._gen)
        except StopIteration:
            self._gen = None
        except Exception as e:
            self.error, self._gen = repr(e), None

    def _run(self):
        out = {}
        for mid in range(1, LAST_MAP_ID + 1):
            self.progress = mid
            entry = None
            try:
                info = MapMethods.GetMapInfo(mid)
                name = Map.GetMapName(mid)
                if info is not None and name:
                    entry = {"name": name, "campaign": int(info.campaign), "continent": int(info.continent),
                             "region": int(info.region), "type": int(info.type), "flags": int(info.flags),
                             "max_party": int(info.max_party_size),
                             "icon": [int(info.icon_start_x), int(info.icon_start_y), int(info.icon_end_x), int(info.icon_end_y)],
                             "icon_dupe": [int(info.icon_start_x_dupe), int(info.icon_start_y_dupe),
                                           int(info.icon_end_x_dupe), int(info.icon_end_y_dupe)],
                             "file": [int(info.file_id1), int(info.file_id2)],
                             "unlocked": bool(Map.IsMapUnlocked(mid))}
                    if FfnaMapMethods.HasDatEntry(mid):
                        entry["portals"] = [[p.x, p.y] for p in Map.Pathing.GetTravelPortals(mid)]
                        s1, s2, s3 = Map.Pathing.GetSpawns(mid)
                        entry["spawns"] = [[[s.x, s.y, s.tag] for s in lst] for lst in (s1, s2, s3)]
                        xs, ys, planes, traps = [], [], 0, []
                        for plane, layer in enumerate(Map.Pathing.GetPathingMaps(mid)):
                            planes += 1
                            for t in layer.trapezoids:
                                traps.append((plane, t.XTL, t.XTR, t.YT, t.XBL, t.XBR, t.YB))
                                xs += [t.XTL, t.XTR, t.XBL, t.XBR]
                                ys += [t.YT, t.YB]
                        if xs:
                            entry["bounds"] = [min(xs), min(ys), max(xs), max(ys)]
                        entry["planes"], entry["trapezoids"] = planes, len(traps)
                        # Which separately walkable piece of the map each door and arrival point
                        # stands on. A file can hold an outpost and several areas side by side;
                        # they are walled off from each other, so the piece tells us which map
                        # a door belongs to.
                        if traps:
                            nav = NavGraph(traps)
                            label = [-1] * len(nav.nodes)
                            sizes = []
                            for start in range(len(nav.nodes)):
                                if label[start] >= 0:
                                    continue
                                cid, stack, count = len(sizes), [start], 0
                                label[start] = cid
                                while stack:
                                    u = stack.pop()
                                    count += 1
                                    for v, _w in nav.adj[u]:
                                        if label[v] < 0:
                                            label[v] = cid
                                            stack.append(v)
                                sizes.append(count)

                            def piece(x, y):
                                i = nav.nearest_node(x, y, max_radius=3000.0)
                                return -1 if i is None else label[i]

                            entry["piece_sizes"] = sizes
                            entry["portal_piece"] = [piece(x, y) for x, y in entry["portals"]]
                            entry["spawn_piece"] = [[piece(x, y) for x, y, _t in lst] for lst in entry["spawns"]]
                        Map.Pathing.ClearPathingCache(mid)
            except Exception as e:
                entry = dict(entry or {}, error=repr(e))
            if entry:
                out[mid] = entry
            yield
        folder = os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs")
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "world_dump_v2.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(out, f)
        self.done = True


class GeometryDump(WorldDump):
    """Export of the walkable ground of every area on the list, one small file per area in
    gwamm_logs/geometry, so pathing problems can be found and fixed without visiting each map."""

    def __init__(self, map_ids_fn):
        super().__init__()
        self._ids_fn, self.total, self.written, self.failed = map_ids_fn, 0, 0, []

    def _run(self):
        import gzip
        ids = sorted(self._ids_fn())
        self.total, self.written, self.failed = len(ids), 0, []
        folder = os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "geometry")
        os.makedirs(folder, exist_ok=True)
        self.path = folder
        for n, mid in enumerate(ids):
            self.progress = n + 1
            try:
                if not FfnaMapMethods.HasDatEntry(mid):
                    raise RuntimeError("no map file")
                traps, portals = [], []
                for plane, layer in enumerate(Map.Pathing.GetPathingMaps(mid)):
                    for t in layer.trapezoids:
                        traps.append([plane, t.XTL, t.XTR, t.YT, t.XBL, t.XBR, t.YB, int(t.id),
                                      [int(i) for i in t.neighbor_ids]])
                    for p in layer.portals:
                        portals.append([plane, int(p.left_layer_id), int(p.right_layer_id),
                                        [int(i) for i in p.trapezoid_indices]])
                s1, s2, s3 = Map.Pathing.GetSpawns(mid)
                data = {"map_id": mid, "name": Map.GetMapName(mid), "traps": traps, "portals": portals,
                        "travel_portals": [[p.x, p.y] for p in Map.Pathing.GetTravelPortals(mid)],
                        "spawns": [[[s.x, s.y, s.tag] for s in lst] for lst in (s1, s2, s3)]}
                with gzip.open(os.path.join(folder, f"{mid}.json.gz"), "wt", encoding="utf-8") as f:
                    json.dump(data, f)
                self.written += 1
                Map.Pathing.ClearPathingCache(mid)
            except Exception as e:
                self.failed.append([mid, repr(e)])
            yield
        with open(os.path.join(folder, "_summary.json"), "w", encoding="utf-8") as f:
            json.dump({"written": self.written, "total": self.total, "failed": self.failed}, f)
        self.done = True
