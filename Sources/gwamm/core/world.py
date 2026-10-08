"""The world as a graph of walkable pieces joined by doors. Pure Python.

Built offline (tools_build_world.py) from an in-game export: every door and arrival point
was placed on the world map, and the two sides of a real connection land on the same spot.
Each node is one walled-off piece of a map file; `map` is the area it holds when known.
"""
import heapq
import json
import math
import os

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "world.json")
STUB_SIZE = 5                # nodes this small are portal stubs, not ground to walk
CROSSING_COST = 4000.0        # loading screens are not free: prefer routes with fewer areas


class World:
    def __init__(self, path=_PATH):
        with open(path, encoding="utf-8") as f:
            w = json.load(f)
        self.nodes = w["nodes"]
        self.area_node = {int(k): v for k, v in w["area_node"].items()}
        self.outpost_node = {int(k): v for k, v in w["outpost_node"].items()}
        self.gates = {int(k): v for k, v in w["gates"].items()}
        self.outpost_xy = {int(k): v for k, v in w.get("outpost_xy", {}).items()}
        self.names = {int(k): v for k, v in w["names"].items()}
        self.vanquishable = set(w["explorable"])

    def name(self, map_id):
        return self.names.get(map_id, f"map {map_id}")

    def _door_back(self, node_id, to_id):
        """Where the party stands in `node_id` after arriving from `to_id`."""
        for d in self.nodes[node_id]["doors"]:
            if d["to"] == to_id:
                return d["xy"]
        return None

    def route(self, target_map, unlocked):
        """Cheapest way to stand in `target_map`, starting by map-travelling to an unlocked
        outpost. Returns a list of legs, or None.
          {"do": "travel", "outpost": id}
          {"do": "gate", "path": [[x, y], ...], "expect": map id}      recorded walk out of the outpost
          {"do": "door", "xy": [x, y], "beyond": [x, y], "portal": [x, y] | None,
           "expect": map id | None, "through": map id | None, "fight": bool}
        """
        goal = self.area_node.get(target_map)
        if goal is None:
            return None
        heap, best, tie = [], {}, 0
        for o in unlocked:
            for g in self.gates.get(o, []):
                legs = [{"do": "travel", "outpost": o}, {"do": "gate", "path": g["path"], "expect": g["to_map"]}]
                at = g["path"][-1] if g["path"] else None
                heapq.heappush(heap, (0.0, tie, g["node"], at, legs)); tie += 1
            n = self.outpost_node.get(o)
            if n is not None:
                legs = [{"do": "travel", "outpost": o}]
                if self.nodes[n]["map"] is not None and not self.gates.get(o):
                    # the outpost opens straight onto an area and no walk out was recorded:
                    # leave by its own portal, found in the game when we are standing there
                    legs.append({"do": "leave_town", "expect": self.nodes[n]["map"], "from": o})
                heapq.heappush(heap, (500.0, tie, n, None, legs)); tie += 1
        while heap:
            cost, _t, node, at, legs = heapq.heappop(heap)
            if node == goal:
                return legs
            if best.get(node, math.inf) <= cost:
                continue
            best[node] = cost
            here = self.nodes[node]
            for d in here["doors"]:
                step = math.hypot(d["xy"][0] - at[0], d["xy"][1] - at[1]) if at else 3000.0
                nxt = d["to"]
                leg = {"do": "door", "xy": d["xy"], "beyond": d["beyond"], "portal": d["portal"],
                       "expect": self.nodes[nxt]["map"], "through": here["map"],
                       "fight": here["map"] is not None}
                # A stub of a few trapezoids is the far side of a portal recorded in the wrong map
                # file (the Leviathan Pits gate's "Silent Surf" is a 3-trapezoid patch of Leviathan
                # Pits). The game carries the party across it; a leg for it would send the party
                # walking at coordinates from another map.
                add = [] if here.get("size", 99) <= STUB_SIZE else [leg]
                heapq.heappush(heap, (cost + step + CROSSING_COST, tie, nxt, self._door_back(nxt, node), legs + add))
                tie += 1
        return None

    def reachable(self, target_map, unlocked):
        return self.route(target_map, unlocked) is not None

    def town_exit_hint(self, outpost):
        """Where this outpost's way out is, as recorded from the area side (the door tagged with
        the outpost's number). None if unknown."""
        tag = f"{int(outpost):04d}"
        for n in self.nodes.values():
            for d in n["doors"]:
                if d.get("tag") == tag and n["map"] is not None:
                    return tuple(d.get("portal") or d["xy"])
        return None

    def outposts_from(self, map_id):
        """Outposts that can be walked into from this area: [(outpost id, leg)]. Used after a
        vanquish to unlock places for later."""
        node = self.area_node.get(map_id)
        if node is None:
            return []
        out = []
        for o in self.nodes[node]["outposts"]:
            # The outpost sits inside this same walkable piece: walk to its own arrival point
            # and the gate is crossed on the way.
            if o in self.outpost_xy:
                out.append((o, {"do": "walk_in", "xy": self.outpost_xy[o], "expect": o, "through": map_id}))
        for d in self.nodes[node]["doors"]:
            other = self.nodes[d["to"]]
            if other["map"] is None:
                for o in other["outposts"]:
                    out.append((o, {"do": "door", "xy": d["xy"], "beyond": d["beyond"], "portal": d["portal"],
                                    "expect": o, "through": map_id, "fight": True}))
        return out

    def neighbours(self, map_id):
        """Areas one door away from this one: {map id: door leg}."""
        node = self.area_node.get(map_id)
        out = {}
        if node is None:
            return out
        for d in self.nodes[node]["doors"]:
            m = self.nodes[d["to"]]["map"]
            if m is not None and m != map_id:
                out[m] = {"do": "door", "xy": d["xy"], "beyond": d["beyond"], "portal": d["portal"],
                          "expect": m, "through": map_id, "fight": True}
        return out

    def exits_of(self, map_id):
        """Every known way out of an area: door points, portals, and outposts inside it."""
        node = self.area_node.get(map_id)
        if node is None:
            return []
        out = []
        for d in self.nodes[node]["doors"]:
            out.append(tuple(d["xy"]))
            if d["portal"]:
                out.append(tuple(d["portal"]))
        for o in self.nodes[node]["outposts"]:
            if o in self.outpost_xy:
                out.append(tuple(self.outpost_xy[o]))
        return out

    def all_outposts(self):
        return set(self.gates) | set(self.outpost_node)
