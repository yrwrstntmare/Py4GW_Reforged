"""Areas entered by talking to an NPC in an outpost instead of walking through a gate
(Zen Daijun: Guard Lae Fao in Seitung Harbor; Bahdok Caverns). The data is Reforged's own,
from the PyQuishAI vanquish maps (Sources/aC_Scripts/PyQuishAI_maps): read, never copied.

Two forms: travel to the outpost and talk to an NPC there; or walk out of the outpost into a
neighbouring area ("transit_id") and talk to an NPC standing in it.
"""
import ast
import os

_CACHE = None


def _maps_dir():
    import PySystem
    return os.path.join(PySystem.Console.get_projects_path(), "Sources", "aC_Scripts", "PyQuishAI_maps")


def parse_file(path):
    """{map id: {"outpost": id, "npc": (x, y), "dialogs": [ids]}} from one PyQuishAI map file."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    vals = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                vals[node.targets[0].id] = ast.literal_eval(node.value)
            except Exception:
                continue
    out = {}
    for name, steps in vals.items():
        if not name.endswith("_transit_path") or not isinstance(steps, list):
            continue
        base = name[: -len("_transit_path")]
        ids, route = vals.get(base + "_ids") or {}, vals.get(base) or []
        if not isinstance(ids, dict) or "outpost_id" not in ids:
            continue
        area = next((s.get("map") for s in route if isinstance(s, dict) and s.get("map")), None) or ids.get("map_id")
        npc = next((s.get("npc") for s in steps if isinstance(s, dict) and s.get("npc")), None)
        dialogs = [int(s["dialog"]) for s in steps if isinstance(s, dict) and "dialog" in s]
        if not (area and npc and dialogs):
            continue
        entry = {"outpost": int(ids["outpost_id"]), "npc": tuple(npc), "dialogs": dialogs}
        if "transit_id" in ids:
            # the NPC stands in another explorable area: walk out of the outpost into it first
            # (Garden of Seborhin: out of Honur Hill into Forum Highlands, then its NPC)
            path = vals.get(base + "_outpost_path")
            if not isinstance(path, list) or not path:
                continue
            entry.update(via=int(ids["transit_id"]), via_path=[tuple(p) for p in path])
        out[int(area)] = entry
    return out


def parse_dialog_farms(path):
    """Areas entered through an NPC, from Reforged's Nicholas the Traveler farm definitions
    (flow 'dialog': outpost, farm map, the NPC's position and its dialog)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "FarmDefinition"):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        try:
            if ast.literal_eval(kw["flow"]) != "dialog":
                continue
            out[int(ast.literal_eval(kw["farm_map_id"]))] = {
                "outpost": int(ast.literal_eval(kw["outpost_map_id"])),
                "npc": tuple(ast.literal_eval(kw["entry_position"])),
                "dialogs": [int(ast.literal_eval(kw["entry_dialog"]))]}
        except Exception:
            continue
    return out


# Entries the PyQuishAI maps do not have, taken from Reforged's other bots. The Hidden City of
# Ahdashim: its gate in Dasha Vestibule only opens through the Key of Ahdashim (Reforged's
# Elite Skills Capture BT: 0x81 then 0x84; its Nicholas farm uses the same NPC with 0x84).
EXTRA = {
    413: {"outpost": 434, "npc": (1341.0, -20346.0), "dialogs": [0x81, 0x84]},
}


def entries():
    global _CACHE
    if _CACHE is None:
        _CACHE = {}
        try:                                 # other Reforged bots first; the PyQuishAI maps and EXTRA win
            import PySystem
            farms = os.path.join(PySystem.Console.get_projects_path(), "Widgets", "Automation", "Bots", "Farmers",
                                 "Trophies", "Nicholas the Traveler BT", "_modules", "NicholasFarms.py")
            _CACHE.update(parse_dialog_farms(farms))
        except Exception:
            pass
        root = _maps_dir()
        try:
            for folder, _dirs, files in os.walk(root):
                for fn in files:
                    if fn.endswith(".py"):
                        try:
                            _CACHE.update(parse_file(os.path.join(folder, fn)))
                        except Exception:
                            continue
        except Exception:
            pass
        _CACHE.update({k: dict(v) for k, v in EXTRA.items()})
    return _CACHE
