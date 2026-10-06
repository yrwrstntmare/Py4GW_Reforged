"""Builds Sources/gwamm/data/routes.json from the hand-made vanquish routes that ship with
Reforged (Sources/aC_Scripts/PyQuishAI_maps): for each area, the waypoints in the order their
authors walk them, plus any blessing spots they marked. The bot uses these as a guide to where
the enemies are; its own pathing, fighting and exit rules still apply between the points."""
import ast, glob, json, os, sys
ROOT = "Sources/aC_Scripts/PyQuishAI_maps"
out, skipped = {}, []
for f in sorted(glob.glob(ROOT + "/*/*.py")):
    name, region = os.path.basename(f)[:-3], os.path.basename(os.path.dirname(f))
    try:
        tree = ast.parse(open(f, encoding="utf-8", errors="replace").read())
        vals = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                try:
                    vals[node.targets[0].id] = ast.literal_eval(node.value)
                except Exception:
                    pass
        ids = next((v for k, v in vals.items() if k.endswith("_ids") and isinstance(v, dict)), None)
        main = vals.get(name) or next((v for k, v in vals.items() if isinstance(v, list) and not k.endswith("_outpost_path")), None)
        if not ids or not main or "map_id" not in ids:
            skipped.append(name); continue
        pts, bless = [], []
        def add(p):
            if isinstance(p, (tuple, list)) and len(p) >= 2 and all(isinstance(v, (int, float)) for v in p[:2]):
                pts.append([round(p[0]), round(p[1])])
        for item in main:
            if isinstance(item, dict):
                for k, v in item.items():
                    if k == "bless" and isinstance(v, (tuple, list)):
                        bless.append([round(v[0]), round(v[1])]); add(v)
                    elif k == "path":
                        for q in v: add(q)
            else:
                add(item)
        if len(pts) >= 3:
            out[str(int(ids["map_id"]))] = {"name": name, "region": region, "points": pts, "bless": bless}
    except Exception as e:
        skipped.append(f"{name}: {e!r}")
json.dump(out, open("Sources/gwamm/data/routes.json", "w"))
print(len(out), "routes;", sum(len(v["points"]) for v in out.values()), "points;", sum(1 for v in out.values() if v["bless"]), "with blessing spots; skipped:", skipped[:12])
