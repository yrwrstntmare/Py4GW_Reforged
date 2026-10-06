"""Cartographer: which world-map cells are still fogged, and where to stand to clear them.

Rules follow GWToolbox++'s Cartographer widget (commit 1ce867e):
  - the explored map is a bitmap, one bit per 32x32 world-map-unit cell
  - 96 game units per world-map unit, Y flipped
  - standing in a cell credits it and the ring of cells around it
"""
import math

WM_PER_CELL = 32.0
GAME_PER_WM = 96.0
REVEAL_RING = 1
_EPS = 1.0 / 64.0


class CartoGrid:
    def __init__(self, width, height, words):
        self.width, self.height, self.words = width, height, words
        self.row_words = width >> 5

    def explored(self, cx, cy):
        if cx < 0 or cy < 0 or cx >= self.width or cy >= self.height:
            return False
        w = cy * self.row_words + (cx >> 5)
        return w < len(self.words) and bool((self.words[w] >> (cx & 31)) & 1)


class Projection:
    """Game position <-> world-map cell for one map. Anchor as in Toolbox's GetMapWorldAnchor."""

    def __init__(self, wm_left, wm_top, game_min_x, game_max_y):
        self.ax = wm_left - game_min_x / GAME_PER_WM
        self.ay = wm_top + game_max_y / GAME_PER_WM + 1.0

    def cell(self, x, y):
        wx, wy = x / GAME_PER_WM + self.ax, -y / GAME_PER_WM + self.ay
        return (int(math.floor((wx + _EPS) / WM_PER_CELL)), int(math.ceil((wy - _EPS) / WM_PER_CELL)) - 1)

    def cell_corners(self, cx, cy):
        """Game-space corners of a cell (min x, min y, max x, max y)."""
        x0 = (cx * WM_PER_CELL - self.ax) * GAME_PER_WM
        x1 = ((cx + 1) * WM_PER_CELL - self.ax) * GAME_PER_WM
        y0 = -((cy + 1) * WM_PER_CELL - self.ay) * GAME_PER_WM
        y1 = -(cy * WM_PER_CELL - self.ay) * GAME_PER_WM
        return x0, y0, x1, y1


def _clip(poly, x0, y0, x1, y1):
    """Clip a convex polygon to an axis-aligned box (Sutherland-Hodgman)."""
    def cut(points, inside, cross):
        out = []
        for i, cur in enumerate(points):
            prev = points[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
        return out

    def at_x(x):
        return lambda a, b: (x, a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]))

    def at_y(y):
        return lambda a, b: (a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]), y)

    for inside, cross in ((lambda p: p[0] >= x0, at_x(x0)), (lambda p: p[0] <= x1, at_x(x1)),
                          (lambda p: p[1] >= y0, at_y(y0)), (lambda p: p[1] <= y1, at_y(y1))):
        poly = cut(poly, inside, cross)
        if not poly:
            return []
    return poly


class CartoPlan:
    """Where the party can stand to clear fog.

    A cell can be stood in if ANY walkable ground pokes into it, even a sliver at the foot of a
    cliff. Those slivers are exactly the awkward edge pieces of the map, so every reachable
    trapezoid is cut against the cell grid, and each cell keeps the footing that sits deepest
    inside it (the easiest spot to actually stand on)."""

    def __init__(self, region_map, projection, min_depth=25.0):
        self.rm, self.proj = region_map, projection
        nav = region_map.nav
        self.stand = {}                  # cell -> (footing xy, graph node, depth inside the cell)
        for ti, trap in enumerate(nav.traps):
            ids = [i for i in nav.trap_nodes[ti] if region_map.reachable[i]]
            if not ids:
                continue
            _p, xtl, xtr, yt, xbl, xbr, yb = trap
            poly = [(xtl, yt), (xtr, yt), (xbr, yb), (xbl, yb)]
            c1 = projection.cell(min(xtl, xbl), yb)
            c2 = projection.cell(max(xtr, xbr), yt)
            for cx in range(min(c1[0], c2[0]), max(c1[0], c2[0]) + 1):
                for cy in range(min(c1[1], c2[1]), max(c1[1], c2[1]) + 1):
                    x0, y0, x1, y1 = projection.cell_corners(cx, cy)
                    piece = _clip(poly, x0, y0, x1, y1)
                    if len(piece) < 3:
                        continue
                    fx = sum(p[0] for p in piece) / len(piece)
                    fy = sum(p[1] for p in piece) / len(piece)
                    if nav.in_no_go(fx, fy):
                        continue
                    depth = min(fx - x0, x1 - fx, fy - y0, y1 - fy)
                    if depth < min_depth:
                        continue
                    best = self.stand.get((cx, cy))
                    if best is None or depth > best[2]:
                        node = min(ids, key=lambda i: (nav.nodes[i][0] - fx) ** 2 + (nav.nodes[i][1] - fy) ** 2)
                        self.stand[(cx, cy)] = ((fx, fy), node, depth)
        self.coverable = set()
        self._ring = {}
        for cell in self.stand:
            ring = {(cell[0] + dx, cell[1] + dy)
                    for dx in range(-REVEAL_RING, REVEAL_RING + 1)
                    for dy in range(-REVEAL_RING, REVEAL_RING + 1)}
            self._ring[cell] = ring
            self.coverable |= ring
        self.declined = set()            # stood there, cell stayed fogged
        self._cache_key, self._cache = None, []

    def fogged(self, grid):
        return {c for c in self.coverable if c not in self.declined and not grid.explored(*c)}

    def targets(self, grid):
        """Greedy pick of standing cells that clear every fogged cell, preferring roomy footings.
        Returns a list of (stand_cell, footing_xy, node_id, set_of_cells_it_reveals)."""
        fog = self.fogged(grid)
        key = frozenset(fog)
        if key == self._cache_key:
            return self._cache
        out = []
        gains = {cell: ring & fog for cell, ring in self._ring.items()}
        gains = {c: g for c, g in gains.items() if g}
        while gains:
            best = max(gains, key=lambda c: (len(gains[c]), self.stand[c][2]))
            got = gains.pop(best)
            footing, node, _depth = self.stand[best]
            out.append((best, footing, node, got))
            gains = {c: g - got for c, g in gains.items() if g - got}
        self._cache_key, self._cache = key, out
        return out
