"""What the bot knows about the current instance: coverage and enemies.

Coverage is tracked per node, then summarised per region:
  UNKNOWN   nothing in the region has been near the party
  SEEN      it has been within sight range, but not close enough to trigger hidden groups
  SEARCHED  the party has passed within search range of (almost) all of it
"""
import math

UNKNOWN, SEEN, SEARCHED = 0, 1, 2
STATE_NAMES = {UNKNOWN: "UNKNOWN", SEEN: "SEEN", SEARCHED: "SEARCHED"}
SEARCHED_SHARE = 0.92        # share of a region's nodes that must be searched


PATROL_WATCH = 1700.0        # further from the party than this, an enemy's movement is its own doing
PATROL_ROAM = 700.0          # moved this far from where it was first seen: it patrols


def is_patrol(e):
    """Seen walking about on its own: either it has gone a fair way from where it was first
    seen, or it has gone some way and is walking right now."""
    return e.roam >= PATROL_ROAM or (e.roam >= 250.0 and math.hypot(e.vel[0], e.vel[1]) >= 70.0)


class Enemy:
    __slots__ = ("id", "xy", "first_xy", "first_seen", "last_seen", "alive",
                 "in_range", "lost", "boss", "roam", "trail", "vel", "plane", "z")

    def __init__(self, eid, xy, now, boss):
        self.id, self.xy, self.first_xy = eid, xy, xy
        self.roam = 0.0              # furthest it has been seen from where it was first seen, while not reacting to us
        self.trail = [xy]            # where it has been (sampled): a patrol's beat
        self.vel = (0.0, 0.0)        # how it is moving right now (units a second), smoothed
        self.plane, self.z = None, None      # which piece of ground it stands on, and its height
        self.first_seen = self.last_seen = now
        self.alive, self.in_range, self.lost, self.boss = True, True, False, boss


class Cluster:
    __slots__ = ("members", "centroid", "in_range", "boss", "last_seen")

    def __init__(self, enemies):
        self.members = sorted(e.id for e in enemies)
        n = len(enemies)
        self.centroid = (sum(e.xy[0] for e in enemies) / n, sum(e.xy[1] for e in enemies) / n)
        self.in_range = sum(1 for e in enemies if e.in_range)
        self.boss = any(e.boss for e in enemies)
        self.last_seen = max(e.last_seen for e in enemies)

    @property
    def key(self):
        return ("cluster", self.members[0])


