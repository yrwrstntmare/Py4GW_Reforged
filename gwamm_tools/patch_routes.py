"""Hand additions to Sources/gwamm/data/routes.json that the PyQuish map files do not carry.
Run from the repository root after build_routes.py; safe to run twice (each patch is named in
the route's "patched" list and applied once).

444 The Sulfurous Wastes, "pillar": the last stretch of the route crosses to the north plateau
over a bridge that is only there once a pillar has been knocked over with a wurm's Junundu
Smash (Guild Wars Wiki, The Sulfurous Wastes). The route has no step for it and reaches the
bridge on foot. Added after the point at (-13595, 8507), while still riding: along the narrow
east-west strip of sulfur to its dead end under the bridge, Smash there, and back. Seen
2026-10-09: party stopped on foot at (-11295, 10174), 12 foes left on the plateau. The spot is
from the area's ground data (the strip is ground piece 592); NOT yet seen working in game.
"""
import json

PATH = "Sources/gwamm/data/routes.json"
JUNUNDU_SMASH, JUNUNDU_SIEGE = 1440, 1441


def insert_points(route, after_xy, points):
    """Put `points` after the route point at after_xy; actions tied to later points move along.
    Returns the index of the first new point."""
    i = next(k for k, p in enumerate(route["points"]) if tuple(p) == tuple(after_xy))
    route["points"][i + 1:i + 1] = [list(p) for p in points]
    for a in route.get("actions", []):
        if a["at"] > i:
            a["at"] += len(points)
    return i + 1


def main():
    data = json.load(open(PATH))
    r = data["444"]
    if "pillar" not in r.setdefault("patched", []):
        strip_west, under_bridge = (-13264, 10065), (-11600, 10330)
        first = insert_points(r, (-13595, 8507), [strip_west, under_bridge, strip_west])
        r.setdefault("actions", []).append({
            "at": first + 2,                 # due once the spot under the bridge has been reached
            "do": "skill", "value": JUNUNDU_SMASH, "pos": list(under_bridge), "riding": True,
            "save_from": first - 2})         # keep HeroAI off Smash from two points before
        r["actions"].sort(key=lambda a: a["at"])
        r["patched"].append("pillar")
    if "pillar-charge" not in r["patched"]:
        # Smash takes adrenaline. If it is not charged at the spot, build it there with Siege on
        # the enemies up on the plateau (the wiki's own way), then Smash.
        for a in r["actions"]:
            if a.get("do") == "skill" and a.get("value") == JUNUNDU_SMASH:
                a["charge_with"], a["charge_reach"] = JUNUNDU_SIEGE, 2800
        r["patched"].append("pillar-charge")
    if "pillar-attack" not in r["patched"]:
        # Junundu skills (Siege among them) build no adrenaline; weapon hits and damage taken
        # do, and it drains after 25 s out of a fight. Charge with plain attacks on the nearest
        # enemy instead, and keep HeroAI off Smash from four route points before, not two.
        for a in r["actions"]:
            if a.get("do") == "skill" and a.get("value") == JUNUNDU_SMASH:
                a["charge_with"], a["charge_reach"] = "attack", 3000
                a["save_from"] = a["at"] - 6
        r["patched"].append("pillar-attack")
    json.dump(data, open(PATH, "w"))
    print("444:", len(r["points"]), "points;", r["actions"])


if __name__ == "__main__":
    main()
