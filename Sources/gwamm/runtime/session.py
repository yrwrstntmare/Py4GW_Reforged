"""One vanquish attempt: owns the engine for the current instance and keeps it fed."""
import json
import os
import time

import PySystem

from .. import __version__
from ..core import elites
from ..core.engine import DONE, FAILED, Config, Engine
from ..core.world import World
from . import game
from .runlog import RunLog

ARRIVAL_WINDOW_MS = 20000      # building this soon after the map loads: we are standing at the way in
PERCEIVE_S = 0.25
CARTO_S = 3.0


class Session:
    def __init__(self):
        self.cfg = Config()
        self.engine = None
        self.map_id = 0
        self.error = ""
        self.result = ""
        self.elite_plan = None       # (secondary id, [Elite])
        self.exits = []
        self.nav_note = ""
        self.lead_note = ""
        self._routes, self.route_note = None, ""
        self.resumed = ""
        from .pcons import Pcons
        self.pcons = Pcons(self)
        self.party, self.party_note = {"players": 1, "heroes": 0, "henchmen": 0}, ""
        self.load_options()
        self.transit = None          # crossing an area: {"xy": door point}; None = normal vanquish
        self._built_for = None
        try:
            self.world = World()
        except Exception:
            self.world = None
        self.learned_note = ""
        self.exits_found = 0
        self.resting = ""
        self.targets, self.captured = [], set()
        self.captures = []           # elite capture attempts this session
        self.log = RunLog()
        self.stalls = 0              # steps abandoned because nothing moved and nothing died
        self._last_perceive = 0.0
        self._last_carto = 0.0

    def drop(self):
        """Forget everything tied to the instance (map change, loading screen)."""
        if self.engine is not None:
            self.log.event("instance_end", reason="map changed or loading", result=self.result)
            self.log.close()
        self.engine = None
        self.map_id = 0
        self._built_for = None

    def _hazard_path(self):
        return os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "hazards.json")

    def _load_hazards(self):
        """Spots where the party died with no enemy near (traps), per map, kept between runs."""
        try:
            with open(self._hazard_path()) as f:
                return {k: [tuple(p) for p in v] for k, v in json.load(f).items()}
        except Exception:
            return {}

    def save_hazards(self):
        if self.engine is None or self.map_id is None:
            return
        try:
            data = self._load_hazards()
            data[str(self.map_id)] = [list(p) for p in self.engine.hazards]
            with open(self._hazard_path(), "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    # ---- carrying a run across a script reload ----
    # ---- settings kept between sessions (only those changed from the defaults) ----
    def _options_path(self):
        return os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "options.json")

    def _option_values(self):
        out = {}
        for k in dir(type(self.cfg)):
            if k.startswith("_") or k == "multibox":
                continue
            d = getattr(type(self.cfg), k)
            if isinstance(d, (bool, int, float)) and not callable(d):
                v = getattr(self.cfg, k)
                if isinstance(v, (bool, int, float)) and v != d:
                    out[k] = v
        return out

    def refresh_party(self):
        """Other players in the party (whatever heroes they or you also brought) change three
        things: the party is not rebuilt, areas are left by resigning together, and the other
        accounts are asked to use their own consumables. Worked out from the party itself."""
        mode = int(self.cfg.party_mode)
        self.party = game.party_makeup()
        self.cfg.multibox = (self.party["players"] > 1) if mode == 0 else (mode == 2)
        return self.cfg.multibox

    def load_options(self):
        try:
            with open(self._options_path(), encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                d = getattr(type(self.cfg), k, None)
                if isinstance(d, bool):
                    setattr(self.cfg, k, bool(v))
                elif isinstance(d, int) and isinstance(v, (int, float)):
                    setattr(self.cfg, k, int(v))
                elif isinstance(d, float) and isinstance(v, (int, float)):
                    setattr(self.cfg, k, float(v))
        except Exception:
            pass
        self._options_saved = self._option_values()

    def save_options_if_changed(self):
        now = self._option_values()
        if now == getattr(self, "_options_saved", None):
            return
        self._options_saved = now
        try:
            os.makedirs(os.path.dirname(self._options_path()), exist_ok=True)
            with open(self._options_path(), "w", encoding="utf-8") as f:
                json.dump(now, f, indent=1)
        except Exception:
            pass

    def _resume_path(self):
        return os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "resume.json")

    def save_resume(self):
        """Write down what this run has done so far (where the party has walked, deaths, map cells
        given up on, elites taken). Reloading the script without leaving the area picks it up."""
        eng = self.engine
        if eng is None or self.transit is not None or not self.map_id:
            return
        try:
            mem = eng.mem
            data = {"map_id": self.map_id, "wall": time.time(), "uptime_ms": game.instance_uptime_ms(),
                    "version": __version__,
                    "route": [[round(x), round(y)] for x, y in mem.route],
                    "strict": bool(mem.strict), "search_radius": mem.search_radius, "escalation": mem.escalation,
                    "deaths": eng.deaths, "fresh_starts": eng.fresh_starts,
                    "declined": sorted(list(c) for c in (eng.carto.declined if eng.carto else ())),
                    "carto_given_up": eng.carto_given_up,
                    "captured": sorted(self.captured), "captures": self.captures[-10:],
                    # enemies seen and still to be dealt with: far ones are not in the game's list
                    # after a reload, so without this a group left for later would be forgotten
                    "enemies": [[e.id, round(e.xy[0]), round(e.xy[1]), bool(e.boss)]
                                for e in mem.enemies.values() if e.alive and not e.lost]}
            tmp = self._resume_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._resume_path())
        except Exception:
            pass

    def _try_resume(self, mid):
        """If the saved run is this very instance (same map, and the instance clock has advanced
        exactly as much as the wall clock since it was saved), replay it into the new engine."""
        try:
            with open(self._resume_path()) as f:
                d = json.load(f)
        except Exception:
            return
        try:
            if int(d.get("map_id", 0)) != mid or not d.get("route"):
                return
            wall_gap = time.time() - float(d["wall"])
            clock_gap = (game.instance_uptime_ms() - float(d["uptime_ms"])) / 1000.0
            if clock_gap < 0 or abs(wall_gap - clock_gap) > 20.0:
                self.log.event("resume", used=False, reason="a different visit to this area",
                               wall_gap=round(wall_gap), clock_gap=round(clock_gap))
                return
            eng, mem = self.engine, self.engine.mem
            mem.strict, mem.search_radius, mem.escalation = bool(d.get("strict")), float(d["search_radius"]), int(d.get("escalation", 0))
            mem._set_need()
            for x, y in d["route"]:
                mem.visit((float(x), float(y)))
            mem._last_xy = None                       # do not count the jump to where we stand now as walking
            eng.deaths, eng.fresh_starts = int(d.get("deaths", 0)), int(d.get("fresh_starts", 0))
            if eng.carto is not None:
                eng.carto.declined |= {tuple(c) for c in d.get("declined", [])}
                eng.carto_given_up = int(d.get("carto_given_up", 0))
            self.captured |= {int(c) for c in d.get("captured", [])}
            from ..core.memory import Enemy
            known = 0
            for eid, x, y, boss in d.get("enemies", []):
                if int(eid) not in mem.enemies:
                    e = mem.enemies[int(eid)] = Enemy(int(eid), (float(x), float(y)), time.time(), bool(boss))
                    e.in_range = False               # remembered, not seen: the next look confirms or drops it
                    known += 1
            counts = mem.counts()
            self.log.event("resume", used=True, minutes_ago=round(wall_gap / 60.0, 1), trail_points=len(d["route"]),
                           enemies_remembered=known, deaths=eng.deaths, regions=len(eng.rm.regions), searched=str(counts)[:120],
                           saved_by=d.get("version"))
            self.resumed = f"picked up this run from {round(wall_gap / 60.0, 1)} min ago ({len(d['route'])} trail points, {known} enemies remembered)"
        except Exception as e:
            self.log.event("resume", used=False, error=repr(e))

    def _dump_geometry(self, mid, traps, links):
        try:
            import json
            path = os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", f"geometry_{mid}.json")
            with open(path, "w") as f:
                json.dump({"map_id": mid, "player": list(game.player_xy()), "traps": traps, "links": links}, f)
        except Exception:
            pass

    def ensure_engine(self):
        """Build the map model once per instance. Returns True when ready."""
        if not game.map_ready():
            if self.engine is not None:
                self.drop()
            return False
        mid = game.map_id()
        want = (mid, tuple(self.transit["xy"]) if self.transit else None)
        if self.engine is not None and self._built_for == want:
            return True
        self.drop()
        try:
            traps = game.read_trapezoids()
            if not traps:
                self.error = "no pathing geometry for this map"
                return False
            goto = tuple(self.transit["xy"]) if self.transit else None
            projection = game.read_projection() if (self.cfg.do_cartography and goto is None) else None
            found = self._learned_exits(mid) + game.read_exits()      # remembered arrivals first
            if self.world is not None:
                found += self.world.exits_of(mid)                     # every door the world map knows
            if goto is not None:
                # the door we are heading for must not be fenced off
                found = [e for e in found if (e[0] - goto[0]) ** 2 + (e[1] - goto[1]) ** 2 > 3000.0 ** 2]
            try:
                links = game.read_level_links()
            except Exception as e:
                links, self.nav_note = [], f"level links unreadable: {e!r}"
            else:
                self.nav_note = f"{len(links)} level links"
            guide, hints, self.route_note = None, None, ""
            if goto is None and (self.cfg.use_routes or self.cfg.route_hints) and self.cfg.do_vanquish:
                try:
                    if self._routes is None:
                        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                               "data", "routes.json"), encoding="utf-8") as f:
                            self._routes = json.load(f)
                    r = self._routes.get(str(mid))
                    if r:
                        hints = [tuple(p) for p in r["points"]] if self.cfg.route_hints else None
                        guide = [tuple(p) for p in r["points"]] if self.cfg.use_routes else None
                        self.route_note = (f"known route '{r['name']}' ({len(r['points'])} points): "
                                           + ("followed point by point" if guide else "used as a guide to where to search"))
                except Exception as e:
                    self.route_note = f"route unreadable: {e!r}"
            self.engine = Engine(traps, game.player_xy(), self.cfg, projection, found, goto=goto, links=links, guide=guide, hints=hints)
            self.pcons.reset()
            if guide:
                self.route_note += f", {len(self.engine.guide)} usable from here"
            elif hints:
                self.route_note += f"; it covers {len(self.engine.hint_regions)} of {len(self.engine.rm.regions)} regions"
            share = sum(self.engine.rm.reachable) / max(1, len(self.engine.nav.nodes))
            if share < 0.9:
                # Much of the map looks cut off from where we stand. Join every pair of levels
                # that touch and keep that if it opens the map up.
                from ..core.geometry import loose_links
                extra = links + loose_links(traps)
                wider = Engine(traps, game.player_xy(), self.cfg, projection, found, goto=goto, links=extra, guide=guide, hints=hints)
                share2 = sum(wider.rm.reachable) / max(1, len(wider.nav.nodes))
                self.nav_note += f"; only {share:.0%} reachable, {share2:.0%} after joining touching levels"
                if share2 > share + 0.05:
                    self.engine, share = wider, share2
                if share < 0.6:
                    self._dump_geometry(mid, traps, links)
            try:
                self.engine.doors = self._learned_exits(mid) + game.read_travel_portals()
            except Exception:
                self.engine.doors = []
            self.engine.hazards = list(self._load_hazards().get(str(mid), []))
            self._built_for = want
            self.exits_found = len(found)
            self.exits = self.engine.exits                            # only the ones that matter here
            self.map_id = mid
            self.error = ""
            self.result = ""
            self._plan_elites(mid)
            eng = self.engine
            self.log.open(mid)
            self.log.event("start", version=__version__, map_id=mid, player=list(eng.player_xy), regions=len(eng.rm.regions),
                           nodes=len(eng.nav.nodes), reachable=sum(eng.rm.reachable), exits=self.exits,
                           carto=None if eng.carto is None else {"anchor": [eng.carto.proj.ax, eng.carto.proj.ay],
                                                                 "stand_cells": len(eng.carto.stand),
                                                                 "coverable": len(eng.carto.coverable)},
                           hazards=len(eng.hazards), nav_note=self.nav_note, route_note=self.route_note, exits_found=self.exits_found, exit_note=game.exit_note, learned_note=self.learned_note,
                           foes=game.foes_remaining(), killed=game.foes_killed(),
                           professions=list(game.player_professions()), signets=game.capture_signets(),
                           config={k: getattr(self.cfg, k) for k in dir(self.cfg) if not k.startswith("_")},
                           elites=[f"{e.name} ({e.boss})" for e in self.targets])
            self.resumed = ""
            if goto is None:
                self._try_resume(mid)
                self._last_resume_save = time.time()
        except Exception as e:
            self.error = f"could not build the map model: {e!r}"
            self.engine = None
            return False
        return True

    def _learned_exits(self, mid):
        """Where the party has arrived in this map before. Each arrival point sits beside the
        portal it came through, so it is remembered (gwamm_logs/known_exits.json) and avoided
        from then on. This covers portals the other two detection methods miss."""
        path = os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "known_exits.json")
        known = {}
        try:
            with open(path, encoding="utf-8") as f:
                known = json.load(f)
        except Exception:
            known = {}
        points = [tuple(p) for p in known.get(str(mid), [])]
        self.learned_note = f"{len(points)} remembered arrival points"
        try:
            uptime = game.instance_uptime_ms()
            if game.is_explorable() and 0 < uptime < ARRIVAL_WINDOW_MS:
                x, y = game.player_xy()
                if all((x - px) ** 2 + (y - py) ** 2 > 400.0 ** 2 for px, py in points):
                    points.append((x, y))
                    known[str(mid)] = [list(p) for p in points]
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(known, f)
                    self.learned_note = f"{len(points)} remembered arrival points (added this one)"
        except Exception as e:
            self.learned_note += f"; could not record arrival: {e!r}"
        return points

    def _plan_elites(self, mid):
        """Work out which elites to go for here, best first. `targets` is every capturable one;
        the bot spends its signets from the top of that list."""
        self.targets, self.captured = [], set()
        area = elites.AREAS.get(mid)
        if not area:
            self.elite_plan = None
            return
        primary, secondary = game.player_professions()
        learnt = {e.skill_id for e in area if game.skill_learnt(e.skill_id)}
        try:
            done = game.vanquished_ids()
        except Exception:
            done = set()
        self.targets = elites.prioritise(primary, secondary, area, learnt, mid, done)
        self.elite_plan = (secondary, self.targets[:max(1, game.capture_signets())])

    def perceive(self, force=False):
        now = time.time()
        if not force and now - self._last_perceive < PERCEIVE_S:
            return
        self._last_perceive = now
        grid = None
        if self.engine.carto is not None and now - self._last_carto >= CARTO_S:
            self._last_carto = now
            grid = game.read_carto_grid()
        self.engine.update(game.player_xy(), game.read_enemies(), game.foes_remaining(), now, grid)
        if now - self.__dict__.get("_last_resume_save", 0.0) >= 20.0:
            self._last_resume_save = now
            self.save_resume()

    def finished(self):
        return self.engine is not None and self.engine.mode in (DONE, FAILED)
