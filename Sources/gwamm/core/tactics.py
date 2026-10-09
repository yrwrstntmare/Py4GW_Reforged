"""Fight decisions. Pure Python, fixed rules (no learning): which enemy to call, and when and
where to fall back so a big pull strings out instead of landing on the party all at once."""
import math

HEALER, CASTER_PROF = (3, 8), (4, 5, 6)      # monk, ritualist / necromancer, mesmer, elementalist


# Where the heroes stand round a flag point: (forward, sideways) from it, facing the enemy.
# Every pair is at least 320 apart, the reach of the widest area spells. (Recordings: flagged
# onto one spot, all seven heroes took the same hit at the same instant, again and again.)
# Fighters take the first, forward places; casters and healers the ones behind.
FORMATION = ((140.0, 0.0), (80.0, -340.0), (80.0, 340.0), (-220.0, -170.0), (-220.0, 170.0),
             (-300.0, -520.0), (-300.0, 520.0))


def formation(centre, toward, count, usable=None):
    """`count` standing places round `centre`, the first ones nearest `toward`.

    `usable(x, y)`: can a hero stand there (walkable, in a straight line from the centre). On a
    path or a bridge the wide places are off the edge. Such a place is pulled in towards the
    centre until it fits; if it never does, the hero gets a place of its own on a small ring
    round the centre. Never the same spot twice: sending every hero whose place was off the
    path to ONE fallback spot put four of seven back in a heap (recordings, 0.45.0)."""
    dx, dy = toward[0] - centre[0], toward[1] - centre[1]
    d = math.hypot(dx, dy) or 1.0
    fx, fy = dx / d, dy / d

    def at(f, side):
        return (centre[0] + fx * f - fy * side, centre[1] + fy * f + fx * side)

    out = []
    for i, (f, side) in enumerate(FORMATION[:count]):
        spot = None
        for k in (1.0, 0.75, 0.55, 0.4):
            p = at(f * k, side * k)
            if usable is None or usable(p[0], p[1]):
                spot = p
                break
        if spot is None:
            for turn in range(8):                        # a ring of its own, 170 out, each hero at its own angle
                ang = (i * 51.0 + turn * 45.0) * math.pi / 180.0
                p = (centre[0] + 170.0 * math.cos(ang), centre[1] + 170.0 * math.sin(ang))
                if usable(p[0], p[1]) and all(math.hypot(p[0] - q[0], p[1] - q[1]) >= 120.0 for q in out):
                    spot = p
                    break
        out.append(spot or centre)
    return out


def _load_roles():
    """What each enemy type does, learnt from the fight recordings (data/enemy_roles.json,
    built by gwamm_tools/build_enemy_roles.py). The game gives no profession for ordinary
    enemies; what they were seen casting does. Missing file: no roles, the old order applies."""
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "enemy_roles.json")
    try:
        with open(path, encoding="utf-8") as f:
            return {int(k): v.get("role", "") for k, v in json.load(f).items()}
    except Exception:
        return {}


ROLES = _load_roles()


def raises_dead(description):
    """A resurrection: kill whoever casts it before anything else (Restore Life says "returned
    to life", not "resurrect")."""
    d = (description or "").lower()
    return "resurrect" in d or "returned to life" in d or "return to life" in d


def heals_allies(description):
    """A skill that restores or protects someone else on its side (not a self-heal), judged
    from its full description text."""
    import re
    d = (description or "").lower()
    if raises_dead(d):
        return True
    helps = re.search(r"\bheals?\b|\bhealed\b|health regeneration|damage (is )?reduc|negates?|prevents?", d)
    others = re.search(r"\ball(y|ies)\b|party", d)
    return bool(helps and others and "steal" not in d)


def supports_allies(description):
    """A skill whose job is keeping someone else on its side going: heals, protection,
    condition or hex removal for allies. Broader than heals_allies (which feeds the role
    table built from the Enemy Tracker): a monk casting Aegis or Reversal of Fortune is the
    one to kill first just as much as one casting Orison of Healing."""
    import re
    d = (description or "").lower()
    if heals_allies(d):
        return True
    if re.search(r"\bsteals? (up to )?(\[|\d)", d) or not re.search(
            r"\b(target (other )?ally|other ally|party members?|all(y|ies)|adjacent creatures)\b", d):
        return False
    return bool(re.search(r"gains? .{0,80}health|\bblock\b|remove .{0,40}(condition|hex)|conditions? .{0,20}transferred|"
                          r"damage .{0,20}reduc|cannot be (killed|reduced)|cannot lose more than|"
                          r"heal yourself and all", d))


