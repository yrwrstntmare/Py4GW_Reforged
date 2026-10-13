"""Settings-backed persistence for the Blessings system setting."""

from typing import Any

from . import model


DOCUMENT = "Widgets/System/Blessings.ini"
OPTIONS_SECTION = "Blessings"
NPC_SECTION = "Blessing NPCs"


def _settings():
    try:
        from Py4GWCoreLib.py4gwcorelib_src.Settings import Settings

        return Settings(DOCUMENT, "account")
    except Exception:
        return None


def local_is_ready() -> bool:
    settings = _settings()
    if settings is None:
        return False
    try:
        return settings.is_ready()
    except Exception:
        return False


def _enum_value(enum_type: Any, raw: int, default: Any) -> Any:
    try:
        return enum_type(int(raw))
    except ValueError:
        return default


def load() -> model.BlessingsConfig:
    config = model.BlessingsConfig()
    settings = _settings()
    if settings is None:
        return config
    config.enabled = settings.get_bool(OPTIONS_SECTION, "enabled", config.enabled)
    detour_range = settings.get_int(OPTIONS_SECTION, "detour_range", config.detour_range)
    config.detour_range = max(model.DETOUR_RANGE_MIN, min(model.DETOUR_RANGE_MAX, detour_range))
    config.combat_mode = _enum_value(
        model.CombatMode,
        settings.get_int(OPTIONS_SECTION, "combat_mode", int(config.combat_mode)),
        config.combat_mode,
    )
    config.priority = _enum_value(
        model.Priority,
        settings.get_int(OPTIONS_SECTION, "priority", int(config.priority)),
        config.priority,
    )
    config.opposing_priest = _enum_value(
        model.OpposingPriestChoice,
        settings.get_int(OPTIONS_SECTION, "opposing_priest", int(config.opposing_priest)),
        config.opposing_priest,
    )
    config.npc_enabled = {
        npc.key: settings.get_bool(NPC_SECTION, npc.key, npc.default_enabled)
        for npc in model.BLESSING_NPCS
    }
    return config


def options_values(config: model.BlessingsConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "detour_range": int(config.detour_range),
        "combat_mode": int(config.combat_mode),
        "priority": int(config.priority),
        "opposing_priest": int(config.opposing_priest),
    }


def npc_values(config: model.BlessingsConfig) -> dict[str, Any]:
    return {npc.key: config.is_npc_enabled(npc) for npc in model.BLESSING_NPCS}


def save(config: model.BlessingsConfig) -> None:
    settings = _settings()
    if settings is None:
        return
    for key, value in options_values(config).items():
        settings.set(OPTIONS_SECTION, key, value)
    for key, value in npc_values(config).items():
        settings.set(NPC_SECTION, key, value)
