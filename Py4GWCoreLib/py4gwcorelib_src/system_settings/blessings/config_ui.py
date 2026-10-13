"""System Settings UI for automatic blessings."""

import PyImGui

from . import model
from .controller import BlessingsController
from .controller import get_controller


_MUTED = (0.60, 0.60, 0.65, 1.0)
_COMBAT_LABELS = ["Pause the detour, then carry on", "Give up on the blessing"]
_OPPOSING_PRIEST_LABELS = ["Bribe (I'll make it worth your while)", "Fight (I've had enough!)"]
_PRIORITY_LABELS = ["Blessing first (pause the running script)", "Other movement first (only when standing still)"]


def _draw_options(controller: BlessingsController) -> None:
    config = controller.config
    enabled = PyImGui.checkbox("Enable Blessings##blessings_enabled", config.enabled)
    if enabled != config.enabled:
        controller.set_enabled(enabled)

    PyImGui.text_wrapped(
        "Walks to an enabled blessing NPC within range when you have no blessing, takes the "
        "blessing, then lets the running script re-plan its route from where you stand. Scripts "
        "that move with Player.Move directly are not paused and may fight the detour."
    )
    PyImGui.text_colored("Status: %s" % controller.status, _MUTED)
    PyImGui.separator()

    detour_range = PyImGui.slider_int(
        "Detour range##blessings_range",
        int(config.detour_range),
        model.DETOUR_RANGE_MIN,
        model.DETOUR_RANGE_MAX,
    )
    if detour_range != config.detour_range:
        config.detour_range = detour_range
    if PyImGui.is_item_deactivated_after_edit():
        controller.save()

    combat_index = PyImGui.combo("During combat##blessings_combat", int(config.combat_mode), _COMBAT_LABELS)
    if combat_index != int(config.combat_mode):
        config.combat_mode = model.CombatMode(combat_index)
        controller.save()

    priority_index = PyImGui.combo("Priority##blessings_priority", int(config.priority), _PRIORITY_LABELS)
    if priority_index != int(config.priority):
        config.priority = model.Priority(priority_index)
        controller.save()


def _draw_campaign(controller: BlessingsController, campaign: model.Campaign) -> None:
    npcs = [npc for npc in model.BLESSING_NPCS if npc.campaign == campaign]
    regions = [region for region in model.BLESSING_REGIONS if region.campaign == campaign]
    enabled_count = sum(1 for npc in npcs if controller.config.is_npc_enabled(npc))
    header = "%s (%d/%d NPCs)###blessings_campaign_%d" % (
        model.CAMPAIGN_TITLES[campaign],
        enabled_count,
        len(npcs),
        int(campaign),
    )
    if not PyImGui.collapsing_header(header):
        return

    PyImGui.text_colored("NPCs", _MUTED)
    for npc in npcs:
        current = controller.config.is_npc_enabled(npc)
        value = PyImGui.checkbox("%s##blessings_npc_%s" % (npc.name, npc.key), current)
        if value != current:
            controller.set_npc_enabled(npc, value)
        if npc.note:
            PyImGui.same_line(0, 8)
            PyImGui.text_colored(npc.note, _MUTED)

    if campaign == model.Campaign.FACTIONS:
        config = controller.config
        choice = PyImGui.combo(
            "Opposite-faction priest##blessings_opposing_priest",
            int(config.opposing_priest),
            _OPPOSING_PRIEST_LABELS,
        )
        if choice != int(config.opposing_priest):
            config.opposing_priest = model.OpposingPriestChoice(choice)
            controller.save()
        PyImGui.text_colored(
            "A priest counts as the opposite faction when your points with the other side are equal or "
            "higher. Your own faction's priest always just blesses.",
            _MUTED,
        )

    PyImGui.text_colored("Maps with a recorded blessing", _MUTED)
    for region in regions:
        PyImGui.text("%s:" % region.name)
        for _map_id, map_name in region.maps:
            PyImGui.bullet_text(map_name)


def _draw_blessings(controller: BlessingsController) -> None:
    _draw_options(controller)
    PyImGui.separator()
    for campaign in model.Campaign:
        _draw_campaign(controller, campaign)


def add_sections(win, group) -> None:
    """Add the Blessings section to Map & Missions."""

    controller = get_controller()
    win.add_account_section(
        group,
        "map.blessings",
        "Blessings",
        lambda c=controller: _draw_blessings(c),
    )