def learn_role(model, role):
    """A role seen in the fight itself (an enemy casting a heal is a healer), for types neither
    our table nor the Enemy Tracker knew. Returns True when it is new."""
    model = int(model or 0)
    if not model or not role or ROLES.get(model) == role:
        return False
    if ROLES.get(model) and role != "healer":
        return False                     # a healer sighting may upgrade a type; nothing downgrades one
    ROLES[model] = role
    return True


def add_roles(extra):
    """Roles from another source (the Enemy Tracker widget's records) for enemy types our own
    table does not have. Ours, measured from recorded fights, wins where both know a type.
    Returns how many were added."""
    added = 0
    for model, role in (extra or {}).items():
        if role and int(model) not in ROLES:
            ROLES[int(model)] = role
            added += 1
    return added


def role_of(model):
    return ROLES.get(model, "")


def threat(e, player_xy):
    """Higher = kill sooner. e: dict(id, xy, hp 0-1, level, boss, caster, prof).
    Order of concern: whoever keeps the others alive, then bosses (they hit hardest and carry
    the elite), then casters, then the rest; within a class, the one nearest to dying and
    nearest to us, so kills come quickly and the pressure drops."""
    s = 0.0
    if e.get("casting") == "raise":
        s += 120.0                   # raising one of theirs right now: stop it before anything else
    elif e.get("casting") == "support":
        s += 30.0                    # healing or shielding someone this very moment
    if e.get("prof") in HEALER or e.get("role") == "healer":
        s += 60.0
    elif e.get("prof") in CASTER_PROF or e.get("caster"):
        s += 25.0
    if e.get("boss"):
        s += 40.0
    if e.get("role") == "minion":
        s -= 35.0                    # summoned things and spirits: they fall with their makers, or do not matter
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


def retreat_point(trail, player_xy, enemy_xys, lo=600.0, hi=1600.0, recent=400):
    """Where to give ground to: the walked spot, 600-1600 from here, that is furthest from every
    known enemy, and further from them than where we stand. Unlike `fallback_point` it does not
    count steps back along the trail: the retreat itself is added to the trail, so "back along
    it" soon pointed forward again and the party shuffled on the spot while a patrol walked in."""
    if not enemy_xys:
        return None

    def room(p):
        return min(math.hypot(ex - p[0], ey - p[1]) for ex, ey in enemy_xys)

    best, best_room = None, room(player_xy) + 150.0
    for xy in list(trail)[-recent:]:
        d = math.hypot(xy[0] - player_xy[0], xy[1] - player_xy[1])
        if lo <= d <= hi:
            r = room(xy)
            if r > best_room:
                best, best_room = xy, r
    return best


def linked(seed, enemies, link=900.0):
    """The group an enemy belongs to: everyone connected to it by gaps of at most `link`.
    `enemies` is a list of dicts with "id" and "xy". Returns the set of ids."""
    def reach(a, b):
        """How far apart these two can be and still be one group (0: they are not).
        One standing while a patrol walks past: not a group. Both patrols: one patrol, and
        a patrol strings out and its members turn at different moments, so it is held together
        over a longer gap and whatever their headings. (A patrol of seven was read as five
        groups of one to three, a pull "of 3" brought all seven. Recordings: two walking
        enemies up to 1000 apart join the same fight more often than not.)"""
        va, vb = a.get("vel") or (0.0, 0.0), b.get("vel") or (0.0, 0.0)
        sa, sb = math.hypot(va[0], va[1]), math.hypot(vb[0], vb[1])
        za, zb = a.get("z"), b.get("z")
        if za is not None and zb is not None and abs(za - zb) > 260.0:
            return 0.0                   # a storey apart is two groups, however close in plan view
        if a.get("patrol") and b.get("patrol"):
            return link * 1.7            # both known to roam (not just shuffling on the spot)
        if (sa >= 60.0) != (sb >= 60.0) and max(sa, sb) >= 100.0 and (a.get("patrol") or b.get("patrol")):
            return 0.0
        return link

    todo, got = [seed], {seed["id"]}
    while todo:
        cur = todo.pop()
        for e in enemies:
            if (e["id"] not in got
                    and math.hypot(e["xy"][0] - cur["xy"][0], e["xy"][1] - cur["xy"][1]) <= reach(cur, e)):
                got.add(e["id"])
                todo.append(e)
    return got


