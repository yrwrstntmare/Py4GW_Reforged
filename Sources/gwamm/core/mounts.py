"""Ground that takes a mount to cross (Desolation: sulfur that kills on foot, crossed as a
junundu wurm). Standard library only.

The game takes the mount away where it is not needed, so ground crossed while riding is ground
that needs it. That is remembered for the visit; on foot, a walk onto it means: mount first.
Ground never ridden is not judged (the way from the gate to the first burrow, rock islands).
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
        self.riding = None       # as last told
        self._last = None        # last place seen mounted

    def note(self, xy, riding, alive=True):
        """Tell it where the leader is and whether mounted. Returns "off" when the mount was
        just lost by a living leader: the game set us down, so this spot takes people on foot.
        (alive=False: lost by dying; the ground is as deadly as before.)"""
        out = None
        if riding:
            pts = _along(self._last, xy) if self._last is not None and \
                math.hypot(xy[0] - self._last[0], xy[1] - self._last[1]) < 3000.0 else [xy]
            self.ridden.update(_cell(x, y) for x, y in pts)
            self._last = (xy[0], xy[1])
        else:
            if self.riding and alive:
                r = int(500.0 // CELL) + 1
                cx, cy = _cell(xy[0], xy[1])
                for i in range(cx - r, cx + r + 1):
                    for j in range(cy - r, cy + r + 1):
                        if math.hypot((i + 0.5) * CELL - xy[0], (j + 0.5) * CELL - xy[1]) <= 500.0 + CELL * 0.71:
                            self.ridden.discard((i, j))
                out = "off"
            self._last = None
        self.riding = bool(riding)
        return out

    def on_ridden(self, xy):
        return _cell(xy[0], xy[1]) in self.ridden

    def crosses(self, a, b, skip=500.0):
        """Does the straight walk from a to b go over ridden ground (past the first `skip`)?"""
        return any(_cell(x, y) in self.ridden for x, y in _along(a, b)
                   if math.hypot(x - a[0], y - a[1]) > skip)

    def needs_mount(self, player_xy, target_xy=None):
        """On foot: standing on ground that was ridden, or about to walk onto it."""
        if not self.ridden:
            return False
        return self.on_ridden(player_xy) or (target_xy is not None and self.crosses(player_xy, target_xy))

    def nearest_mount(self, player_xy, mounts):
        """Which burrow to walk to: one reached without crossing ridden ground if there is
        one, then the closest."""
        if not mounts:
            return None
        return min(mounts, key=lambda m: (self.crosses(player_xy, m),
                                          math.hypot(m[0] - player_xy[0], m[1] - player_xy[1])))
