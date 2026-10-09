import math
import time
from typing import Callable

import PySystem

from ..Agent import Agent
from ..AgentArray import AgentArray
from ..Map import Map
from ..Player import Player
from ..enums_src.GameData_enums import Range
from ..py4gwcorelib_src.BehaviorTree import BehaviorTree

MODULE_NAME = "ShrineBlessing"

_BLESSING_NPCS: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...] = (
    ((4778, 4776), (1,)),
    ((5384, 5383), (1,)),
    ((5632,), (1,)),
    ((5547, 5548), (1,)),
    ((5615,), (1,)),
    ((5218, 5683), (1,)),
    ((5002,), (1,)),
    ((593, 912, 3426), (1, 2, 1, 1)),
    ((1947, 3641), (1, 2, 1, 1)),
    ((1986, 1987, 6044, 6045, 6043), (1,)),
    ((6755, 6756, 6775, 6779), (1,)),
    ((6374, 6380), (1,)),
)

_BUTTONS_BY_MODEL: dict[int, tuple[int, ...]] = {
    model_id: buttons
    for model_ids, buttons in _BLESSING_NPCS
    for model_id in model_ids
}

# Blessing NPCs answered with a dialog ID instead of a visible button index.
_DIALOG_NPCS: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...] = (
    ((6807,), (0x84,)),  # Purifier Krewe Member, Verdant Cascades
    ((5916,), (0x84,)),  # Beacon of Droknar; corrects the earlier unverified guess of model 5865
)

_DIALOGS_BY_MODEL: dict[int, tuple[int, ...]] = {
    model_id: dialogs
    for model_ids, dialogs in _DIALOG_NPCS
    for model_id in model_ids
}

_MAX_DISTANCE = Range.Compass.value
_TALK_DISTANCE = Range.Nearby.value * 0.8
_RETRY_AFTER_TALK_S = 300.0
_SCAN_INTERVAL_S = 1.0
_MOVE_INTERVAL_S = 0.5
_MOVE_TIMEOUT_S = 30.0
_INTERACT_DELAY_S = 0.8
_BUTTON_INTERVAL_S = 0.6
_DIALOG_TIMEOUT_S = 10.0

# Module-level, not per-build: a planner "Restart From" calls Start() -> Reset(), which rebuilds this
# service from build_shrine_blessing_service(), discarding its closure state. Keeping "who we already
# talked to" here instead means a restart does not forget it and walk back to the same NPC. Keyed by
# (map_id, agent_id) since agent ids are reused across map instances.
_TALKED: dict[tuple[int, int], float] = {}


def _in_danger() -> bool:
    from ..Routines import Checks

    return bool(Checks.Agents.InDanger(aggro_area=Range.Earshot))


