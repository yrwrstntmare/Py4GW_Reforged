"""Situational weapon sets for the leader (core.gear decides, this does the equipping).

Nothing is ever dropped, sold or moved anywhere but between the leader's hands and the bags:
equipping from a bag swaps the item held into a bag. The main set (what was held when the run
began, or the set a crashed run was swapped out of) is written to gwamm_logs/gear_main.json
before the first swap and put back:
  - whenever nothing else is clearly better,
  - while crossing an area on the way somewhere,
  - at the end of every area (and on the first look of the next run, if a run stopped mid-swap).
After each equip the hands are checked; an equip that did not happen stops swapping for the
rest of the area."""
import json
import math
import os
import time

import PySystem

from ..core import gear
from . import game

CHECK_EVERY_S = 30.0


def _path():
    return os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "gear_main.json")


class GearSwap:
    def __init__(self, session):
        self.s = session
        self.main = None            # ids of the main set
        self.swapped = False
        self.off = False            # given up for this area (an equip failed)
        self._next = 0.0
        self._names = {}            # enemy agent id -> (name, primary profession)
        self._pending = None        # (ids wanted, deadline)

    # ---- the main set ----
    def _load(self):
        try:
            with open(_path(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        try:
            with open(_path(), "w", encoding="utf-8") as f:
                json.dump({"main": list(self.main or ()), "swapped": self.swapped}, f)
        except Exception:
            pass

    def _ensure_main(self):
        if self.main is not None:
            return
        d = self._load()
        if d.get("swapped") and d.get("main") and all(game.item_where(i) for i in d["main"]):
            self.main, self.swapped = [int(i) for i in d["main"]], True     # a run stopped mid-swap
            self.s.log.event("gear", note="last run stopped while swapped: main set to be put back", main=self.main)
        else:
            self.main = [int(i) for i in game.held_gear_ids()]

    # ---- equipping ----
    def _equip(self, ids, why):
        ids = [int(i) for i in ids if i]
        if not ids:
            return
        missing = [i for i in ids if not game.item_where(i)]
        if missing:
            self.s.log.event("gear", error="items not in the inventory", ids=missing)
            self.off = True
            return
        game.equip_items(ids)
        self._pending = (set(ids), time.time() + 3.0)
        self.s.log.event("gear", equip=ids, why=why, held_before=game.held_gear_ids())

    def _check_pending(self):
        if self._pending is None:
            return True
        want, until = self._pending
        held = set(game.held_gear_ids())
        if want <= held:
            self._pending = None
            return True
        if time.time() > until:
            self._pending = None
            self.off = True
            self.s.log.event("gear", error="equip did not take; no more swapping in this area",
                             wanted=sorted(want), held=sorted(held))
            return True
        return False

    def restore(self, why):
        """Put the main set back (if it is not held already)."""
        if self.main is None:
            return
        if set(game.held_gear_ids()) != set(self.main):
            self._equip(self.main, why)
        self.swapped = False
        self._save()

    def new_area(self):
        self._names, self.off, self._next = {}, False, time.time() + 10.0
        self._ensure_main()
        if self.swapped:
            self.restore("new area")

    # ---- the per-tick look ----
    def tick(self, eng, transit, fighting):
        if not getattr(self.s.cfg, "gear_swap", True) or self.off:
            return
        now = time.time()
        if not self._check_pending() or now < self._next:
            return
        self._next = now + CHECK_EVERY_S
        self._ensure_main()
        if transit is not None:
            if self.swapped:
                self.restore("crossing an area")
            return
        px, py = eng.player_xy
        if fighting or any(e.alive and e.in_range and math.hypot(e.xy[0] - px, e.xy[1] - py) < 2500.0
                           for e in eng.mem.enemies.values()):
            self._next = now + 5.0           # never mid-fight: look again soon
            return
        for e in eng.mem.enemies.values():
            if e.id not in self._names and e.in_range:
                self._names[e.id] = (game.agent_name(e.id), game.tracker_primary(game.enemy_model(e.id)))
        prof = gear.profile([v for v in self._names.values() if v[0]])
        try:
            items = game.gear_items()
            # the main set's pieces may have been put back into another bag by a swap
            have = {i["id"] for i in items}
            items += [i for i in game.gear_items((1, 2, 3, 4)) if i["id"] in self.main and i["id"] not in have]
        except Exception as ex:
            self.s.log.event("gear", error=repr(ex))
            self.off = True
            return
        pick = gear.choose(items, game.held_gear_ids(), self.main, prof, game.leader_attributes())
        if pick is None:
            return
        main_item, off_item, gain, base_loss, why = pick
        ids = [main_item["id"]] + ([off_item["id"]] if off_item else [])
        if set(ids) == set(self.main):
            self.restore(why)
            return
        self.swapped = True
        self._save()                         # main set on disk before anything moves
        self.s.log.event("gear", plan=ids, gain=gain, base_given_up=base_loss, why=why,
                         species={k: round(v, 2) for k, v in prof["species"].items()},
                         elemental=round(prof["elemental"], 2), physical=round(prof["physical"], 2))
        self._equip(ids, why)
