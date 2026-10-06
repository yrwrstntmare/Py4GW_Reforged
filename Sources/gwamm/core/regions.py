"""Regions: the coarse map the planner reasons about instead of raw trapezoids.

Viewpoints are picked so every reachable node lies within `radius` of one.
Each node belongs to the viewpoint that is closest by walking distance.
"""
import heapq
import math
from collections import deque


class Region:
    __slots__ = ("id", "node", "pos", "nodes", "neighbors")

    def __init__(self, rid, node, pos):
        self.id, self.node, self.pos = rid, node, pos
        self.nodes = []
        self.neighbors = {}      # region id -> walking distance between viewpoints


class RegionMap:
    def __init__(self, nav, start_xy, radius):
        self.nav, self.radius = nav, radius
        start = nav.nearest_node(*start_xy)
        if start is None:
            raise ValueError("start position is nowhere near walkable ground")
        # No-go ground is never a place to go, search or stand, even though the party may start on it.
        self.reachable = [ok and not nav.forbidden[i] for i, ok in enumerate(nav.component(start))]
        start = nav.nearest_node(*start_xy, allowed=self.reachable)
        if start is None:
            raise ValueError("no walkable ground outside the no-go zones")
        self.regions = []
        self._pick_viewpoints(start)
        dist, owner = nav.dijkstra([r.node for r in self.regions])
        self.node_region = [o if self.reachable[i] else -1 for i, o in enumerate(owner)]
        for i, rid in enumerate(self.node_region):
            if rid >= 0:
                self.regions[rid].nodes.append(i)
        for u, ru in enumerate(self.node_region):
            if ru < 0:
                continue
            for v, w in nav.adj[u]:
                rv = self.node_region[v]
                if rv >= 0 and rv != ru:
                    d = dist[u] + w + dist[v]
                    if d < self.regions[ru].neighbors.get(rv, math.inf):
                        self.regions[ru].neighbors[rv] = d
                        self.regions[rv].neighbors[ru] = d
        self.dist, self.next_hop = self._all_pairs()

    def _pick_viewpoints(self, start):
        nav, covered = self.nav, [False] * len(self.nav.nodes)
        seen = [False] * len(nav.nodes)
        seen[start] = True
        queue = deque([start])
        while queue:
            u = queue.popleft()
            if not covered[u]:
                x, y = nav.nodes[u]
                self.regions.append(Region(len(self.regions), u, (x, y)))
                for i in nav.nodes_within(x, y, self.radius):
                    covered[i] = True
            for v, _ in nav.adj[u]:
                if not seen[v] and self.reachable[v]:
                    seen[v] = True
                    queue.append(v)

    def _all_pairs(self):
        n = len(self.regions)
        dist = [[math.inf] * n for _ in range(n)]
        nxt = [[-1] * n for _ in range(n)]
        for s in range(n):
            d, first = dist[s], nxt[s]
            d[s] = 0.0
            first[s] = s
            heap = [(0.0, s)]
            while heap:
                du, u = heapq.heappop(heap)
                if du > d[u]:
                    continue
                for v, w in self.regions[u].neighbors.items():
                    if du + w < d[v]:
                        d[v] = du + w
                        first[v] = v if u == s else first[u]
                        heapq.heappush(heap, (d[v], v))
        return dist, nxt

    def region_at(self, x, y):
        i = self.nav.nearest_node(x, y, allowed=self.reachable)
        return -1 if i is None else self.node_region[i]

    def path(self, a, b):
        """Region ids from a to b, both included."""
        if a < 0 or b < 0 or self.next_hop[a][b] < 0:
            return []
        out = [a]
        while a != b:
            a = self.next_hop[a][b]
            out.append(a)
        return out

    def tour(self, start_region, only=None, max_passes=30):
        """A visiting order starting at `start_region`: nearest-neighbour, then 2-opt.
        `only` limits it to a set of regions (the start is always first)."""
        d = self.dist
        pool = set(range(len(self.regions))) if only is None else set(only)
        order, left = [start_region], pool - {start_region}
        n = len(left) + 1
        while left:
            cur = order[-1]
            nxt = min(left, key=lambda r: d[cur][r])
            order.append(nxt)
            left.discard(nxt)
        for _ in range(max_passes):
            improved = False
            for i in range(1, n - 1):
                for j in range(i + 1, n):
                    a, b = order[i - 1], order[i]
                    c = order[j]
                    e = order[j + 1] if j + 1 < n else None
                    delta = d[a][c] - d[a][b]
                    if e is not None:
                        delta += d[b][e] - d[c][e]
                    if delta < -1.0:
                        order[i:j + 1] = reversed(order[i:j + 1])
                        improved = True
            if not improved:
                break
        return order

    def tour_length(self, order):
        return sum(self.dist[a][b] for a, b in zip(order, order[1:]))