def build_shrine_blessing_service(is_enabled: Callable[[], bool]) -> BehaviorTree:
    state: dict[str, object] = {
        "phase": "idle",
        "agent": 0,
        "buttons": (),
        "by_dialog": False,
        "button_index": 0,
        "next_at": 0.0,
        "deadline": 0.0,
        "last_scan": 0.0,
    }

    def _clear_detour(node: BehaviorTree.Node) -> None:
        state["phase"] = "idle"
        state["agent"] = 0
        state["buttons"] = ()
        state["button_index"] = 0
        node.blackboard["SHRINE_BLESSING_ACTIVE"] = False

    def _abort(node: BehaviorTree.Node, reason: str) -> None:
        PySystem.Console.Log(MODULE_NAME, f"Blessing detour aborted: {reason}.", PySystem.Console.MessageType.Warning)
        _clear_detour(node)

    def _finish(node: BehaviorTree.Node) -> None:
        _TALKED[(Map.GetMapID(), int(state["agent"]))] = time.monotonic()
        PySystem.Console.Log(MODULE_NAME, "Blessing collected.", PySystem.Console.MessageType.Success)
        _clear_detour(node)

    def _nearest_blessing_npc() -> tuple[int, tuple[int, ...], bool] | None:
        map_id = Map.GetMapID()
        px, py = Player.GetXY()
        now = time.monotonic()
        best: tuple[int, tuple[int, ...], bool] | None = None
        best_distance = float("inf")
        for agent in AgentArray.GetNPCMinipetArray():
            agent_id = int(agent)
            model_id = int(Agent.GetModelID(agent_id) or 0)
            dialogs = _DIALOGS_BY_MODEL.get(model_id)
            buttons = _BUTTONS_BY_MODEL.get(model_id)
            if dialogs is not None:
                options, by_dialog = dialogs, True
            elif buttons is not None:
                options, by_dialog = buttons, False
            else:
                continue
            if now - _TALKED.get((map_id, agent_id), -1e9) < _RETRY_AFTER_TALK_S:
                continue
            x, y = Agent.GetXY(agent_id)
            distance = math.dist((px, py), (x, y))
            if distance <= _MAX_DISTANCE and distance < best_distance:
                best = (agent_id, options, by_dialog)
                best_distance = distance
        return best

    def _tick(node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        if not is_enabled() or not Map.IsMapReady() or Map.IsMapLoading() or not Map.IsExplorable():
            if state["phase"] != "idle":
                _abort(node, "map not ready")
            return BehaviorTree.NodeState.RUNNING

        now = time.monotonic()
        phase = state["phase"]

        if phase == "idle":
            if bool(node.blackboard.get("COMBAT_ACTIVE", False)) or _in_danger():
                return BehaviorTree.NodeState.RUNNING
            if now - float(state["last_scan"]) < _SCAN_INTERVAL_S:
                return BehaviorTree.NodeState.RUNNING
            state["last_scan"] = now
            target = _nearest_blessing_npc()
            if target is None:
                return BehaviorTree.NodeState.RUNNING
            agent_id, buttons, by_dialog = target
            state["phase"] = "move"
            state["agent"] = agent_id
            state["buttons"] = buttons
            state["by_dialog"] = by_dialog
            state["button_index"] = 0
            state["next_at"] = 0.0
            state["deadline"] = now + _MOVE_TIMEOUT_S
            node.blackboard["SHRINE_BLESSING_ACTIVE"] = True
            PySystem.Console.Log(MODULE_NAME, f"Moving to blessing NPC {agent_id}.", PySystem.Console.MessageType.Info)
            return BehaviorTree.NodeState.RUNNING

        agent_id = int(state["agent"])

        if phase == "move":
            if now > float(state["deadline"]) or not Agent.IsValid(agent_id):
                _abort(node, "NPC unreachable or gone")
                return BehaviorTree.NodeState.RUNNING
            if bool(node.blackboard.get("COMBAT_ACTIVE", False)) or _in_danger():
                _abort(node, "combat started")
                return BehaviorTree.NodeState.RUNNING
            x, y = Agent.GetXY(agent_id)
            px, py = Player.GetXY()
            if math.dist((px, py), (x, y)) > _TALK_DISTANCE:
                if now >= float(state["next_at"]):
                    Player.Move(float(x), float(y))
                    state["next_at"] = now + _MOVE_INTERVAL_S
                return BehaviorTree.NodeState.RUNNING
            Player.ChangeTarget(agent_id)
            Player.Interact(agent_id, False)
            state["phase"] = "dialog"
            state["next_at"] = now + _INTERACT_DELAY_S
            state["deadline"] = now + _DIALOG_TIMEOUT_S
            return BehaviorTree.NodeState.RUNNING

        if phase == "dialog":
            if now > float(state["deadline"]):
                _abort(node, "dialog timed out")
                return BehaviorTree.NodeState.RUNNING
            if now < float(state["next_at"]):
                return BehaviorTree.NodeState.RUNNING
            buttons = state["buttons"]
            assert isinstance(buttons, tuple)
            index = int(state["button_index"])
            if index < len(buttons):
                if state["by_dialog"]:
                    Player.SendDialog(int(buttons[index]))
                else:
                    Player.SendAutomaticDialog(int(buttons[index]))
                state["button_index"] = index + 1
                state["next_at"] = now + _BUTTON_INTERVAL_S
                return BehaviorTree.NodeState.RUNNING
            _finish(node)
            return BehaviorTree.NodeState.RUNNING

        return BehaviorTree.NodeState.RUNNING

    return BehaviorTree(
        BehaviorTree.ActionNode(
            name="ShrineBlessingService",
            action_fn=_tick,
            aftercast_ms=0,
        )
    )