def engagement(player_xy, enemies, min_group=4, max_take=9, near=1250.0, far=2100.0,
               link=900.0, join=1200.0, patrol_reach=1600.0, patrol_close=2800.0):
    """What starting a fight with the nearest enemy would bring, and what to do about it.

    `enemies`: dicts with "id", "xy", "patrol" (bool: seen walking about on its own) and "trail"
    (points it has been seen at). Returns None when there is nothing to decide (nobody in the
    band between `near` and `far`), otherwise a dict:

      target   the nearest enemy (id, xy), the one to draw
      group    how many are linked to it: they come together
      joiners  how many more stand within `join` of that group: they usually come too
      patrols  how many patrolling enemies, not already counted, have a beat that passes
               within `patrol_reach` of the target; `patrol_near` of them are within
               `patrol_close` of it right now
      total    group + joiners (+ patrol_near)
      verdict  "fight"  small enough to take as it stands, where it stands
               "pull"   (with patrol_first=True) the patrol itself is the target: see below
               "pull"   bring it back to the party
               "wait"   a patrol is close by, or the total is more than `max_take`: hold off,
                        out of reach, and look again
    """
    if not enemies:
        return None
    px, py = player_xy
    nearest = min(enemies, key=lambda e: (e["xy"][0] - px) ** 2 + (e["xy"][1] - py) ** 2)
    d = math.hypot(nearest["xy"][0] - px, nearest["xy"][1] - py)
    if d < near or d > far:
        return None
    group = linked(nearest, enemies, link)
    members = [e for e in enemies if e["id"] in group]
    joiners = [e for e in enemies if e["id"] not in group
               and any(math.hypot(e["xy"][0] - m["xy"][0], e["xy"][1] - m["xy"][1]) <= join for m in members)]
    counted = group | {e["id"] for e in joiners}
    tx, ty = nearest["xy"]

    def ahead(e):
        """Where it will be over the next 25 seconds if it keeps walking as it is now."""
        vx, vy = e.get("vel") or (0.0, 0.0)
        if math.hypot(vx, vy) < 40.0:
            return []
        return [(e["xy"][0] + vx * t, e["xy"][1] + vy * t) for t in (5.0, 10.0, 15.0, 20.0, 25.0)]

    def passes(e):
        """A patrol is a threat to this fight if any of these holds:
        - its known beat passes the target
        - the way it is walking now takes it past the target
        - it is close and its beat is not known yet (few sightings): it could go anywhere"""
        if any(math.hypot(x - tx, y - ty) <= patrol_reach for x, y in list(e.get("trail") or ()) + [e["xy"]]):
            return True
        if any(math.hypot(x - tx, y - ty) <= patrol_reach for x, y in ahead(e)):
            return True
        return math.hypot(e["xy"][0] - tx, e["xy"][1] - ty) <= patrol_close and len(e.get("trail") or ()) < 6

    def soon(e):
        """Could it be here before the fight is over? Close now and not walking away, or
        further off but walking this way."""
        d_now = math.hypot(e["xy"][0] - tx, e["xy"][1] - ty)
        steps = ahead(e)
        if steps:
            d_then = math.hypot(steps[-1][0] - tx, steps[-1][1] - ty)
            if any(math.hypot(x - tx, y - ty) <= patrol_reach for x, y in steps):
                return True                      # walking into it, from however far
            if d_then > d_now + 600.0 and d_now > 1500.0:
                return False                     # walking away and already clear
        return d_now <= patrol_close

    patrols = [e for e in enemies if e["id"] not in counted and e.get("patrol") and passes(e)]
    patrol_near = [e for e in patrols if soon(e)]
    total = len(members) + len(joiners) + len(patrol_near)
    if patrol_near:
        # Often the better answer to a patrol is to take it first: it comes to you anyway, and
        # with it gone the group it circles can be fought in peace. Worth it when the patrol is
        # within fetching distance, small enough to take, and at a point on its beat where the
        # standing group would not come with it.
        lead = min(patrol_near, key=lambda e: (e["xy"][0] - px) ** 2 + (e["xy"][1] - py) ** 2)
        ld = math.hypot(lead["xy"][0] - px, lead["xy"][1] - py)
        pack = linked(lead, [e for e in enemies if e.get("patrol")], link)
        pack_members = [e for e in enemies if e["id"] in pack]
        drags = [e for e in enemies if e["id"] not in pack
                 and any(math.hypot(e["xy"][0] - m["xy"][0], e["xy"][1] - m["xy"][1]) <= join for m in pack_members)]
        if near <= ld <= far + 500.0 and not drags and len(pack_members) <= max_take:
            return {"target": lead["id"], "target_xy": lead["xy"], "distance": ld, "group": len(pack_members),
                    "joiners": 0, "patrols": len(patrols), "patrol_near": len(patrol_near),
                    "total": len(pack_members), "verdict": "pull", "patrol_first": True, "members": sorted(pack)}
    if patrol_near or total > max_take:
        verdict = "wait"
    elif total >= min_group:
        verdict = "pull"
    else:
        verdict = "fight"
    return {"target": nearest["id"], "target_xy": nearest["xy"], "distance": d, "group": len(members),
            "joiners": len(joiners), "patrols": len(patrols), "patrol_near": len(patrol_near),
            "total": total, "verdict": verdict, "members": sorted(group)}


