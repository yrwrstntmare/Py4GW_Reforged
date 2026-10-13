"""Profiled Update callback that detours to nearby blessing NPCs.

Runs on every account, in every script. While it walks to and talks with a giver it holds the
process-wide ``MovementHold``, which the shared movement primitives honour; releasing the hold
makes them re-plan from wherever the detour left the character.
"""

import math
import time
from enum import Enum
from typing import Optional

from Py4GWCoreLib.Agent import Agent
from Py4GWCoreLib.AgentArray import AgentArray
from Py4GWCoreLib.Effect import Effects
from Py4GWCoreLib.Map import Map
from Py4GWCoreLib.Player import Player
from Py4GWCoreLib.UIManager import UIManager
from Py4GWCoreLib.enums_src.GameData_enums import Range
from Py4GWCoreLib.py4gwcorelib_src.MovementHold import MovementHold
from Py4GWCoreLib.routines_src.Checks import Checks

from . import model
from . import persistence


_CALLBACK_NAME = "Blessings"
_HOLD_OWNER = "Blessings"

_SCAN_INTERVAL_S = 1.0
_MOVE_INTERVAL_S = 0.5
_TALK_DISTANCE = Range.Nearby.value * 0.8
_DETOUR_TIMEOUT_S = 90.0
_DIALOG_WAIT_S = 4.0
_AFTER_DIALOG_S = 0.6
_VERIFY_S = 3.0
_CHALLENGE_START_S = 8.0
_CHALLENGE_TIMEOUT_S = 120.0
_IDLE_BEFORE_DETOUR_S = 1.5
_HOSTILE_ALLEGIANCE = 3


class _Phase(Enum):
    IDLE = "idle"
    APPROACH = "approach"
    TALK = "talk"
    CHALLENGE = "challenge"
    FIGHT = "fight"
    VERIFY = "verify"


def _log(message: str, error: bool = False) -> None:
    try:
        import PySystem

        message_type = PySystem.Console.MessageType.Error if error else PySystem.Console.MessageType.Info
        PySystem.Console.Log(_CALLBACK_NAME, message, message_type)
    except Exception:
        pass


def _agent_name(agent_id: int) -> str:
    # The native name lookup crashes the client for an agent that no longer exists.
    try:
        if not agent_id or not Agent.IsValid(agent_id):
            return ""
        return str(Agent.GetNameByID(agent_id) or "")
    except Exception:
        return ""


