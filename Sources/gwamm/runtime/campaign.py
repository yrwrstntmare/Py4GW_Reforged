"""Campaign: a queue of areas to vanquish one after another, travel included.

Travel data (outpost, the walk to its exit, and any areas to cross on the way) comes from
Reforged's PyQuishAI map files, converted into data/travel.json. It is used only to GET to an
area; once there, the adaptive engine does the rest with no recorded route.
"""
import json
import os
import time

import PySystem
from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Sources.ApoSource.ApoBottingLib import wrappers as BT

from ..core import builds as teambuilds
from ..core import elites
from . import game

_HERE = os.path.dirname(os.path.abspath(__file__))
MAX_ATTEMPTS = 2
MAX_STARTS = 4           # times one area may be started over (after wipes that end in town) per run
EYE_OF_THE_NORTH = 642
SKILL_TRAINER_XY = (-3551.0, 2341.0)      # as used by Reforged's elite capture helper
SKILL_TRAINER_DIALOG = 0x84


def load_travel():
    with open(os.path.join(os.path.dirname(_HERE), "data", "travel.json"), encoding="utf-8") as f:
        return {int(k): v for k, v in json.load(f).items()}


class Campaign:
    def __init__(self):
        self.travel = load_travel()
        self.world = None
        try:
            from ..core.world import World
            self.world = World()
        except Exception:
            self.world = None
        self._unlocked, self._unlocked_at = set(), 0.0
        self._hops = {}
        self.queue = []          # map ids, in order
        self.results = {}        # map id -> {"result", "minutes", "attempts", "when"}
        self.current = None
        self.phase = ""
        self.capture_template = ""   # your saved bar; signets are swapped onto it as each area needs
        self.signet_slots = [5, 6]   # bar slots given up for signets, in order
        self.max_signets = 2
        self.exit_when_done = False  # close the game once the queue has been worked through
        self.nodata_signets = 1      # signets to carry into an area we have no boss list for
        self.builds = {}             # "party size:signets" -> saved team (bar + heroes)
        self.library = []            # teams imported from Toolbox, to choose from
        self.toolbox_path = ""
        self.import_note = ""
        self._path = os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "campaign.json")
        self.load()

    # ---- persistence ----
    def load(self):
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self.queue = [int(m) for m in data.get("queue", []) if int(m) in self.all_areas()]
            self.results = {int(k): v for k, v in data.get("results", {}).items()}
            for r in self.results.values():      # a fresh script start gives unfinished areas a fresh go
                if r.get("result") != "complete":
                    r["attempts"] = 0
            self.capture_template = data.get("capture_template", "")
            self.signet_slots = [int(x) for x in data.get("signet_slots", self.signet_slots)][:2]
            self.max_signets = int(data.get("max_signets", self.max_signets))
            self.nodata_signets = int(data.get("nodata_signets", self.nodata_signets))
            self.exit_when_done = bool(data.get("exit_when_done", False))
            self.builds = dict(data.get("builds", {}))
            self.library = list(data.get("library", []))
            self.toolbox_path = data.get("toolbox_path", "")
        except Exception:
            pass
        try:                             # this computer's own paths live apart from what gets shared
            with open(self._local_path(), encoding="utf-8") as f:
                self.toolbox_path = json.load(f).get("toolbox_path", "") or self.toolbox_path
        except Exception:
            pass

    def _local_path(self):
        return os.path.join(os.path.dirname(self._path), "local.json")

    def save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump({"queue": self.queue, "results": self.results,
                           "capture_template": self.capture_template, "signet_slots": self.signet_slots,
                           "max_signets": self.max_signets, "nodata_signets": self.nodata_signets, "exit_when_done": self.exit_when_done,
                           "builds": self.builds, "library": self.library}, f)
            with open(self._local_path(), "w", encoding="utf-8") as f:
                json.dump({"toolbox_path": self.toolbox_path}, f)
        except Exception:
            pass

    # ---- team builds ----
    def import_toolbox(self, path=""):
        """Read GWToolbox++'s saved team builds. Fills empty slots that fit this character and
        refreshes every slot already pointing at a team of the same name, so edits made in
        Toolbox reach the bot. Always reads the newest build file it can find near the path
        given (Toolbox moved from herobuilds.ini to configs/<profile>/herobuilds.json)."""
        import os
        path = game.find_toolbox_file(path or self.toolbox_path) or path or self.toolbox_path
        if not path:
            self.import_note = "could not find herobuilds.json or herobuilds.ini; paste the full path above"
            return
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                teams = teambuilds.parse_toolbox(f.read(), path)
            saved = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
        except Exception as e:
            self.import_note = f"could not read {path}: {e!r}"
            return
        primary = game.player_professions()[0]
        mine = [t for t in teams if t.primary == primary]
        self.toolbox_path = path
        self.library = [t.to_dict() for t in mine]
        by_name, updated = {t.name: t.to_dict() for t in mine}, 0
        for key, cur in list(self.builds.items()):
            new = by_name.get(cur.get("name"))
            if new is not None and new != cur:
                self.builds[key] = new
                updated += 1
        filled = teambuilds.auto_assign(mine, primary, self.builds)
        self.import_note = (f"read {len(teams)} teams ({len(mine)} for this profession) from {path}, which Toolbox "
                            f"last saved {saved}. Updated {updated} slots, filled {filled} empty ones. "
                            f"If a change is missing, Toolbox has not written it to disk yet: use Save Now "
                            f"in Toolbox's settings, then import again.")
        self.save()

    def save_current_team(self):
        """Store the bar and heroes you have right now in the slot they fit."""
        team = teambuilds.Team("saved in game", game.bar_template(),
                               [[h, "", 1] for h in game.party_hero_ids()])
        key = teambuilds.slot_key(team.size, min(2, team.signets))
        self.builds[key] = team.to_dict()
        self.save()
        return key

    def team_for(self, size, signets):
        return teambuilds.pick(self.builds, size, signets)

    # ---- queue building ----
    def unlocked(self, refresh=False):
        """Outposts this character can map-travel to (checked at most every 20 seconds)."""
        if refresh or time.time() - self._unlocked_at > 20.0:
            ids = self.world.all_outposts() if self.world else {t["outpost"] for t in self.travel.values()}
            self._unlocked = {o for o in ids if game.map_unlocked(o)}
            self._unlocked_at = time.time()
            self._hops = {}
        return self._unlocked

    def all_areas(self):
        ids = set(self.travel)
        if self.world is not None:
            ids |= {m for m in self.world.vanquishable if m in self.world.area_node}
        return ids

    def regions(self):
        out = {}
        for mid in self.all_areas():
            region = self.travel[mid]["region"] if mid in self.travel else "Other areas"
            out.setdefault(region, []).append(mid)
        return {r: sorted(ids, key=game.map_name) for r, ids in sorted(out.items())}

    def hops(self, mid):
        """How many doors must be walked through to reach an area (0 = straight out of an
        outpost you have). None = no way there yet."""
        if mid not in self._hops:
            legs = self.world.route(mid, self.unlocked()) if self.world else None
            if legs is None:
                t = self.travel.get(mid)
                ok = t and t["plain"] and t["outpost"] in self.unlocked()
                self._hops[mid] = len(t["transit"]) if ok else None
            else:
                self._hops[mid] = sum(1 for leg in legs if leg["do"] == "door")
        return self._hops[mid]

    def available(self, mid, vanquished):
        """Why an area cannot be queued right now, or '' if it can."""
        if mid in vanquished:
            return "already vanquished"
        if self.hops(mid) is None:
            return "no route from your outposts yet"
        return ""

    def toggle(self, mid):
        if mid in self.queue:
            self.queue.remove(mid)
        else:
            self.queue.append(mid)
            r = self.results.get(mid)
            if r and r.get("result") != "complete":
                r["attempts"], r["travel_fails"] = 0, 0      # chosen again by hand: a fresh go
        self.save()

    def queue_all(self, vanquished, region=None):
        """Queue every area that can be done, grouped so areas sharing an outpost run back to back."""
        regions = self.regions()
        ids = [m for m in (regions.get(region, []) if region else self.all_areas())
               if not self.available(m, vanquished)]
        for m in ids:                                    # asked for by hand: every unfinished area gets a fresh go
            r = self.results.get(m)
            if r and r.get("result") != "complete":
                r["attempts"], r["travel_fails"] = 0, 0
        ids = self.order(ids)
        for m in ids:
            if m not in self.queue:
                self.queue.append(m)
        self.save()

    def pick_next(self, slot):
        """The area to run in this slot of the queue, chosen when the slot starts (not when
        Start was pressed), so outposts unlocked by the earlier areas count. Reachable areas
        come first, fewest areas to walk through first, then the nearest on the world map.
        The choice is kept for the slot, so a restart after a wipe resumes the same area."""
        plan = self.__dict__.setdefault("_plan", {})
        if slot in plan:
            return plan[slot]
        try:
            done = game.vanquished_ids()
        except Exception:
            done = set()
        taken = set(plan.values())
        left = [m for m in self.queue if m not in taken and m not in done
                and self.results.get(m, {}).get("attempts", 0) < MAX_ATTEMPTS]
        if not left:
            plan[slot] = None
            return None
        self.unlocked(refresh=True)
        try:
            here = game.map_world_pos(game.map_id())
        except Exception:
            here = None

        def cost(m):
            hops = self.hops(m)
            try:
                p = game.map_world_pos(m)
            except Exception:
                p = None
            far = 0.0 if not (here and p) else (1e12 if p[2] != here[2] else 0.0) + (p[0] - here[0]) ** 2 + (p[1] - here[1]) ** 2
            return (hops is None, hops or 0, far, self.queue.index(m))
        plan[slot] = min(left, key=cost)
        return plan[slot]

    def order(self, ids):
        """Visit order: stay on one continent, start with the area nearest to where the party is,
        and always go on to the nearest remaining one. Areas sharing an outpost end up together.
        (Distances are world-map distances; this does not yet know which areas actually connect.)"""
        pos = {}
        for m in ids:
            try:
                pos[m] = game.map_world_pos(m)
            except Exception:
                pos[m] = None
        try:
            here = game.map_world_pos(game.map_id())
        except Exception:
            here = None
        left, out = [m for m in ids if pos[m]], []
        rest = sorted((m for m in ids if not pos[m]), key=game.map_name)
        cur = here
        while left:
            def cost(m):
                p = pos[m]
                if cur is None:
                    return (p[2], p[0], p[1])
                far = 0 if p[2] == cur[2] else 1
                return (far, (p[0] - cur[0]) ** 2 + (p[1] - cur[1]) ** 2, 0)
            # among the nearest few, take one that needs no walking through other areas first:
            # doing it may unlock an outpost that shortens the others
            near = sorted(left, key=cost)[:3]
            near.sort(key=lambda m: (self.hops(m) or 0))
            left.remove(near[0])
            out.append(near[0])
            cur = pos[near[0]]
            continue
            nxt = min(left, key=cost)
            left.remove(nxt)
            out.append(nxt)
            cur = pos[nxt]
        return out + rest

    def record(self, mid, result, started):
        r = self.results.setdefault(mid, {"attempts": 0})
        if "could not travel" not in str(result):    # never reaching the area is not an attempt at it
            r["attempts"] = r.get("attempts", 0) + 1
        else:
            r["travel_fails"] = r.get("travel_fails", 0) + 1
            if r["travel_fails"] >= 4:
                r["attempts"] = MAX_ATTEMPTS
        r["result"] = result
        r["minutes"] = round((time.time() - started) / 60.0, 1)
        r["when"] = time.strftime("%Y-%m-%d %H:%M")
        if mid in self.queue and (result == "complete" or r["attempts"] >= MAX_ATTEMPTS):
            self.queue.remove(mid)
        self.save()


from .maprun import MapRunNode      # noqa: E402  (kept importable from here)