def pull_plan(player_xy, enemy_xys, min_group=4, near=1250.0, far=2100.0, spread=1500.0):
    """Whether to pull the nearest group back to the party instead of walking into it.
    Returns (nearest enemy xy, size of its group, its distance) or None.

    Pull when the nearest enemy is close enough to fetch (under `far`) but has not noticed the
    party yet (over `near`), and it is not alone: at least `min_group` enemies stand within
    `spread` of it. A straggler or a pair is simply fought; a group is brought to ground the
    party has already cleared, away from its neighbours."""
    if not enemy_xys:
        return None
    px, py = player_xy
    nearest = min(enemy_xys, key=lambda p: (p[0] - px) ** 2 + (p[1] - py) ** 2)
    d = math.hypot(nearest[0] - px, nearest[1] - py)
    if d < near or d > far:
        return None
    group = sum(1 for x, y in enemy_xys if math.hypot(x - nearest[0], y - nearest[1]) <= spread)
    if group < min_group:
        return None
    return nearest, group, d


def fight_forecast(spot, anchor, enemy_xys, spread=1500.0, earshot=1500.0):
    """What a fight at `spot` against the group around `anchor` would involve, from positions
    alone: (members of that group, other enemies standing within earshot of the spot). The
    second number is the one to get to zero: those are the ones that join in uninvited."""
    group = [p for p in enemy_xys if math.hypot(p[0] - anchor[0], p[1] - anchor[1]) <= spread]
    others = [p for p in enemy_xys if p not in group and math.hypot(p[0] - spot[0], p[1] - spot[1]) <= earshot]
    return len(group), len(others)


def choose_camp(trail, target_xy, enemies, members=(), near=1150.0, far=2300.0, clear=1450.0):
    """Where to park the party and fight what is pulled. A point on the ground already walked
    (`trail`, oldest first) that is
      - within fetching distance of the target (between `near` and `far`: closer and the rest
        of the crowd notices the party, further and what is pulled gives up on the way), and
      - at least `clear` from every enemy that is NOT being pulled, and from everywhere those
        enemies have been seen walking (their beats).
    Of the points that qualify, the nearest: the leader has to run back to it with the group
    behind him. None if there is none."""
    members = set(members)
    keep_off = []
    for e in enemies:
        if e["id"] in members:
            continue
        keep_off.append(e["xy"])
        if e.get("patrol"):
            keep_off.extend(e.get("trail") or ())
    best, best_d = None, 1e18
    for p in trail:
        d = math.hypot(p[0] - target_xy[0], p[1] - target_xy[1])
        if d < near or d > far:
            continue
        room = min((math.hypot(p[0] - x, p[1] - y) for x, y in keep_off), default=1e9)
        if room >= clear and d < best_d:         # the nearest that qualifies: the leader has to get back to it
            best, best_d = (p[0], p[1]), d
    return best


def clusters(enemies, link=500.0):
    """Split everything in view into groups: enemies standing (or walking) within `link` of each
    other. Tighter than the "who might join in" distance on purpose: the question here is who
    wakes up TOGETHER, and a spawn group stands close. Returns a list of lists of enemy dicts."""
    left, out = list(enemies), []
    while left:
        ids = linked(left[0], left, link)
        out.append([e for e in left if e["id"] in ids])
        left = [e for e in left if e["id"] not in ids]
    return out