class BlessingsController:
    """Find, walk to and talk with blessing NPCs, holding route movement meanwhile."""

    def __init__(self) -> None:
        self.config = persistence.load()
        self.status = "Idle"
        self._registered = False
        self._settings_account_email = ""
        self._map_id = 0
        self._done_agents: set[int] = set()
        self._npc_by_agent: dict[int, Optional[model.BlessingNpc]] = {}
        self._last_scan = 0.0
        self._last_moving_at = 0.0
        self._reset_detour()

    # ---- configuration -------------------------------------------------------------------------

    def save(self) -> None:
        persistence.save(self.config)

    def set_enabled(self, enabled: bool) -> None:
        self.config.enabled = bool(enabled)
        self.save()
        if not enabled:
            self._abort("disabled")

    def set_npc_enabled(self, npc: model.BlessingNpc, enabled: bool) -> None:
        self.config.npc_enabled[npc.key] = bool(enabled)
        self.save()

    def reload_account_settings(self) -> bool:
        self.config = persistence.load()
        return True

    # ---- lifecycle -----------------------------------------------------------------------------

    def register(self) -> None:
        """Register one profiled Update callback, idempotently across widget reloads."""

        try:
            import PyCallback

            from Py4GWCoreLib.py4gwcorelib_src.Profiling import ProfilingRegistry

            PyCallback.PyCallback.RemoveByName(_CALLBACK_NAME)
            PyCallback.PyCallback.Register(
                _CALLBACK_NAME,
                PyCallback.Phase.Update,
                self._callback,
                priority=99,
                context=PyCallback.Context.Main,
            )
            ProfilingRegistry().register(_CALLBACK_NAME)
            self._registered = True
        except Exception as exc:
            _log("callback registration error: %s" % exc, error=True)

    def unregister(self) -> None:
        try:
            import PyCallback

            PyCallback.PyCallback.RemoveByName(_CALLBACK_NAME)
        except Exception:
            pass
        self._registered = False
        self._abort("unregistered")

    def _callback(self) -> None:
        try:
            from Py4GWCoreLib.py4gwcorelib_src.Profiling import ProfilingRegistry

            registry = ProfilingRegistry()
            if registry.enabled:
                registry.runcall_scope("widgets", "%s:main" % _CALLBACK_NAME, self._tick)
                return
        except Exception:
            pass
        self._tick()

    def _refresh_local_config_after_bind(self) -> bool:
        try:
            account_email = str(Player.GetAccountEmail() or "").strip()
        except Exception:
            return False
        if not account_email:
            return False
        if account_email == self._settings_account_email:
            return True
        if not persistence.local_is_ready():
            return False
        self.config = persistence.load()
        self._settings_account_email = account_email
        return True

    # ---- detour state --------------------------------------------------------------------------

    def _reset_detour(self) -> None:
        self._phase = _Phase.IDLE
        self._agent_id = 0
        self._npc: Optional[model.BlessingNpc] = None
        # (dialog id, talk to the NPC first); False continues on the dialog page already open.
        self._dialogs: list[tuple[int, bool]] = []
        self._dialog_index = 0
        self._phase_started = 0.0
        self._detour_deadline = 0.0
        self._next_action_at = 0.0
        self._interacted = False
        self._challenge_seen_hostile = False

    def _finish(self, result: str) -> None:
        if self._agent_id:
            self._done_agents.add(self._agent_id)
        name = self._npc.name if self._npc is not None else "blessing NPC"
        self.status = "%s: %s" % (name, result)
        _log(self.status)
        MovementHold.Release(_HOLD_OWNER)
        self._reset_detour()

    def _abort(self, reason: str) -> None:
        if self._phase == _Phase.IDLE:
            return
        self._finish("aborted (%s)" % reason)

    def _enter(self, phase: _Phase, now: float) -> None:
        self._phase = phase
        self._phase_started = now
        self._next_action_at = now
        self._interacted = False

    # ---- checks --------------------------------------------------------------------------------

    @staticmethod
    def _has_blessing() -> bool:
        player_id = Player.GetAgentID()
        return any(Effects.EffectExists(player_id, skill_id) for skill_id in model.BLESSING_EFFECT_IDS)

    @staticmethod
    def _in_danger() -> bool:
        return bool(Checks.Agents.InDanger(aggro_area=Range.Earshot))

    def _npc_for_agent(self, agent_id: int) -> Optional[model.BlessingNpc]:
        if agent_id in self._npc_by_agent:
            return self._npc_by_agent[agent_id]
        name = _agent_name(agent_id).lower()
        if name:
            npc = next(
                (entry for entry in model.BLESSING_NPCS if any(frag in name for frag in entry.name_fragments)),
                None,
            )
            self._npc_by_agent[agent_id] = npc
            return npc
        # Name not loaded yet: fall back to the model id, but do not cache, so the name wins later.
        model_id = int(Agent.GetModelID(agent_id) or 0)
        return next((entry for entry in model.BLESSING_NPCS if model_id in entry.model_ids), None)

    def _nearest_giver(self) -> Optional[tuple[int, model.BlessingNpc]]:
        px, py = Player.GetXY()
        best: Optional[tuple[int, model.BlessingNpc]] = None
        best_distance = float(self.config.detour_range)
        for agent_id in AgentArray.GetNPCMinipetArray():
            agent_id = int(agent_id)
            if agent_id in self._done_agents or not Agent.IsValid(agent_id) or not Agent.IsAlive(agent_id):
                continue
            npc = self._npc_for_agent(agent_id)
            if npc is None or not self.config.is_npc_enabled(npc):
                continue
            x, y = Agent.GetXY(agent_id)
            distance = math.dist((px, py), (x, y))
            if distance <= best_distance:
                best = (agent_id, npc)
                best_distance = distance
        return best

    def _dialog_plan(self, npc: model.BlessingNpc) -> list[tuple[int, bool]]:
        if npc.flow != model.BlessingFlow.FACTION_PRIEST:
            return [(npc.dialog_id, True)]
        kurzick = int(Player.GetKurzickData()[0] or 0)
        luxon = int(Player.GetLuxonData()[0] or 0)
        opposite_faction = kurzick >= luxon if npc.faction == "luxon" else luxon >= kurzick
        if not opposite_faction:
            return [(npc.dialog_id, True)]
        if self.config.opposing_priest == model.OpposingPriestChoice.FIGHT:
            return [(model.DIALOG_PRIEST_TALK_IT_OVER, True), (model.DIALOG_PRIEST_FIGHT, False)]
        # Bribe, then ask for the blessing; the talk tick stops early if the bribe already blessed.
        return [
            (model.DIALOG_PRIEST_TALK_IT_OVER, True),
            (model.DIALOG_PRIEST_BRIBE, False),
            (npc.dialog_id, True),
        ]

    # ---- per-frame tick ------------------------------------------------------------------------

    def _tick(self) -> None:
        try:
            self._tick_unsafe()
        except Exception as exc:
            _log("tick error: %s" % exc, error=True)
            self._abort("error")

    def _tick_unsafe(self) -> None:
        if not self._refresh_local_config_after_bind():
            return
        if not self.config.enabled:
            self._abort("disabled")
            return
        if not Checks.Map.MapValid() or Map.IsMapLoading() or not Map.IsExplorable():
            self._abort("left the map")
            self._map_id = 0
            return

        map_id = int(Map.GetMapID() or 0)
        if map_id != self._map_id:
            # Agent ids are reused across map instances.
            self._map_id = map_id
            self._done_agents.clear()
            self._npc_by_agent.clear()

        player_id = Player.GetAgentID()
        if Agent.IsDead(player_id):
            self._abort("player dead")
            return

        now = time.monotonic()
        if Agent.IsMoving(player_id):
            self._last_moving_at = now

        if self._phase == _Phase.IDLE:
            self._tick_idle(now)
            return

        # Keep the lease alive while the detour owns the character.
        MovementHold.Acquire(_HOLD_OWNER, "Blessing: %s" % (self._npc.name if self._npc else ""))
        if now > self._detour_deadline:
            self._abort("took too long")
            return
        if self._phase == _Phase.FIGHT:
            self._tick_fight(now)
            return
        if not Agent.IsValid(self._agent_id):
            self._abort("NPC gone")
            return

        if self._phase == _Phase.APPROACH:
            self._tick_approach(now)
        elif self._phase == _Phase.TALK:
            self._tick_talk(now)
        elif self._phase == _Phase.CHALLENGE:
            self._tick_challenge(now)
        elif self._phase == _Phase.VERIFY:
            self._tick_verify(now)

    def _tick_idle(self, now: float) -> None:
        if now - self._last_scan < _SCAN_INTERVAL_S:
            return
        self._last_scan = now
        if self._has_blessing():
            self.status = "Already blessed"
            return
        if self._in_danger():
            self.status = "Waiting: enemies nearby"
            return
        if MovementHold.IsHeld():
            self.status = "Waiting: %s has movement" % MovementHold.GetOwner()
            return
        if self.config.priority == model.Priority.MOVEMENT_FIRST and now - self._last_moving_at < _IDLE_BEFORE_DETOUR_S:
            self.status = "Waiting: other movement first"
            return

        target = self._nearest_giver()
        if target is None:
            self.status = "Idle"
            return
        agent_id, npc = target
        if not MovementHold.Acquire(_HOLD_OWNER, "Blessing: %s" % npc.name):
            return
        self._agent_id = agent_id
        self._npc = npc
        self._dialogs = self._dialog_plan(npc)
        self._dialog_index = 0
        self._detour_deadline = now + _DETOUR_TIMEOUT_S
        self._enter(_Phase.APPROACH, now)
        self.status = "Going to %s" % npc.name
        _log("Detouring to %s (agent %d)." % (npc.name, agent_id))

    def _tick_approach(self, now: float) -> None:
        if self._in_danger():
            if self.config.combat_mode == model.CombatMode.ABORT:
                self._abort("combat")
                return
            # Pause: keep the hold so the route stays put while HeroAI fights, then carry on.
            self.status = "Paused for combat (%s)" % (self._npc.name if self._npc else "")
            self._detour_deadline = max(self._detour_deadline, now + 30.0)
            return
        x, y = Agent.GetXY(self._agent_id)
        if math.dist(Player.GetXY(), (x, y)) > _TALK_DISTANCE:
            if now >= self._next_action_at:
                Player.Move(float(x), float(y))
                self._next_action_at = now + _MOVE_INTERVAL_S
            return
        self._enter(_Phase.TALK, now)

    def _tick_talk(self, now: float) -> None:
        if now < self._next_action_at:
            return
        if self._dialog_index > 0 and self._has_blessing():
            self._finish("blessed")
            return
        dialog_id, talk_first = self._dialogs[self._dialog_index]
        if not self._interacted:
            if talk_first:
                Player.ChangeTarget(self._agent_id)
                Player.Interact(self._agent_id, False)
            self._interacted = True
            self._phase_started = now
            return
        if not UIManager.IsNPCDialogVisible() and now - self._phase_started < _DIALOG_WAIT_S:
            return
        Player.SendDialog(int(dialog_id))
        self._dialog_index += 1
        self._interacted = False
        self._next_action_at = now + _AFTER_DIALOG_S
        if self._dialog_index < len(self._dialogs):
            return
        npc = self._npc
        if npc is not None and npc.flow == model.BlessingFlow.FACTION_PRIEST and dialog_id == model.DIALOG_PRIEST_FIGHT:
            self._enter(_Phase.FIGHT, now)
            self.status = "Fighting %s" % npc.name
            return
        if npc is not None and npc.flow == model.BlessingFlow.NORN_CHALLENGE and not self._challenge_seen_hostile:
            self._enter(_Phase.CHALLENGE, now)
            self.status = "%s challenge" % npc.name
            return
        self._enter(_Phase.VERIFY, now)

    def _tick_challenge(self, now: float) -> None:
        hostile = int(Agent.GetAllegiance(self._agent_id)[0]) == _HOSTILE_ALLEGIANCE
        if hostile:
            self._challenge_seen_hostile = True
            self._detour_deadline = max(self._detour_deadline, now + _CHALLENGE_TIMEOUT_S)
            return
        if not self._challenge_seen_hostile:
            if self._has_blessing():
                self._finish("blessed")
            elif now - self._phase_started > _CHALLENGE_START_S:
                self._finish("challenge never started")
            return
        # Hunter is friendly again after the fight: walk back and talk once more.
        self._dialog_index = 0
        self._enter(_Phase.APPROACH, now)

    def _tick_fight(self, now: float) -> None:
        # The priest turns hostile; HeroAI fights it while the hold keeps the route in place.
        if not Agent.IsValid(self._agent_id) or Agent.IsDead(self._agent_id):
            self._finish("fought")
            return
        if int(Agent.GetAllegiance(self._agent_id)[0]) == _HOSTILE_ALLEGIANCE:
            self._challenge_seen_hostile = True
            self._detour_deadline = max(self._detour_deadline, now + _CHALLENGE_TIMEOUT_S)
            return
        if self._challenge_seen_hostile:
            self._finish("fought")
        elif now - self._phase_started > _CHALLENGE_START_S:
            self._finish("fight never started")

    def _tick_verify(self, now: float) -> None:
        if self._has_blessing():
            self._finish("blessed")
        elif now - self._phase_started > _VERIFY_S:
            self._finish("no blessing effect seen")


_controller: Optional[BlessingsController] = None


def get_controller() -> BlessingsController:
    """Return the process-wide Blessings controller."""

    global _controller
    if _controller is None:
        _controller = BlessingsController()
    return _controller
