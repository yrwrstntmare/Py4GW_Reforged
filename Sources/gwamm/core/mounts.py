"""Ground that takes a mount to cross (Desolation: sulfur that kills on foot, crossed as a
junundu wurm). Standard library only.

The game takes the mount away where it is not needed, so ground crossed while riding is ground
that needs it. That is remembered for the visit; on foot, a walk onto it means: mount first.
Ground never ridden is not judged (the way from the gate to the first burrow, rock islands).

Ground is kept per level (the game's plane) where the level is known: a bridge over a sulfur
strip is not the strip. (Sulfurous Wastes: ridden under the bridge for the pillar, it then took
the bridge for sulfur, went for a burrow 11k away on foot and died.)
"""
import math

CELL = 400.0


def _cell(x, y):
    return (int(math.floor(x / CELL)), int(math.floor(y / CELL)))


def _along(a, b, step=CELL / 2.0):
    d = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(d // step))
    return [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]


class RideGround:
    def __init__(self):
        self.ridden = set()      # map cells crossed while mounted
        self.levels = {}         # cell -> planes it was ridden on (cells with no entry: any level)
        self.cleared = set()     # cells found to take people on foot this visit (set down there)
        self.riding = None       # as last told
        self._last = None        # last place seen mounted

    def _hit(self, cell, plane=None):
        if cell not in self.ridden:
            return False
        lv = self.levels.get(cell)
        return plane is None or not lv or plane in lv

    def note(self, xy, riding, alive=True, plane=None):
        """Tell it where the leader is and whether mounted. Returns "off" when the mount was
        just lost by a living leader: the game set us down, so this spot takes people on foot.
        (alive=False: lost by dying; the ground is as deadly as before.)"""
        out = None
        if riding:
            pts = _along(self._last, xy) if self._last is not None and \
                math.hypot(xy[0] - self._last[0], xy[1] - self._last[1]) < 3000.0 else [xy]
            cells = {_cell(x, y) for x, y in pts}
            self.ridden.update(cells)
            if plane is not None:
                for c in cells:
                    self.levels.setdefault(c, set()).add(plane)
            self._last = (xy[0], xy[1])
        else:
            if self.riding and alive:
                r = int(500.0 // CELL) + 1
                cx, cy = _cell(xy[0], xy[1])
                for i in range(cx - r, cx + r + 1):
                    for j in range(cy - r, cy + r + 1):
                        if math.hypot((i + 0.5) * CELL - xy[0], (j + 0.5) * CELL - xy[1]) <= 500.0 + CELL * 0.71:
                            lv = self.levels.get((i, j))
                            if plane is not None and lv and lv - {plane}:
                                lv.discard(plane)    # still wurm ground on another level
                            else:
                                self.ridden.discard((i, j))
                                self.levels.pop((i, j), None)
                                self.cleared.add((i, j))
                out = "off"
            self._last = None
        self.riding = bool(riding)
        return out

    def on_ridden(self, xy, plane=None):
        return self._hit(_cell(xy[0], xy[1]), plane)

    def crosses(self, a, b, skip=500.0, plane=None):
        """Does the straight walk from a to b go over ridden ground (past the first `skip`)?
        plane: the level walked on (the walker's own; a walk stays on its level)."""
        return any(self._hit(_cell(x, y), plane) for x, y in _along(a, b)
                   if math.hypot(x - a[0], y - a[1]) > skip)

    def needs_mount(self, player_xy, target_xy=None, plane=None):
        """On foot: standing on ground that was ridden, or about to walk onto it."""
        if not self.ridden:
            return False
        return self.on_ridden(player_xy, plane) or (target_xy is not None and self.crosses(player_xy, target_xy, plane=plane))

    def nearest_mount(self, player_xy, mounts):
        """Which burrow to walk to: one reached without crossing ridden ground if there is
        one, then the closest."""
        if not mounts:
            return None
        return min(mounts, key=lambda m: (self.crosses(player_xy, m),
                                          math.hypot(m[0] - player_xy[0], m[1] - player_xy[1])))

    def foot_way(self, nav, start, goal, clear_start=500.0, clear_goal=700.0, gap=600.0, avoid=()):
        """The walk on foot from start to goal (a burrow) over the map graph, going round ground
        ridden as a wurm (sulfur that kills on foot). Returns (points, safe, length): points to
        walk through, about one every `gap`, ending at goal; safe is False when the only way
        crosses ridden ground (the first `clear_start` and the last `clear_goal` are not counted:
        a burrow stands at the sulfur's edge). Each node is judged on its own level. `avoid`:
        (x, y, radius) spots also gone round where possible (deaths with no enemy near).
        ([], False, inf) when there is no way at all."""
        a = nav.nearest_node(start[0], start[1])
        b = nav.nearest_node(goal[0], goal[1])
        if a is None or b is None:
            return [], False, math.inf
        bad = {}
        for cx, cy in self.ridden:
            mx, my = (cx + 0.5) * CELL, (cy + 0.5) * CELL
            for i in nav.nodes_within(mx, my, CELL * 0.75):
                x, y = nav.nodes[i]
                if _cell(x, y) != (cx, cy):
                    continue
                try:
                    lvl = nav.traps[nav.node_trap[i]][0]
                except Exception:
                    lvl = None
                if not self._hit((cx, cy), lvl):
                    continue
                if math.hypot(x - start[0], y - start[1]) <= clear_start or \
                        math.hypot(x - goal[0], y - goal[1]) <= clear_goal:
                    continue
                bad[i] = 1000.0
        soft = dict(bad)
        for ax, ay, r in avoid:
            for i in nav.nodes_within(ax, ay, r):
                soft.setdefault(i, 20.0)
        way = nav.path(a, b, soft)
        if not way:
            return [], False, math.inf
        pts, length, since, last = [], 0.0, 0.0, start
        for i in way:
            x, y = nav.nodes[i]
            d = math.hypot(x - last[0], y - last[1])
            length += d
            since += d
            last = (x, y)
            if since >= gap:
                pts.append((x, y))
                since = 0.0
        pts.append((float(goal[0]), float(goal[1])))
        return pts, not any(i in bad for i in way), length

    def safest_mount(self, nav, player_xy, mounts, avoid=()):
        """Which burrow to walk to on foot, and the way: (spot, points, safe). The shortest walk
        that keeps off ridden ground; when every way crosses it, the shortest of those with
        safe False (standing on it already: get off it the quickest way). None if none reached."""
        best = None
        for m in mounts:
            pts, safe, length = self.foot_way(nav, player_xy, m, avoid=avoid)
            if not pts:
                continue
            key = (not safe, length)
            if best is None or key < best[0]:
                best = (key, m, pts, safe)
        return None if best is None else best[1:]

    def absorb(self, cells, levels=()):
        """Take in wurm ground learnt on earlier visits (cells, [(cell, planes)]): known from the
        first step, before it is ridden again. Ground found walkable this visit stays walkable."""
        for c in cells:
            c = tuple(c)
            if c not in self.cleared:
                self.ridden.add(c)
        for c, planes in levels:
            c = tuple(c)
            if c in self.ridden:
                self.levels.setdefault(c, set()).update(planes)

    def merged_with(self, cells, levels=()):
        """What to keep for the next visit: what was known plus what was ridden now, less what
        was found walkable now. Returns (cells, [(cell, planes)]) as plain lists."""
        out = {tuple(c) for c in cells} | self.ridden
        out -= self.cleared
        lv = {}
        for c, planes in levels:
            if tuple(c) in out:
                lv.setdefault(tuple(c), set()).update(planes)
        for c, planes in self.levels.items():
            if c in out:
                lv.setdefault(c, set()).update(planes)
        return sorted(out), [(c, sorted(p)) for c, p in sorted(lv.items())]

    def mark(self, xy, plane=None):
        """Ground found to hurt on foot (health falling, no enemy near), never ridden: wurm
        ground from now on, so the walk goes round it or mounts first."""
        c = _cell(xy[0], xy[1])
        self.cleared.discard(c)
        self.ridden.add(c)
        if plane is not None:
            self.levels.setdefault(c, set()).add(plane)

    def walked_safely(self, xy, plane=None):
        """Stood on this ground on foot without harm: not wurm ground after all (Joko's Domain:
        sand the wurm rode is walked on foot by the known route). True if a cell was cleared."""
        c = _cell(xy[0], xy[1])
        if not self._hit(c, plane):
            return False
        lv = self.levels.get(c)
        if plane is not None and lv and lv - {plane}:
            lv.discard(plane)
            return True
        self.ridden.discard(c)
        self.levels.pop(c, None)
        self.cleared.add(c)
        return True