# Chance that an enemy joins a fight, by how far it stands from the nearest enemy already in
# it. Measured from the fight recordings (about 5,000 pairs over five areas): there is no sharp
# edge to a group, the chance just falls away with distance.
JOIN_CHANCE = ((200.0, 0.8), (400.0, 0.65), (600.0, 0.6), (800.0, 0.5), (1000.0, 0.4), (1400.0, 0.35), (1800.0, 0.2))


def join_chance(distance):
    for limit, chance in JOIN_CHANCE:
        if distance <= limit:
            return chance
    return 0.0


def plan_fight(leader_xy, enemies, trail, max_take=9, aggro=1000.0, margin=150.0, look=2600.0,
               near=None, far=1700.0, stand=None, route_fn=None, leader_z=None):
    """Tell the groups in front of us apart and decide how to start: which one to wake, from
    where, where to fight it, and what else that would bring.

    An idle group wakes when a party member comes within `aggro` of any of its members, and it
    wakes whole. So what a pull brings is decided by WHERE the leader stands when he is
    noticed: every group with a member within reach of that spot comes, and no other. For each
    group in view the best such spot is tried (on the line to its nearest member, just inside
    its reach), the groups that spot would wake are counted, and the spot that wakes the least
    is chosen. Patrols are counted where they will be over the next 20 seconds, not only where
    they are now.

    Planning starts once the nearest group is within `far` (close enough to see the room, still
    outside everyone's reach). The party itself is a candidate camp: stopping here and fighting
    here is the usual answer; the ground behind is used when here is too close to the others.

    `stand`: how far from the chosen enemy the leader stops. By default just inside its notice
    range (he wakes it by being there, and wakes everything else within reach of that spot
    too). A leader with a ranged weapon passes his weapon range instead: he stops OUTSIDE
    everyone's notice range and hits the one enemy, and only that enemy's group comes.

    Returns None when nothing is near enough to plan for, otherwise a dict:
      incoming  a patrol that is not part of the plan is heading for where the party stands, or
                the party is standing on that patrol's beat: give ground first, whatever the
                verdict, until it is standing somewhere the patrol does not go
      verdict   "pull"   wake `members` from `tag_xy`, fight at `camp`
                "probe"  everything wakes more than `max_take`, but the crowd may be more than
                         one group: wake its nearest edge from as far out as possible, fight
                         far back, and get out if all of it comes
                "wait"   a patrol is about to walk into the chosen fight: hold off
                "avoid"  a single group bigger than `max_take`: it cannot be split; keep away
      groups    sizes of the groups told apart, nearest first
      total     how many the chosen pull is expected to bring
    """
    near = aggro + margin if near is None else near
    px, py = leader_xy
    reach = aggro + margin
    # groups are told apart over everything known, then those with a member within `look` are
    # kept whole (cutting at a distance would slice a group in two and miscount it)
    packs = [pk for pk in clusters(list(enemies))
             if min(math.hypot(e["xy"][0] - px, e["xy"][1] - py) for e in pk) <= look]
    if not packs:
        return None
    seen = [e for pk in packs for e in pk]

    def gap(pack, p):
        return min(math.hypot(e["xy"][0] - p[0], e["xy"][1] - p[1]) for e in pack)

    def is_patrol(pack):
        return sum(1 for e in pack if e.get("patrol")) * 2 >= len(pack)

    def future(pack, p, seconds=(5.0, 10.0, 15.0, 20.0)):
        """Closest this group comes to `p` over the next 20 s if it keeps walking as it is."""
        best = gap(pack, p)
        for e in pack:
            vx, vy = e.get("vel") or (0.0, 0.0)
            if math.hypot(vx, vy) < 40.0:
                continue
            for t in seconds:
                best = min(best, math.hypot(e["xy"][0] + vx * t - p[0], e["xy"][1] + vy * t - p[1]))
        return best

    packs.sort(key=lambda pk: gap(pk, leader_xy))
    if gap(packs[0], leader_xy) > far:
        return None
    options = []
    for pk in packs:
        m = min(pk, key=lambda e: (e["xy"][0] - px) ** 2 + (e["xy"][1] - py) ** 2)
        d = math.hypot(m["xy"][0] - px, m["xy"][1] - py)
        if d > far:
            continue
        if sum(1 for e in pk if e.get("blocked")) * 2 > len(pk):
            continue                     # behind a wall or on another level: counted, never the one we go for
        stand_at = aggro - 60.0 if stand is None else stand
        tag = leader_xy if d <= stand_at else (m["xy"][0] + (px - m["xy"][0]) * stand_at / d,
                                            m["xy"][1] + (py - m["xy"][1]) * stand_at / d)
        woken = [q for q in packs if gap(q, tag) <= reach or (is_patrol(q) and future(q, tag) <= reach)]
        if pk not in woken:
            woken.append(pk)
        ids = sorted(e["id"] for q in woken for e in q)
        # only ground no nearer the group than we stand now: the trail also holds where earlier
        # attempts walked (and died), and a camp picked from those lay forward, among the groups
        behind = [p for p in trail if math.hypot(p[0] - m["xy"][0], p[1] - m["xy"][1]) >= d - 50.0]
        camp = choose_camp(behind + [leader_xy], m["xy"], seen, ids, near=aggro + 100.0, far=aggro + 1100.0)
        # what else may well come: everyone not in the plan, weighed by how near they stand to it
        inside = [e["xy"] for q in woken for e in q]
        extra = sum(join_chance(min(math.hypot(e["xy"][0] - x, e["xy"][1] - y) for x, y in inside))
                    for q in packs if q not in woken for e in q)
        late = [q for q in packs if q not in woken and is_patrol(q)
                and future(q, camp if camp is not None else tag) <= reach]
        # The way the woken group walks to us, and the party fights along: any other group near
        # that way is likely to join (Sunjiang: a group a level down came round the long way, past
        # a bigger one, and the party fighting towards it woke that one too).
        on_route = []
        if route_fn is not None:
            try:
                way = route_fn(m["xy"], camp if camp is not None else tag) or []
            except Exception:
                way = []
            way = way[::2] if len(way) > 40 else way
            for q in packs:
                if q in woken:
                    continue
                if any(math.hypot(e["xy"][0] - x, e["xy"][1] - y) <= reach * 0.8 for e in q for x, y in way):
                    on_route.append(q)
        other_level = (leader_z is not None and m.get("z") is not None and abs(m["z"] - leader_z) > 260.0)
        options.append({"target": m["id"], "target_xy": m["xy"], "tag_xy": tag, "distance": d,
                        "members": ids, "total": len(ids), "woken_groups": [len(q) for q in woken],
                        "patrol_first": is_patrol(pk), "camp": camp, "late": sum(len(q) for q in late),
                        "on_route": sum(len(q) for q in on_route), "other_level": other_level,
                        "likely": round(len(ids) + extra + 0.7 * sum(len(q) for q in on_route), 1)})
    if not options:
        return None
    sizes = [len(pk) for pk in packs]

    def rank(o):       # fits and has somewhere to fight; then: nothing about to walk in; patrols first; least; nearest
        return (o["total"] > max_take, o["camp"] is None, o["late"] > 0, o["likely"] > max_take,
                o["other_level"], o["on_route"] > 0, not o["patrol_first"], o["likely"], o["distance"])

    best = min(options, key=rank)
    best["groups"] = sizes
    def beat_passes(pack, p, within):
        """Has any of this patrol ever been seen within `within` of p? A patrol that turns,
        doubles back or weaves cannot be predicted from its heading, but where it has walked
        before it will walk again."""
        return any(math.hypot(x - p[0], y - p[1]) <= within for e in pack for x, y in (e.get("trail") or ()))

    chosen = set(best["members"])
    best["incoming"] = sum(
        len(q) for q in packs
        if is_patrol(q) and not set(e["id"] for e in q) <= chosen and gap(q, leader_xy) <= 3200.0
        and (future(q, leader_xy) <= reach + 250.0 or beat_passes(q, leader_xy, reach + 100.0)))
    if best["total"] > max_take:
        # More than the party should take. If that is several groups a single spot would wake,
        # they may yet drift apart or be woken from another side: "probe". If it is ONE group
        # (or groups so on top of each other they cannot be told apart) there is no clever way
        # in: "avoid" it, leave it for last, and say so.
        best["verdict"] = "probe" if len(best["woken_groups"]) > 1 and max(best["woken_groups"]) <= max_take else "avoid"
    elif best["late"] or best["incoming"] or best["camp"] is None:
        best["verdict"] = "wait"
    else:
        best["verdict"] = "pull"
    return best


