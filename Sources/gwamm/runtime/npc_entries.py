"""Areas entered by talking to an NPC in an outpost instead of walking through a gate
(Zen Daijun: Guard Lae Fao in Seitung Harbor; Bahdok Caverns). The data is Reforged's own,
from the PyQuishAI vanquish maps (Sources/aC_Scripts/PyQuishAI_maps): read, never copied.

Only the simple form is used: travel to the outpost, then one NPC and its dialog chain. Files
whose entry first crosses another explorable area ("transit_id") are left to the normal routes.
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
        if not isinstance(ids, dict) or "transit_id" in ids or "outpost_id" not in ids:
            continue
        area = next((s.get("map") for s in route if isinstance(s, dict) and s.get("map")), None) or ids.get("map_id")
        npc = next((s.get("npc") for s in steps if isinstance(s, dict) and s.get("npc")), None)
        dialogs = [int(s["dialog"]) for s in steps if isinstance(s, dict) and "dialog" in s]
        if area and npc and dialogs:
            out[int(area)] = {"outpost": int(ids["outpost_id"]), "npc": tuple(npc), "dialogs": dialogs}
    return out


def entries():
    global _CACHE
    if _CACHE is None:
        _CACHE = {}
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
    return _CACHE
