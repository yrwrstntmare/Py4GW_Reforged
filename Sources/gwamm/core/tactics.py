"""Fight decisions. Pure Python, fixed rules (no learning): which enemy to call, and when and
where to fall back so a big pull strings out instead of landing on the party all at once."""
import math

HEALER, CASTER_PROF = (3, 8), (4, 5, 6)      # monk, ritualist / necromancer, mesmer, elementalist


def threat(e, player_xy):
    """Higher = kill sooner. e: dict(id, xy, hp 0-1, level, boss, caster, prof).
    Order of concern: whoever keeps the others alive, then bosses (they hit hardest and carry
    the elite), then casters, then the rest; within a class, the one nearest to dying and
    nearest to us, so kills come quickly and the pressure drops."""
    s = 0.0
    if e.get("prof") in HEALER:
        s += 60.0
    elif e.get("prof") in CASTER_PROF or e.get("caster"):
        s += 25.0
    if e.get("boss"):
        s += 40.0
    s += 2.0 * max(0, int(e.get("level", 0)) - 20)
    s += 20.0 * (1.0 - max(0.0, min(1.0, e.get("hp", 1.0))))
    s -= math.hypot(e["xy"][0] - player_xy[0], e["xy"][1] - player_xy[1]) / 100.0
    return s


def pick_target(enemies, player_xy, current=None, reach=1500.0):
    """The enemy to call, or None. Keeps the current call unless another is clearly worse to
    leave alone (so the party does not swap targets every second)."""
    near = [e for e in enemies if math.hypot(e["xy"][0] - player_xy[0], e["xy"][1] - player_xy[1]) <= reach]
    if not near:
        return None
    best = max(near, key=lambda e: threat(e, player_xy))
    cur = next((e for e in near if e["id"] == current), None)
    if cur is not None and threat(best, player_xy) < threat(cur, player_xy) + 15.0:
        return cur["id"]
    return best["id"]


def crowd(enemy_xys, player_xy, radius=1600.0):
    return sum(1 for x, y in enemy_xys if math.hypot(x - player_xy[0], y - player_xy[1]) <= radius)


def fallback_point(trail, player_xy, enemy_xys, distance=1300.0, clear=1100.0):
    """A spot back along the way we came (ground already cleared), `distance` behind us, with no
    known enemy within `clear` of it. None if the trail is too short or was broken by a shrine
    jump, or the spot is not clear."""
    left, prev = distance, player_xy
    for xy in reversed(trail):
        step = math.hypot(xy[0] - prev[0], xy[1] - prev[1])
        if step > 1200.0:
            return None                      # the trail jumps here (revived at a shrine)
        left -= step
        prev = xy
        if left <= 0:
            if any(math.hypot(ex - xy[0], ey - xy[1]) < clear for ex, ey in enemy_xys):
                return None
            return xy
    return None
