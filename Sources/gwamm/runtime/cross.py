"""CrossNode: step through a door from one area into the next."""
import math
import time

from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Sources.ApoSource.ApoBottingLib import wrappers as BT

from ..core import elites, tactics
from ..core.engine import DONE
from . import game

from .guard import guarded_tick


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
            self.session.log.event("crossed", from_map=self.from_map, to_map=game.map_id(), expected=self.leg.get("expect"))
            return S.SUCCESS
        if not self._t0:
            self._t0 = now
        if now - self._t0 > self.timeout_s:
            self.session.log.event("cross_failed", from_map=self.from_map, leg=self.leg, player=list(game.player_xy()))
            return S.FAILURE
        first = self.leg.get("portal") or self.leg["xy"]
        beyond = self.leg.get("beyond") or first
        px, py = game.player_xy()
        if self._stage == 0 and math.hypot(px - first[0], py - first[1]) < 300.0:
            self._stage = 1
        if now - self._last_move >= 1.2:
            self._last_move = now
            if self._stage == 0:
                game.move_to(first[0], first[1])
            else:
                # push on past the arrival point: the trigger can sit a little beyond it
                dx, dy = beyond[0] - first[0], beyond[1] - first[1]
                n = math.hypot(dx, dy) or 1.0
                reach = n + 600.0 + 400.0 * min(6, int((now - self._t0) / 8.0))
                game.move_to(first[0] + dx / n * reach, first[1] + dy / n * reach)
        return S.RUNNING
