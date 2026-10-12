"""CrossNode: step through a door from one area into the next."""
import json
import math
import os
import time

import PySystem

from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Sources.ApoSource.ApoBottingLib import wrappers as BT

from ..core import elites, tactics
from ..core.engine import DONE
from . import game

from .guard import guarded_tick


def _spots_path():
    return os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "gate_spots.json")


def gate_spot(from_map, to_map):
    """Where the door from one map to another really took us through last time, and which way
    we were walking: (x, y, dx, dy), or None. Learnt by CrossNode each time a door is crossed;
    recorded paths and portal markers can sit to one side of the real trigger (Mihanu Township
    into Holdings of Chokhin)."""
    try:
        with open(_spots_path(), encoding="utf-8") as f:
            v = json.load(f).get(f"{int(from_map)}>{int(to_map)}")
        return tuple(float(n) for n in v[:4]) if v else None
    except Exception:
        return None


def _remember_gate_spot(from_map, to_map, spot):
    try:
        try:
            with open(_spots_path(), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[f"{int(from_map)}>{int(to_map)}"] = [round(n, 3) if i > 1 else round(n) for i, n in enumerate(spot)]
        with open(_spots_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


class CrossNode(BehaviorTree.Node):
    """Step through a door: walk at it, then keep walking at the far side's arrival point until
    the map changes. Used only when leaving is intended (never during a vanquish)."""

    def __init__(self, session, leg, from_map, timeout_s=75.0):
        super().__init__(name="CrossDoor", node_type="CrossDoor", node_category="action")
        self.session, self.leg, self.from_map, self.timeout_s = session, leg, from_map, timeout_s
        self._t0, self._last_move, self._stage = 0.0, 0.0, 0

    def reset(self):
        super().reset()
        self._t0, self._stage = 0.0, 0

    def _tick_impl(self):
        return guarded_tick(self, self.session, lambda: BehaviorTree.NodeState.FAILURE)

    def _tick_core(self):
        S, now = BehaviorTree.NodeState, time.time()
        if not game.map_ready():
            return S.RUNNING                                  # loading screen
        if game.map_id() != self.from_map:
            # where we stood and which way we were going when the door took us: kept for next time
            trail, spot = self.__dict__.get("_trail") or [], None
            if trail:
                lx, ly = trail[-1][1], trail[-1][2]
                back = next(((x, y) for t, x, y in reversed(trail) if math.hypot(lx - x, ly - y) >= 150.0), None)
                if back is not None:
                    n = math.hypot(lx - back[0], ly - back[1])
                    spot = (lx, ly, (lx - back[0]) / n, (ly - back[1]) / n)
                    if self.leg.get("expect") in (None, game.map_id()):
                        _remember_gate_spot(self.from_map, game.map_id(), spot)
            self.session.log.event("crossed", from_map=self.from_map, to_map=game.map_id(), expected=self.leg.get("expect"),
                                   other_side=bool(getattr(self, "_alt", None)), learnt=bool(self.leg.get("learnt")),
                                   at=None if spot is None else [round(spot[0]), round(spot[1])],
                                   heading=None if spot is None else [round(spot[2], 2), round(spot[3], 2)])
            return S.SUCCESS
        if not self._t0:
            self._t0 = now
            self._origin = game.player_xy()
        if now - self._t0 > self.timeout_s:
            self.session.log.event("cross_failed", from_map=self.from_map, leg=self.leg, player=list(game.player_xy()))
            return S.FAILURE
        first = self.leg.get("portal") or self.leg["xy"]
        beyond = self.leg.get("beyond") or first
        px, py = game.player_xy()
        trail = self.__dict__.setdefault("_trail", [])
        if not trail or now - trail[-1][0] >= 0.3:
            trail.append((now, px, py))
            del trail[:-200]
        if self._stage == 0 and math.hypot(px - first[0], py - first[1]) < 300.0:
            self._stage, self._at_door = 1, now
        # The recorded way out did not trigger the door: its last stretch can point along the
        # wall instead of through it (Mihanu Township: pushed west past the portal into a corner,
        # five tries). Turn to the portal the game shows nearest to it and walk through that,
        # trying its sides in turn.
        if self._stage == 1 and getattr(self, "_alt", None) is None and now - self._at_door > 15.0:
            self._alt = False
            try:
                near = [p for p in game.travel_portals() if math.hypot(p[0] - first[0], p[1] - first[1]) < 900.0]
            except Exception:
                near = []
            if near and math.hypot(beyond[0] - first[0], beyond[1] - first[1]) >= 1.0:
                p = min(near, key=lambda q: math.hypot(q[0] - first[0], q[1] - first[1]))
                self._alt, self._at_door = (float(p[0]), float(p[1])), now
                self.timeout_s = max(self.timeout_s, now - self._t0 + 55.0)      # time for all four sides
                start = self.leg.get("start")
                if start:
                    self._origin = (float(start[0]), float(start[1]))
                self.session.log.event("cross_other_side", from_map=self.from_map, recorded=list(first),
                                       portal=list(self._alt), player=[round(px), round(py)])
        if getattr(self, "_alt", None):
            first = beyond = self._alt
        if now - self._last_move >= 1.2:
            self._last_move = now
            if self._stage == 0:
                game.move_to(first[0], first[1])
            else:
                # push on past the arrival point: the trigger can sit a little beyond it
                dx, dy = beyond[0] - first[0], beyond[1] - first[1]
                n = math.hypot(dx, dy)
                if n < 1.0:
                    # No far side known (an outpost portal): keep walking the way we came in, and if
                    # that does not trigger it, try the other sides of the portal in turn. Before,
                    # the push was "0 past the portal" and the leader stood on it for two minutes
                    # (Nundu Bay, four tries).
                    ox, oy = getattr(self, "_origin", (px, py))
                    ax, ay = first[0] - ox, first[1] - oy
                    an = math.hypot(ax, ay) or 1.0
                    ax, ay = ax / an, ay / an
                    turn = int((now - getattr(self, "_at_door", now)) / 12.0) % 4
                    dx, dy = [(ax, ay), (-ay, ax), (ay, -ax), (-ax, -ay)][turn]
                    n = 1.0
                reach = n + 600.0 + 400.0 * min(6, int((now - self._t0) / 8.0))
                game.move_to(first[0] + dx / n * reach, first[1] + dy / n * reach)
        return S.RUNNING