def take_limit(max_take, morale):
    """How many enemies at once the party should accept, given its morale (100 = no death
    penalty, 40 = the -60% floor). Death penalty cuts health and energy by that fraction, so
    what the party can stand shrinks with it. Never below 3: a pair or three is always fought."""
    return max(3, int(round(max_take * max(40.0, min(110.0, morale)) / 100.0)))


def _ahead(e, seconds=(4.0, 8.0, 12.0, 16.0, 20.0)):
    vx, vy = e.get("vel") or (0.0, 0.0)
    if math.hypot(vx, vy) < 40.0:
        return []
    return [(e["xy"][0] + vx * t, e["xy"][1] + vy * t) for t in seconds]


def incoming_to(spot, enemies, engaged=(), reach=1150.0, watch=2400.0):
    """Groups that are NOT in the fight at `spot` but are on their way into it. `engaged`: ids of
    the enemies already being fought. A group counts when it is within `watch` and any of:
      - the way it is walking now brings it within `reach` of the spot inside 20 seconds
      - it is closing on the spot at walking pace or better
      - it patrols, and has been seen within `reach` of the spot before (the spot is on its beat)
    Returns a list of groups (each a list of enemy dicts), nearest first."""
    engaged = set(engaged)
    out = []
    for pack in clusters([e for e in enemies if e["id"] not in engaged]):
        d = min(math.hypot(e["xy"][0] - spot[0], e["xy"][1] - spot[1]) for e in pack)
        if d > watch:
            continue
        soon = any(math.hypot(x - spot[0], y - spot[1]) <= reach for e in pack for x, y in _ahead(e))
        closing = 0
        for e in pack:
            vx, vy = e.get("vel") or (0.0, 0.0)
            gap = math.hypot(spot[0] - e["xy"][0], spot[1] - e["xy"][1]) or 1.0
            if (vx * (spot[0] - e["xy"][0]) + vy * (spot[1] - e["xy"][1])) / gap >= 70.0:
                closing += 1
        beat = (sum(1 for e in pack if e.get("patrol")) * 2 >= len(pack)
                and any(math.hypot(x - spot[0], y - spot[1]) <= reach for e in pack for x, y in (e.get("trail") or ())))
        if soon or closing * 2 >= len(pack) or (beat and d <= 1900.0):
            out.append((d, pack))
    return [p for _d, p in sorted(out, key=lambda t: t[0])]


