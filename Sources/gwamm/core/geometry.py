"""Walkable-ground model built from pathing trapezoids. Pure Python, no game imports.

A trapezoid is (plane, xtl, xtr, yt, xbl, xbr, yb): a horizontal strip with a top
edge at yt and a bottom edge at yb.  Trapezoids that share a horizontal edge are
connected.  Sample points ("nodes") are laid along each strip and linked into a
graph, which is what regions, coverage and travel estimates are computed on.
"""
import heapq
import math
from collections import defaultdict

NODE_SPACING = 300.0
_HASH = 600.0


class NavGraph:
    def __init__(self, traps, spacing=NODE_SPACING, links=()):
        keep = [i for i, t in enumerate(traps) if abs(t[3] - t[6]) > 0.5]
        index = {old: new for new, old in enumerate(keep)}
        self.traps = [tuple(traps[i]) for i in keep]
        self.nodes = []          # (x, y)
        self.node_trap = []      # trapezoid index per node
        self.adj = []            # per node: list of (other, distance)
        self.trap_nodes = []     # node indices per trapezoid
        self._build_nodes(spacing)
        self._link_traps(spacing)
        # Joins the game lists between levels (stairs, ramps, bridges): the two pieces of ground
        # touch but do not share a clean horizontal edge, so the edge matching above misses them.
        self.level_links = 0
        for a, b in links:
            a, b = index.get(a), index.get(b)
            if a is None or b is None or a == b:
                continue
            pair = min(((i, j) for i in self.trap_nodes[a] for j in self.trap_nodes[b]),
                       key=lambda ij: (self.nodes[ij[0]][0] - self.nodes[ij[1]][0]) ** 2
                                      + (self.nodes[ij[0]][1] - self.nodes[ij[1]][1]) ** 2)
            self._edge(*pair)
            self.level_links += 1
        self._hash = defaultdict(list)
        for i, (x, y) in enumerate(self.nodes):
            self._hash[(int(x // _HASH), int(y // _HASH))].append(i)
        self.forbidden = [False] * len(self.nodes)   # no-go ground (around area exits)
        self.no_go = []                              # (x, y, radius)
        self._bands = defaultdict(list)
        for ti, t in enumerate(self.traps):
            for b in range(int(min(t[3], t[6]) // 512), int(max(t[3], t[6]) // 512) + 1):
                self._bands[b].append(ti)

    # ---- construction ----
    def _build_nodes(self, spacing):
        for ti, (_p, xtl, xtr, yt, xbl, xbr, yb) in enumerate(self.traps):
            left, right, yc = (xtl + xbl) / 2, (xtr + xbr) / 2, (yt + yb) / 2
            n = max(1, int(math.ceil((right - left) / spacing)))
            ids = []
            for i in range(n):
                ids.append(len(self.nodes))
                self.nodes.append((left + (right - left) * (i + 0.5) / n, yc))
                self.node_trap.append(ti)
                self.adj.append([])
            for a, b in zip(ids, ids[1:]):
                self._edge(a, b)
            self.trap_nodes.append(ids)

    def _edge(self, a, b):
        if a == b or any(o == b for o, _ in self.adj[a]):
            return
        (ax, ay), (bx, by) = self.nodes[a], self.nodes[b]
        d = math.hypot(ax - bx, ay - by)
        self.adj[a].append((b, d))
        self.adj[b].append((a, d))

    def _nearest_in_trap(self, ti, x):
        return min(self.trap_nodes[ti], key=lambda i: abs(self.nodes[i][0] - x))

    def _link_traps(self, spacing):
        by_top = defaultdict(list)
        for ti, t in enumerate(self.traps):
            by_top[int(round(max(t[3], t[6])))].append(ti)
        for ai, a in enumerate(self.traps):
            bottom = min(a[3], a[6])
            a_lo, a_hi = (a[4], a[5]) if a[6] <= a[3] else (a[1], a[2])
            for key in (int(round(bottom)) - 1, int(round(bottom)), int(round(bottom)) + 1):
                for bi in by_top.get(key, ()):
                    if bi == ai:
                        continue
                    b = self.traps[bi]
                    if abs(max(b[3], b[6]) - bottom) > 1.0:
                        continue
                    b_lo, b_hi = (b[1], b[2]) if b[3] >= b[6] else (b[4], b[5])
                    lo, hi = max(a_lo, b_lo), min(a_hi, b_hi)
                    if hi - lo < 1.0:
                        continue
                    k = max(1, int((hi - lo) // spacing))
                    for j in range(k):
                        x = lo + (hi - lo) * (j + 0.5) / k
                        self._edge(self._nearest_in_trap(ai, x), self._nearest_in_trap(bi, x))

    def forbid(self, points, radius):
        """Mark ground within `radius` of each point as no-go. Paths never step INTO no-go
        ground; they may only walk out of it (the party arrives next to an exit)."""
        for x, y in points:
            self.no_go.append((x, y, radius))
            for i in self.nodes_within(x, y, radius):
                self.forbidden[i] = True

    def in_no_go(self, x, y):
        return any((x - px) ** 2 + (y - py) ** 2 <= r * r for px, py, r in self.no_go)

    # ---- queries ----
    def on_mesh(self, x, y, tol=20.0):
        for ti in self._bands.get(int(y // 512), ()):
            _p, xtl, xtr, yt, xbl, xbr, yb = self.traps[ti]
            if yb - tol <= y <= yt + tol:
                f = min(1.0, max(0.0, (y - yb) / (yt - yb)))
                if xbl + (xtl - xbl) * f - tol <= x <= xbr + (xtr - xbr) * f + tol:
                    return True
        return False

    def stacked(self, x, y, tol=60.0):
        """Is there walkable ground on more than one level at (x, y)? (A bridge over a path, a
        ramp over a room.) Getting stuck at such a spot says little about the scenery: the walk
        was usually aimed at the other level."""
        planes = set()
        for ti in self._bands.get(int(y // 512), ()):
            p, xtl, xtr, yt, xbl, xbr, yb = self.traps[ti]
            if yb - tol <= y <= yt + tol:
                f = min(1.0, max(0.0, (y - yb) / ((yt - yb) or 1.0)))
                if xbl + (xtl - xbl) * f - tol <= x <= xbr + (xtr - xbr) * f + tol:
                    planes.add(p)
                    if len(planes) > 1:
                        return True
        return False

    def nodes_within(self, x, y, radius):
        r2, out = radius * radius, []
        cx0, cx1 = int((x - radius) // _HASH), int((x + radius) // _HASH)
        cy0, cy1 = int((y - radius) // _HASH), int((y + radius) // _HASH)
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                for i in self._hash.get((cx, cy), ()):
                    nx, ny = self.nodes[i]
                    if (nx - x) ** 2 + (ny - y) ** 2 <= r2:
                        out.append(i)
        return out

    # ---- is this point, this straight line, on walkable ground? ----
    def _trap_grid(self):
        grid = self.__dict__.get("_tgrid")
        if grid is None:
            grid = self._tgrid = {}
            for i, t in enumerate(self.traps):
                x0, x1 = min(t[1], t[4]), max(t[2], t[5])
                y0, y1 = min(t[3], t[6]), max(t[3], t[6])
                for cx in range(int(x0 // 400), int(x1 // 400) + 1):
                    for cy in range(int(y0 // 400), int(y1 // 400) + 1):
                        grid.setdefault((cx, cy), []).append(i)
        return grid

    def on_ground(self, x, y, slack=12.0):
        """True if (x, y) lies on a walkable piece of ground (any level)."""
        for i in self._trap_grid().get((int(x // 400), int(y // 400)), ()):
            t = self.traps[i]
            yt, yb = max(t[3], t[6]), min(t[3], t[6])
            if y > yt + slack or y < yb - slack:
                continue
            f = 0.5 if yt == yb else (y - t[6]) / (t[3] - t[6])      # 0 at the bottom edge, 1 at the top
            f = min(1.0, max(0.0, f))
            left, right = t[4] + (t[1] - t[4]) * f, t[5] + (t[2] - t[5]) * f
            if left - slack <= x <= right + slack:
                return True
        return False

    def line_clear(self, a, b, step=90.0):
        """True if the straight line from a to b stays on walkable ground the whole way."""
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(d // step))
        return all(self.on_ground(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1))

    def node_plane(self, i):
        """Which of the map's ground pieces (pathing planes) a node lies on."""
        return self.traps[self.node_trap[i]][0]

    def reach_costs(self, start, limit):
        """Walking distance from `start` to every node within `limit` of it (one sweep; used
        when many "how far is the walk to X" questions are asked at once)."""
        best, heap = {start: 0.0}, [(0.0, start)]
        while heap:
            g, u = heapq.heappop(heap)
            if g > best.get(u, math.inf):
                continue
            for v, w in self.adj[u]:
                ng = g + w
                if ng <= limit and ng < best.get(v, math.inf) and (self.forbidden[u] or not self.forbidden[v]):
                    best[v] = ng
                    heapq.heappush(heap, (ng, v))
        return best

    def nearest_node(self, x, y, allowed=None, max_radius=20000.0, plane=None):
        """Nearest node to a point. `plane`: only nodes on that ground piece (where one level
        lies over another, the nearest node in plan view can be on the wrong one)."""
        radius = min(_HASH, max_radius)      # (a limit under the first search ring used to find nothing at all)
        while True:
            best, best_d = None, None
            for i in self.nodes_within(x, y, radius):
                if allowed is not None and not allowed[i]:
                    continue
                if plane is not None and self.traps[self.node_trap[i]][0] != plane:
                    continue
                d = (self.nodes[i][0] - x) ** 2 + (self.nodes[i][1] - y) ** 2
                if best is None or d < best_d:
                    best, best_d = i, d
            if best is not None:
                return best
            if radius >= max_radius:
                return None
            radius = min(radius * 2, max_radius)

    def component(self, start):
        """Flags for every node reachable from `start`."""
        seen = [False] * len(self.nodes)
        seen[start] = True
        stack = [start]
        while stack:
            u = stack.pop()
            for v, _ in self.adj[u]:
                if not seen[v] and (self.forbidden[u] or not self.forbidden[v]):
                    seen[v] = True
                    stack.append(v)
        return seen

    def dijkstra(self, sources):
        """Multi-source shortest paths. Returns (dist, owner) where owner is the
        index into `sources` of the closest source."""
        n = len(self.nodes)
        dist, owner = [math.inf] * n, [-1] * n
        heap = []
        for k, s in enumerate(sources):
            dist[s], owner[s] = 0.0, k
            heap.append((0.0, s))
        heapq.heapify(heap)
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u]:
                continue
            for v, w in self.adj[u]:
                nd = d + w
                if self.forbidden[v] and not self.forbidden[u]:
                    continue
                if nd < dist[v]:
                    dist[v], owner[v] = nd, owner[u]
                    heapq.heappush(heap, (nd, v))
        return dist, owner

    def path(self, start, goal, penalty=None):
        """Shortest walkable path between two nodes (A*). Returns node ids, or [] if none.
        `penalty` maps node -> factor (>1) making ground there costlier, so the path goes round
        it when a detour is worth it and through it when there is no other way."""
        if start == goal:
            return [start]
        gx, gy = self.nodes[goal]
        best = {start: 0.0}
        parent = {}
        heap = [(math.hypot(self.nodes[start][0] - gx, self.nodes[start][1] - gy), 0.0, start)]
        while heap:
            _f, g, u = heapq.heappop(heap)
            if u == goal:
                out = [u]
                while u in parent:
                    u = parent[u]
                    out.append(u)
                return out[::-1]
            if g > best.get(u, math.inf):
                continue
            for v, w in self.adj[u]:
                ng = g + (w * penalty.get(v, 1.0) if penalty else w)
                if self.forbidden[v] and not self.forbidden[u]:
                    continue
                if ng < best.get(v, math.inf):
                    best[v] = ng
                    parent[v] = u
                    vx, vy = self.nodes[v]
                    heapq.heappush(heap, (ng + math.hypot(vx - gx, vy - gy), ng, v))
        return []


def town_waypoints(traps, links, start_xy, goal_xy, start_plane=None, gap=450.0):
    """Points to walk through, in order, from `start_xy` to `goal_xy` over this ground: one
    about every `gap`, and always one where the way changes level. For errands in towns.

    The game's own walk goes to a spot on the level the character is standing on. A town on
    several levels (the Eye of the North has 24 pieces of ground) sends it to the floor under
    or over the target when the arrival point is on another level, and it paces about there.
    Walking our own route in short hops keeps each hop on one level. [] if there is no route."""
    nav = NavGraph(traps, links=links)
    a = nav.nearest_node(start_xy[0], start_xy[1], max_radius=600.0, plane=start_plane) if start_plane is not None else None
    if a is None:
        a = nav.nearest_node(start_xy[0], start_xy[1])
    b = nav.nearest_node(goal_xy[0], goal_xy[1])
    if a is None or b is None:
        return []
    route = nav.path(a, b)
    if not route:
        return []
    out, since = [], 0.0
    for i in range(1, len(route)):
        since += math.hypot(nav.nodes[route[i]][0] - nav.nodes[route[i - 1]][0],
                            nav.nodes[route[i]][1] - nav.nodes[route[i - 1]][1])
        changed = nav.node_plane(route[i]) != nav.node_plane(route[i - 1])
        if changed or since >= gap:
            out.append(nav.nodes[route[i]])
            since = 0.0
    if not out or math.hypot(out[-1][0] - goal_xy[0], out[-1][1] - goal_xy[1]) > 150.0:
        out.append((goal_xy[0], goal_xy[1]))
    return out


def loose_links(traps, vert_tol=100.2, horiz_tol=100.6):
    """Every pair of trapezoids on different planes that touch (the tolerances Reforged's own
    pathing uses). A last resort for maps whose levels are still cut off from each other."""
    buckets = defaultdict(list)
    for i, t in enumerate(traps):
        for b in range(int((min(t[3], t[6]) - vert_tol) // 256), int((max(t[3], t[6]) + vert_tol) // 256) + 1):
            buckets[b].append(i)
    out = set()
    for ids in buckets.values():
        for n, i in enumerate(ids):
            a = traps[i]
            al, ar = min(a[1], a[4]), max(a[2], a[5])
            for j in ids[n + 1:]:
                b = traps[j]
                if a[0] == b[0]:
                    continue
                bl, br = min(b[1], b[4]), max(b[2], b[5])
                if (abs(a[6] - b[3]) < vert_tol or abs(a[3] - b[6]) < vert_tol) and ar >= bl and br >= al:
                    out.add((i, j))
                elif ((abs(a[5] - b[4]) < horiz_tol or abs(a[4] - b[5]) < horiz_tol)
                      and max(a[3], a[6]) >= min(b[3], b[6]) and max(b[3], b[6]) >= min(a[3], a[6])):
                    out.add((i, j))
    return sorted(out)


def walked_round(point, trail, inner=300.0, outer=900.0):
    """Has the party stood on every side of `point` (each quarter round it, between `inner` and
    `outer` away)? A real door sits on the edge of the walkable ground, so one side of it is
    always out of reach; a point walked round on all four sides is not a door."""
    px, py = point
    quarters = set()
    for x, y in trail:
        dx, dy = x - px, y - py
        d = math.hypot(dx, dy)
        if inner <= d <= outer:
            quarters.add((dx >= 0, dy >= 0))
            if len(quarters) == 4:
                return True
    return False