class InstanceMemory:
    def __init__(self, region_map, search_radius, sight_radius=4200.0, cluster_radius=1300.0):
        self.rm, self.nav = region_map, region_map.nav
        self.search_radius, self.sight_radius = search_radius, sight_radius
        self.cluster_radius = cluster_radius
        n = len(self.nav.nodes)
        self.node_closest = [math.inf] * n   # nearest the party has passed to each node
        self.node_searched = [False] * n
        self.node_seen = [False] * n
        self.track_radius = max(search_radius, 2500.0)
        self.region_searched_count = [0] * len(region_map.regions)
        self.region_seen = [False] * len(region_map.regions)
        self.strict = False          # cleanup demands every node, not just most of a region
        self._set_need()
        self.enemies = {}
        self.blocked = {}            # objective key -> failure count
        self.route = []              # where the party has been
        self.walked = 0.0
        self.wipes = 0
        self.escalation = 0          # how many times cleanup had to tighten the search
        self._last_cov_xy = None
        self._last_xy = None

    # ---- coverage ----
    def visit(self, xy):
        if self._last_xy is not None:
            self.walked += math.hypot(xy[0] - self._last_xy[0], xy[1] - self._last_xy[1])
        self._last_xy = xy
        if not self.route or math.hypot(xy[0] - self.route[-1][0], xy[1] - self.route[-1][1]) > 150:
            self.route.append(xy)
        if self._last_cov_xy and math.hypot(xy[0] - self._last_cov_xy[0], xy[1] - self._last_cov_xy[1]) < 120:
            return
        self._last_cov_xy = xy
        nr, nodes, closest, limit = self.rm.node_region, self.nav.nodes, self.node_closest, self.search_radius
        for i in self.nav.nodes_within(xy[0], xy[1], self.track_radius):
            if nr[i] < 0:
                continue
            d = math.hypot(nodes[i][0] - xy[0], nodes[i][1] - xy[1])
            if d < closest[i]:
                closest[i] = d
                if d <= limit and not self.node_searched[i]:
                    self.node_searched[i] = True
                    self.region_searched_count[nr[i]] += 1
        for i in self.nav.nodes_within(xy[0], xy[1], self.sight_radius):
            if not self.node_seen[i] and nr[i] >= 0:
                self.node_seen[i] = True
                self.region_seen[nr[i]] = True

    def _set_need(self):
        share = 1.0 if self.strict else SEARCHED_SHARE
        self._need = [max(1, int(math.ceil(share * len(r.nodes)))) for r in self.rm.regions]

    def region_state(self, rid):
        if self.region_searched_count[rid] >= self._need[rid]:
            return SEARCHED
        return SEEN if self.region_seen[rid] else UNKNOWN

    def unsearched_target(self, rid):
        """A spot inside the region that still needs a close pass (the viewpoint if untouched)."""
        region = self.rm.regions[rid]
        if not self.node_searched[region.node]:
            return region.pos
        todo = [i for i in region.nodes if not self.node_searched[i]]
        if not todo:
            return region.pos
        # The nearest unchecked spot to where we are, so the party sweeps the region in one
        # pass. (It used to aim at the middle of what was left, which moved every time a bit was
        # checked: with a tight search radius the party zig-zagged across one region for minutes.)
        if self.route:
            px, py = self.route[-1]
        else:
            px = sum(self.nav.nodes[i][0] for i in todo) / len(todo)
            py = sum(self.nav.nodes[i][1] for i in todo) / len(todo)
        return self.nav.nodes[min(todo, key=lambda i: (self.nav.nodes[i][0] - px) ** 2 + (self.nav.nodes[i][1] - py) ** 2)]

    def counts(self):
        out = {UNKNOWN: 0, SEEN: 0, SEARCHED: 0}
        for rid in range(len(self.rm.regions)):
            out[self.region_state(rid)] += 1
        return out

    def tighten_search(self, factor=0.75):
        """Cleanup found nothing left to check but foes remain. First demand every last node
        of each region; after that, demand a closer pass. Ground the party already walked
        close enough to stays searched, so only the gaps open up again."""
        self.escalation += 1
        if not self.strict:
            self.strict = True
        else:
            self.search_radius *= factor
        self._set_need()
        self.region_searched_count = [0] * len(self.rm.regions)
        for i, d in enumerate(self.node_closest):
            ok = d <= self.search_radius and self.rm.node_region[i] >= 0
            self.node_searched[i] = ok
            if ok:
                self.region_searched_count[self.rm.node_region[i]] += 1
        self._last_cov_xy = None

    def start_over(self):
        """Forget what was searched and what was given up on, keeping the search as tight as it
        got. Used as the last thing to try before declaring an area cannot be finished."""
        n = len(self.node_closest)
        self.node_closest = [math.inf] * n
        self.node_searched = [False] * n
        self.region_searched_count = [0] * len(self.rm.regions)
        self.blocked.clear()
        self.escalation = 0
        self._last_cov_xy = None

    # ---- enemies ----
    def observe(self, seen, player_xy, now):
        """`seen` is a list of (agent_id, x, y, alive, boss) for every enemy currently visible."""
        ids = set()
        for eid, x, y, alive, boss, *level in seen:
            ids.add(eid)
            e = self.enemies.get(eid)
            if e is None:
                if not alive:
                    continue
                e = self.enemies[eid] = Enemy(eid, (x, y), now, boss)
            dt = now - e.last_seen
            if 0.05 < dt < 3.0:
                vx, vy = (x - e.xy[0]) / dt, (y - e.xy[1]) / dt
                if math.hypot(vx, vy) < 600.0:           # faster than anything walks: a glitch, not movement
                    e.vel = (0.6 * e.vel[0] + 0.4 * vx, 0.6 * e.vel[1] + 0.4 * vy)
            elif dt >= 3.0:
                e.vel = (0.0, 0.0)
            e.xy, e.last_seen, e.alive, e.boss = (x, y), now, alive, boss or e.boss
            e.in_range, e.lost = True, False
            if len(level) >= 2:
                e.plane, e.z = level[0], level[1]
            # Movement seen while the party is too far off to be the cause is the enemy's own:
            # a patrol. (Closer than that it may simply be coming for us.)
            if alive and math.hypot(x - player_xy[0], y - player_xy[1]) > PATROL_WATCH:
                e.roam = max(e.roam, math.hypot(x - e.first_xy[0], y - e.first_xy[1]))
                lx, ly = e.trail[-1]
                if math.hypot(x - lx, y - ly) > 350.0:
                    e.trail.append((x, y))
                    if len(e.trail) > 24:
                        del e.trail[0:len(e.trail) - 24]
        check = self.sight_radius * 0.8
        for eid, e in self.enemies.items():
            if eid in ids:
                continue
            e.in_range = False
            # We are standing where it was and it is not here: it moved or died out of sight.
            if e.alive and not e.lost and math.hypot(e.xy[0] - player_xy[0], e.xy[1] - player_xy[1]) < check:
                e.lost = True

    def patrollers(self):
        """Living enemies that have been seen walking about on their own."""
        return [e for e in self.enemies.values() if e.alive and not e.lost and is_patrol(e)]

    def live_enemies(self):
        return [e for e in self.enemies.values() if e.alive and not e.lost]

    def clusters(self):
        live = self.live_enemies()
        r, cell = self.cluster_radius, self.cluster_radius
        grid = {}
        for idx, e in enumerate(live):
            grid.setdefault((int(e.xy[0] // cell), int(e.xy[1] // cell)), []).append(idx)
        parent = list(range(len(live)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for idx, e in enumerate(live):
            cx, cy = int(e.xy[0] // cell), int(e.xy[1] // cell)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in grid.get((cx + dx, cy + dy), ()):
                        if j > idx and math.hypot(e.xy[0] - live[j].xy[0], e.xy[1] - live[j].xy[1]) <= r:
                            parent[find(j)] = find(idx)
        groups = {}
        for idx, e in enumerate(live):
            groups.setdefault(find(idx), []).append(e)
        return [Cluster(g) for g in groups.values()]
