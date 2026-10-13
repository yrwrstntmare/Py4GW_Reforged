"""Process-wide cooperative hold on route movement.

A global service that needs the character for a short detour (for example the Blessings system
setting walking to a shrine NPC) acquires the hold. The shared movement primitives honour it:
``Routines.Yield.Movement.FollowPath``, ``BT.Movement.MovePath``, the BottingTree planner and the
HeroAI follower all stand still while it is held. Releasing the hold bumps a generation counter;
movement that was in progress compares generations and re-plans from the character's current
position instead of resuming a path that was computed before the detour.

The hold is a lease: the owner must refresh it with ``Acquire`` at least every ``LEASE_S``
seconds. A lease that lapses (its owner crashed or was unloaded mid-detour) ends on its own, so a
failed owner cannot freeze every script. Scripts that call ``Player.Move`` directly bypass the
hold; it is cooperative, not enforced.
"""

import time
from dataclasses import dataclass


@dataclass
class _HoldState:
    owner: str = ""
    reason: str = ""
    refreshed_at: float = 0.0
    generation: int = 0


_state = _HoldState()


class MovementHold:
    """Static API over the single process-wide movement hold."""

    LEASE_S: float = 2.0

    @staticmethod
    def _expire_lapsed_lease() -> None:
        if _state.owner and time.monotonic() - _state.refreshed_at > MovementHold.LEASE_S:
            _state.owner = ""
            _state.reason = ""
            _state.generation += 1

    @staticmethod
    def Acquire(owner: str, reason: str = "") -> bool:
        """Take or refresh the hold for ``owner``. Returns False if another owner holds it."""

        MovementHold._expire_lapsed_lease()
        if _state.owner and _state.owner != owner:
            return False
        _state.owner = owner
        _state.reason = reason
        _state.refreshed_at = time.monotonic()
        return True

    @staticmethod
    def Release(owner: str) -> bool:
        """End the hold if ``owner`` holds it, signalling a re-plan. Returns True if it was released."""

        MovementHold._expire_lapsed_lease()
        if not _state.owner or _state.owner != owner:
            return False
        _state.owner = ""
        _state.reason = ""
        _state.generation += 1
        return True

    @staticmethod
    def IsHeld() -> bool:
        MovementHold._expire_lapsed_lease()
        return bool(_state.owner)

    @staticmethod
    def IsHeldBy(owner: str) -> bool:
        return MovementHold.IsHeld() and _state.owner == owner

    @staticmethod
    def GetOwner() -> str:
        MovementHold._expire_lapsed_lease()
        return _state.owner

    @staticmethod
    def GetReason() -> str:
        MovementHold._expire_lapsed_lease()
        return _state.reason

    @staticmethod
    def GetGeneration() -> int:
        """Counter bumped each time a hold ends; compare it to detect that a re-plan is due."""

        MovementHold._expire_lapsed_lease()
        return _state.generation