def shift_camp(trail, camp, enemies, engaged=(), nearest=700.0, furthest=1600.0, clear=1500.0):
    """Somewhere to drag a fight to, away from a group walking into it: a point back along the
    ground already walked, between `nearest` and `furthest` from the present camp, that is at
    least `clear` from every enemy not in the fight, from where those are heading, and from the
    beats of the patrols among them. The closest such point (the fight has to get there with
    the enemy on its heels). None if the trail offers none, or breaks (a shrine jump) first."""
    engaged = set(engaged)
    keep_off = []
    for e in enemies:
        if e["id"] in engaged:
            continue
        keep_off.append(e["xy"])
        keep_off.extend(_ahead(e))
        if e.get("patrol"):
            keep_off.extend(e.get("trail") or ())
    if not trail:
        return None
    start = min(range(len(trail)), key=lambda i: (trail[i][0] - camp[0]) ** 2 + (trail[i][1] - camp[1]) ** 2)
    prev = trail[start]
    for i in range(start - 1, -1, -1):
        p = trail[i]
        if math.hypot(p[0] - prev[0], p[1] - prev[1]) > 1200.0:
            return None
        prev = p
        d = math.hypot(p[0] - camp[0], p[1] - camp[1])
        if d > furthest:
            return None
        if d >= nearest and all(math.hypot(p[0] - x, p[1] - y) >= clear for x, y in keep_off):
            return (p[0], p[1])
    return None


def on_arrival(level, rate, gap, speed=290.0, notice=1000.0, cap=8000.0):
    """What a 0-1 level (energy, party health) will be by the time the party reaches the next
    fight `gap` away, refilling at `rate` per second while it walks. The fight starts about
    `notice` short of the enemy; at most `cap` of walking is counted
    (the caller passes a short gap when nothing is known ahead: an unseen group can be round the corner)."""
    walk = max(0.0, min(gap, cap) - notice) / speed
    return min(1.0, level + max(0.0, rate) * walk)
