"""Things learnt per area and kept between runs in gwamm_logs/: wurm ground (ride_ground.json,
core/mounts.py RideGround holds it in a visit), hand-set route spots (route_spots.json) and
teleporters (teleporters.json)."""
import json
import os

import PySystem


def _path():
    return os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "ride_ground.json")


def load(map_id):
    """(cells, [(cell, planes)]) known for this area; empty when none."""
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f).get(str(int(map_id))) or {}
        return [tuple(c) for c in d.get("cells", [])], [(tuple(c), p) for c, p in d.get("levels", [])]
    except Exception:
        return [], []


def save(map_id, ground):
    """Merge this visit's ground into the file. Returns the number of cells kept, or None."""
    try:
        try:
            with open(_path(), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        old = data.get(str(int(map_id))) or {}
        cells, levels = ground.merged_with(old.get("cells", []), old.get("levels", []))
        data[str(int(map_id))] = {"cells": [list(c) for c in cells], "levels": [[list(c), p] for c, p in levels]}
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
        return len(cells)
    except Exception:
        return None


def _spots_path():
    return os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "route_spots.json")


def route_spot(map_id, at):
    """Where a route's skill step is done, as set by hand in the window (Developer tab: stand
    there and press the button), or None. Overrides the route file's guess."""
    try:
        with open(_spots_path(), encoding="utf-8") as f:
            v = json.load(f).get(f"{int(map_id)}:{int(at)}")
        return (float(v[0]), float(v[1])) if v else None
    except Exception:
        return None


def set_route_spot(map_id, at, xy):
    try:
        try:
            with open(_spots_path(), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[f"{int(map_id)}:{int(at)}"] = [round(xy[0]), round(xy[1])]
        with open(_spots_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
        return True
    except Exception:
        return False


def _tp_path():
    return os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "teleporters.json")


def _tp_data():
    try:
        with open(_tp_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# Where the pad of a teleporter is, seen standing on it (the pad is no object the game lists:
# only its four switches are). Shipped so anyone's bot steps on the right spot.
KNOWN_PADS = {
    113: [(-5053, 6002)],        # Prophet's Path: switches 751-754 round it
}


def pad_hints(map_id):
    """Spots known to be teleporter pads in this area: shipped ones and those learnt."""
    out = list(KNOWN_PADS.get(int(map_id), []))
    out += [tuple(r["at"]) for r in teleporters(map_id) if r.get("pad")]
    return out


def teleporters(map_id):
    """[{"at": [x, y], "to": [x, y], "gadget": id}] learnt in this area: walking up to the
    object at `at` and using it put the party at `to`."""
    return list(_tp_data().get(str(int(map_id)), []))


def teleporter_gadget_ids():
    """Gadget ids seen to teleport, in any area: likely teleporters wherever they stand."""
    ids = set()
    for rows in _tp_data().values():
        if isinstance(rows, list):
            ids.update(int(r.get("gadget", 0)) for r in rows if r.get("gadget"))
    return ids


def add_teleporter(map_id, at, to, gadget, pad=False):
    try:
        data = _tp_data()
        rows = data.setdefault(str(int(map_id)), [])
        for r in rows:
            if abs(r["at"][0] - at[0]) < 300 and abs(r["at"][1] - at[1]) < 300 \
                    and abs(r["to"][0] - to[0]) < 600 and abs(r["to"][1] - to[1]) < 600:
                return False
        rows.append({"at": [round(at[0]), round(at[1])], "to": [round(to[0]), round(to[1])], "gadget": int(gadget or 0),
                     "pad": bool(pad)})
        with open(_tp_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
        return True
    except Exception:
        return False


def unreachable(map_id):
    """Spots the game's pathing refused at once (no way on foot from below): [(x, y)]. Kept in
    teleporters.json under "<map>_cut", so the next visit goes for the teleporter straight away."""
    shipped = KNOWN_CUT.get(int(map_id), [])
    return list(shipped) + [tuple(p) for p in _tp_data().get(str(int(map_id)) + "_cut", [])]


# Ground the game's pathing refuses from below, seen in runs (shipped like KNOWN_PADS).
KNOWN_CUT = {
    113: [(-16238, 11104), (-17037, 13891), (-17806, 10456)],   # Prophet's Path NW shelf
}


def add_unreachable(map_id, xy):
    try:
        data = _tp_data()
        rows = data.setdefault(str(int(map_id)) + "_cut", [])
        if any(abs(r[0] - xy[0]) < 400 and abs(r[1] - xy[1]) < 400 for r in rows):
            return False
        rows.append([round(xy[0]), round(xy[1])])
        del rows[:-40]
        with open(_tp_path(), "w", encoding="utf-8") as f:
            json.dump(data, f)
        return True
    except Exception:
        return False
