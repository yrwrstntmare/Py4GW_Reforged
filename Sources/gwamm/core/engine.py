"""The adaptive engine: perception in, "go here next" out. Pure Python.

It never moves the character or fights. A runtime layer feeds it observations
and carries out the step it returns.
"""
import math

from .cartography import CartoPlan
from .geometry import NavGraph
from .memory import SEARCHED, SEEN, UNKNOWN, InstanceMemory
from .regions import RegionMap

SWEEP, CLEANUP, EXPLORE, DONE, FAILED = "SWEEP", "CLEANUP", "EXPLORE", "DONE", "FAILED"


class Config:
    search_radius = 2500.0       # how close the party must pass to count ground as searched.
                                 # Sight-based: hidden groups only join the foe count once triggered,
                                 # so the party does not need to walk on top of every patch of ground.
    sight_radius = 4200.0        # how far enemies are reliably visible
    cluster_radius = 1300.0
    engage_radius = 1200.0       # clear radius handed to the fight routine (small = fewer groups pulled)
    standoff_patience = 2        # plans to wait at the standoff point before closing in
    standoff = 1000.0            # stop this far short of a group and let it come to the party
    interrupt_distance = 3500.0  # a visible group within this walk may interrupt the current objective
    divert_distance = 3500.0     # during the sweep, leave the route for groups this close
    cleanup_threshold = 15       # foes remaining at which cleanup rules take over
    switch_factor = 1.3          # a new objective must beat the current one by this much
    max_escalations = 4
    max_failures = 2
    edge_exit_radius = 350.0     # once the vanquish is done, how close to an exit the map-clearing may go
    edge_pass = False            # after the vanquish, also clear map cells that sit beside exits (extra time)
    retreat_when_losing = True   # several of the party down and the enemy still here: the living pull right back
    heroes_first = False         # (off: wasted more time than it saved in live runs) before a fight, send the heroes in ahead so they take the first hits, not you
    call_targets = True          # call healers, bosses and casters first so the party focuses them
    fall_back = True             # on a big pull, back up along cleared ground so it strings out
    crowd_size = 9               # this many enemies around us counts as a big pull
    fall_back_distance = 1000.0
    fall_back_cooldown = 25.0
    hazard_radius = 450.0        # ground kept clear of around a spot where something other than enemies killed us
    hazard_factor = 15.0         # (a detour up to this many times longer is accepted to stay out of it)
    take_blessings = True        # walk to a shrine NPC in reach and take its blessing or bounty
    blessing_reach = 3500.0      # how far off the way a blessing is worth
    max_fresh_starts = 2         # full re-searches of the area before a vanquish is given up
    step_length = 2800.0         # how far ahead each walking step reaches
    transit_step_length = 1200.0     # crossing an area on the way somewhere: shorter steps,
    transit_avoid_radius = 1900.0    # keep this far from known enemies when there is a way round,
    transit_avoid_factor = 5.0       # (a detour up to this many times longer is accepted)
    transit_block_radius = 1500.0    # a group this close to the path ahead is fought first, alone
    transit_lookahead = 3500.0
    rest_hp = 0.85               # do not start toward the next objective below this health...
    rest_energy = 0.60           # ...or this energy...
    rest_ally_hp = 0.80          # ...or this average party health, or with anyone dead
    rest_max_seconds = 75.0      # but never wait longer than this
    danger_radius = 2500.0       # after a death, leave this much ground around it for later
    danger_seconds = 600.0
    carto_fetch_limit = -1.0     # (off: no gain in simulation) fetch a far map cell when the plan brings us nearest, up to this walk
    carto_leftover_walk = 0.0    # after the vanquish, skip a leftover map cell that needs a longer walk than this (0 = no limit)
    carto_fetch_slack = 0.6
    route_hints = True           # search first where the area's known route goes; the rest only if foes remain
    route_hint_radius = 2500.0
    off_route_cost = 1.25        # walking off the known route's lanes counts as this much longer
    use_routes = False           # follow the area's known route when there is one, instead of sweeping every corner
    carto_route_reach = 5000.0   # on a route: fetch a map cell from the route point nearest it, if the walk is within this
    carto_in_tour = True         # plan map-only corners into the visiting order with everything else
    carto_detour = 3000.0        # take a fogged map cell now if it is within this walk
    carto_patience = 12.0        # seconds to stand in a map cell before deciding it gives no credit
    exit_avoid_radius = 1000.0   # never walk this close to a portal out of the area
    retour = True                # re-plan the visiting order from where the party stands after each objective
    off_tour_value = 1.0         # pull of a pending region that is not next on the tour (next = 2.5)
    capture_elites = True        # use Signet of Capture on bosses carrying an elite we lack
    multibox = False             # set by the bot: other players are in the party, so leave the party as it is
    party_mode = 0               # 0 work it out from the party, 1 always treat as heroes only, 2 always as shared
    pcons_mode = 0               # consumables: 0 never, 1 once the area proves hard, 2 from the start
    pcons_after_deaths = 2       # mode 1: deaths in this run before they are used
    pcons_min_foes = 25          # not for the tail of an area
    pcons_conset = True
    pcons_pcons = True
    buy_signets = True           # campaigns: top the bar back up with signets between areas
    change_secondary = True      # campaigns: switch secondary in the outpost for the best elites
    unlock_outposts = True       # campaigns: after a vanquish, walk into a locked outpost if one is reachable
    do_vanquish = True
    do_cartography = True


class Objective:
    __slots__ = ("kind", "key", "pos", "region", "value", "data", "target", "waited", "walk")

    def __init__(self, kind, key, pos, region, value, data=None):
        self.kind, self.key, self.pos, self.region, self.value, self.data = kind, key, pos, region, value, data
        self.target = None           # for clusters: the member we are walking at
        self.walk = None             # exact walking distance when known
        self.waited = 0              # plans spent standing at the standoff point with no kill

    def label(self):
        return f"{self.kind} {self.key[1]}"


class Engine:
    def __init__(self, traps, start_xy, config=None, projection=None, exits=(), goto=None, links=(), guide=None, hints=None):
        self.cfg = config or Config()
        self.fresh_starts = 0
        self.hazards = []             # (x, y): traps and the like, learnt from deaths with no enemy near
        self.goto = goto              # crossing an area: just get to this point, fighting what is in the way
        self.nav = NavGraph(traps, links=links)
        self.exits = self._relevant_exits(exits, start_xy)
        self.nav.forbid(self.exits, self.cfg.exit_avoid_radius)
        self.exit_radius = self.cfg.exit_avoid_radius
        self.doors = []               # exits known for certain to be doors (game portals, places we arrived at)
        self.edge_pass = False
        self.rm = RegionMap(self.nav, start_xy, self.cfg.search_radius)
        self.mem = InstanceMemory(self.rm, self.cfg.search_radius, self.cfg.sight_radius, self.cfg.cluster_radius)
        self.start_region = self._start_region(start_xy)
        self.tour = self.rm.tour(self.start_region)
        self.guide, self.guide_i, self._guide_near = self._prepare_guide(guide, start_xy), 0, {}
        self._route_src = list(guide or hints or ())
        self.walls, self.detours, self._detour = [], 0, False
        self.hint_regions = self._hint_regions(hints)
        self.route_pen = self._route_lanes(hints) if self.cfg.route_hints else None
        self.tour_rank = {rid: i for i, rid in enumerate(self.tour)}
        self.carto = CartoPlan(self.rm, projection) if projection else None
        self.carto_targets = []
        self.grid = None
        self.mode = SWEEP
        self.objective = None
        self.player_xy = start_xy
        self.player_region = self.start_region
        self.foes_remaining = None
        self.clusters = []
        self.note = ""
        self._now = 0.0
        self._stand_since = None
        self.player_node = self.nav.nearest_node(start_xy[0], start_xy[1], allowed=self.rm.reachable)
        self.route_ahead = []
        self.carto_given_up = 0
        self.carto_skipped_far = set()
        self.danger = []             # (x, y, expires) around places the party died
        self.deaths = 0

    def _relevant_exits(self, exits, start_xy):
        """Keep only exits that touch ground the party can actually walk to, and merge ones that
        sit on top of each other. Arrival points inside a neighbouring outpost share this map's
        geometry but are walled off from it, so they are dropped."""
        start = self.nav.nearest_node(*start_xy)
        if start is None:
            return list(exits)
        walkable, radius, kept = self.nav.component(start), self.cfg.exit_avoid_radius, []
        for x, y in exits:
            if not any(walkable[i] for i in self.nav.nodes_within(x, y, radius)):
                continue
            if any(math.hypot(x - kx, y - ky) < 0.5 * radius for kx, ky in kept):
                continue
            kept.append((x, y))
        return kept

    def open_edges(self, radius=None):
        """The vanquish is finished, so leaving the area can no longer undo it. Shrink the fence
        round every exit to just the doorway and plan again: the map cells along the edges,
        which could only be stood in from inside the old fence, become reachable. Returns how
        many fogged cells are now on the list."""
        radius = self.cfg.edge_exit_radius if radius is None else radius
        nav = self.nav
        # During the vanquish every point that might be a door was fenced, to be safe; some of
        # those sit in corridors and wall off whole districts. Now only the certain doors matter,
        # and only so the party does not walk out before the rest of the map is cleared.
        if self.doors:
            self.exits = [e for e in self.exits
                          if any(math.hypot(e[0] - d[0], e[1] - d[1]) <= 600.0 for d in self.doors)]
        nav.no_go, nav.forbidden = [], [False] * len(nav.nodes)
        nav.forbid(self.exits, radius)
        self.exit_radius, self.edge_pass = radius, True
        declined = self.carto.declined if self.carto else set()
        proj = self.carto.proj if self.carto else None
        self.rm = RegionMap(nav, self.player_xy, self.cfg.search_radius)
        self.mem = InstanceMemory(self.rm, self.cfg.search_radius, self.cfg.sight_radius, self.cfg.cluster_radius)
        self.start_region = self._start_region(self.player_xy)
        self.tour = self.rm.tour(self.start_region)
        self.tour_rank = {rid: i for i, rid in enumerate(self.tour)}
        self.player_node = nav.nearest_node(self.player_xy[0], self.player_xy[1], allowed=self.rm.reachable)
        self.player_region = self.start_region
        self.objective, self.mode, self.clusters, self._stand_since = None, EXPLORE, [], None
        self.carto_targets = []
        if proj is not None:
            self.carto = CartoPlan(self.rm, proj)
            self.carto.declined = set(declined)
            if self.grid is not None:
                self.carto_targets = self.carto.targets(self.grid)
        return sum(len(t[3]) for t in self.carto_targets)

    def _prepare_guide(self, points, start_xy):
        """A known route through the area (waypoints in walking order), made usable from where
        we actually stand: points off the walkable ground or inside an exit fence are dropped,
        and the list is turned so it starts at the point nearest to us."""
        if not points or self.goto is not None:
            return []
        keep = []
        for x, y in points:
            if self.nav.in_no_go(x, y):
                continue
            node = self.nav.nearest_node(x, y, allowed=self.rm.reachable, max_radius=700.0)
            if node is None:
                continue
            if keep and math.hypot(x - keep[-1][0], y - keep[-1][1]) < 150.0:
                continue
            keep.append((float(x), float(y)))
        if len(keep) < 3:
            return []
        first = min(range(len(keep)), key=lambda i: (keep[i][0] - start_xy[0]) ** 2 + (keep[i][1] - start_xy[1]) ** 2)
        return keep[first:] + keep[:first]

    def _hint_regions(self, points):
        """Regions the area's known route passes through or near. Its authors walked where the
        enemies are, so these are searched first; ground the route never goes near is left
        unless foes still remain afterwards. Empty set = no route known, search everything."""
        out = set()
        if not points or self.goto is not None:
            return out
        nr = self.rm.node_region
        for x, y in points:
            for i in self.nav.nodes_within(x, y, self.cfg.route_hint_radius):
                if nr[i] >= 0:
                    out.add(nr[i])
        return out

    def _route_lanes(self, points):
        """Ground away from the known route costs a little more to walk, so between two places
        the party takes the way the route's authors took where there is one: the same side of a
        group to come at it from, the same corridors (which are the ones they found safe)."""
        if not points or self.goto is not None:
            return None
        on = set()
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            n = max(1, int(math.hypot(bx - ax, by - ay) // 300.0))
            if n > 40:
                continue                         # a jump in the route, not a walk
            for k in range(n + 1):
                x, y = ax + (bx - ax) * k / n, ay + (by - ay) * k / n
                on.update(self.nav.nodes_within(x, y, 450.0))
        f = self.cfg.off_route_cost
        return {i: f for i in range(len(self.nav.nodes)) if i not in on} if on else None

    def guiding(self):
        return self.guide_i < len(self.guide)

    # ---- perception ----
    def update(self, player_xy, enemies, foes_remaining, now, carto_grid=None):
        self.player_xy = player_xy
        self.player_node = self._track_node(player_xy)
        if self.player_node is not None:
            self.player_region = self._region_for(self.player_node, player_xy)
        self.foes_remaining = foes_remaining
        self.mem.visit(player_xy)
        self.mem.observe(enemies, player_xy, now)
        self.clusters = self.mem.clusters()
        while self.guide_i < len(self.guide):       # tick off route points as we reach (or give up on) them
            gx, gy = self.guide[self.guide_i]
            if (math.hypot(gx - player_xy[0], gy - player_xy[1]) < 500.0
                    or self.mem.blocked.get(("guide", self.guide_i), 0) >= self.cfg.max_failures):
                self.guide_i += 1
            else:
                break
        if self._detour and not self.guiding():
            self.guide, self.guide_i, self._guide_near, self._detour = [], 0, {}, False   # past it: plan freely again
        if carto_grid is not None and self.carto is not None:
            self.grid = carto_grid
            self.carto_targets = self.carto.targets(carto_grid)
        self._now = now

    def _start_region(self, xy):
        r = self.rm.region_at(*xy)
        if r >= 0:
            return r
        nr, nodes = self.rm.node_region, self.nav.nodes
        for radius in (1200.0, 2500.0, 5000.0, 10000.0, 30000.0):
            near = [i for i in self.nav.nodes_within(xy[0], xy[1], radius) if nr[i] >= 0]
            if near:
                return nr[min(near, key=lambda i: (nodes[i][0] - xy[0]) ** 2 + (nodes[i][1] - xy[1]) ** 2)]
        return 0

    def _region_for(self, node, xy):
        """The region the party counts as standing in. Ground inside an exit fence (where every
        run starts) belongs to no region; the answer then is the nearest region, or the last one
        we were in. It must never be "none": distances to everything are measured from it, and
        with none they came out as if the party stood in the last region on the list - the far
        side of the map - so far-away targets looked near and the party paced back and forth
        along the edge of the fence."""
        r = self.rm.node_region[node]
        if r >= 0:
            return r
        nr, nodes = self.rm.node_region, self.nav.nodes
        for radius in (1200.0, 2500.0, 5000.0, 10000.0):
            near = [i for i in self.nav.nodes_within(xy[0], xy[1], radius) if nr[i] >= 0]
            if near:
                return nr[min(near, key=lambda i: (nodes[i][0] - xy[0]) ** 2 + (nodes[i][1] - xy[1]) ** 2)]
        return self.player_region if getattr(self, "player_region", -1) >= 0 else 0

    def _track_node(self, xy):
        """The graph node under the party. Where planes overlap (a bridge over ground) the
        nearest point can be on the wrong level, so prefer nodes a few links from the last one."""
        prev = self.player_node
        if prev is not None:
            near, frontier = {prev}, [prev]
            for _ in range(4):
                frontier = [v for u in frontier for v, _w in self.nav.adj[u] if v not in near and not near.add(v)]
            best = min(near, key=lambda i: (self.nav.nodes[i][0] - xy[0]) ** 2 + (self.nav.nodes[i][1] - xy[1]) ** 2)
            if math.hypot(self.nav.nodes[best][0] - xy[0], self.nav.nodes[best][1] - xy[1]) <= 500.0:
                return best
        return self.nav.nearest_node(xy[0], xy[1], allowed=self.rm.reachable)

    # ---- planning ----
    def _cost(self, pos, region):
        if region < 0:
            return math.inf
        if region == self.player_region or self.player_region < 0:
            return math.hypot(pos[0] - self.player_xy[0], pos[1] - self.player_xy[1])
        vp = self.rm.regions[region].pos
        return self.rm.dist[self.player_region][region] + math.hypot(pos[0] - vp[0], pos[1] - vp[1])

    def _walk_distance(self, pos):
        """Real walking distance from the party to a spot (exact when nearby, estimated when far)."""
        if math.hypot(pos[0] - self.player_xy[0], pos[1] - self.player_xy[1]) > 2 * self.cfg.divert_distance:
            return self._cost(pos, self.rm.region_at(*pos))
        goal = self.nav.nearest_node(pos[0], pos[1], allowed=self.rm.reachable)
        if goal is None or self.player_node is None:
            return math.inf
        route = self.nav.path(self.player_node, goal)
        if not route:
            return math.inf
        nodes = self.nav.nodes
        return sum(math.hypot(nodes[a][0] - nodes[b][0], nodes[a][1] - nodes[b][1]) for a, b in zip(route, route[1:]))

    def _vanquish_active(self):
        if self.goto is not None:
            return False
        return self.cfg.do_vanquish and (self.foes_remaining is None or self.foes_remaining > 0)

    def _hazard_penalty(self):
        walls = getattr(self, "walls", ())
        if not self.hazards and not walls:
            return None
        pen = {}
        for x, y in walls:                   # ground the party could not walk through (a shut gate)
            for i in self.nav.nodes_within(x, y, 400.0):
                pen[i] = 1000.0
        for x, y in self.hazards:
            for i in self.nav.nodes_within(x, y, self.cfg.hazard_radius):
                pen[i] = self.cfg.hazard_factor
        return pen

    def _hazard_near(self, reach):
        px, py = self.player_xy
        return any(math.hypot(x - px, y - py) <= reach for x, y in self.hazards)

    def _transit_penalty(self):
        """Ground to keep away from while crossing: around known enemies and where we died."""
        pen, r = {}, self.cfg.transit_avoid_radius
        for e in self.mem.enemies.values():
            if e.alive and not e.lost:
                for i in self.nav.nodes_within(e.xy[0], e.xy[1], r):
                    pen[i] = self.cfg.transit_avoid_factor
        for x, y, until in self.danger:
            if until > self._now:
                for i in self.nav.nodes_within(x, y, self.cfg.danger_radius):
                    pen[i] = self.cfg.transit_avoid_factor
        pen.update(self._hazard_penalty() or {})
        return pen

    def _transit_route(self):
        goal = self.nav.nearest_node(self.goto[0], self.goto[1], allowed=self.rm.reachable)
        if goal is None or self.player_node is None:
            self._goto_route = []
        else:
            self._goto_route = self.nav.path(self.player_node, goal, self._transit_penalty())
        return self._goto_route

    def record_death(self, xy, hazard=False):
        """The party leader died here. Work elsewhere for a while and come back later.
        hazard: nothing hostile was near, so it was the place itself (a trap): keep off that
        spot for good instead of avoiding the whole neighbourhood for a while."""
        self.deaths += 1
        if hazard:
            if not any(math.hypot(xy[0] - x, xy[1] - y) < 200.0 for x, y in self.hazards):
                self.hazards.append((xy[0], xy[1]))
            self.objective = None
            return
        self.danger.append((xy[0], xy[1], self._now + self.cfg.danger_seconds))
        self.objective = None

    def _dangerous(self, pos):
        r2 = self.cfg.danger_radius ** 2
        return any((pos[0] - x) ** 2 + (pos[1] - y) ** 2 <= r2 for x, y, until in self.danger if until > self._now)

    def _candidates(self):
        out = self._all_candidates()
        safe = [o for o in out if o.kind == "carto" or not self._dangerous(o.pos)]
        return safe if any(o.kind != "carto" for o in safe) or not out else out

    def _all_candidates(self):
        if self.goto is not None:
            gx, gy = self.goto
            if math.hypot(gx - self.player_xy[0], gy - self.player_xy[1]) <= 300.0:
                return []
            out = [Objective("goto", ("goto", 0), (gx, gy), self.rm.region_at(gx, gy), 10.0)]
            # Crossing an area: go round the groups we can, and for a group the path cannot
            # avoid, stop short and fight it on its own instead of walking into it.
            route = self._transit_route()
            ahead, left = [], self.cfg.transit_lookahead
            for a, b in zip(route, route[1:]):
                ahead.append(self.nav.nodes[b])
                left -= math.hypot(self.nav.nodes[a][0] - self.nav.nodes[b][0], self.nav.nodes[a][1] - self.nav.nodes[b][1])
                if left <= 0:
                    break
            ahead.append(self.player_xy)
            px, py = self.player_xy
            near2 = self.cfg.transit_block_radius ** 2
            for c in self.clusters:
                if self.mem.blocked.get(c.key, 0) >= self.cfg.max_failures:
                    continue
                spots = [self.mem.enemies[i].xy for i in c.members]
                if not any((sx - x) ** 2 + (sy - y) ** 2 <= near2 for sx, sy in spots for x, y in ahead):
                    continue
                spot = min(spots, key=lambda p: (p[0] - px) ** 2 + (p[1] - py) ** 2)
                obj = Objective("cluster", c.key, spot, self.rm.region_at(*spot), 20.0 + len(c.members), c)
                obj.walk = math.hypot(spot[0] - px, spot[1] - py)
                cur = self.objective
                if cur is not None and cur.kind == "cluster" and cur.target in c.members:
                    obj.key, obj.target, obj.waited = cur.key, cur.target, cur.waited
                out.append(obj)
            return out
        cfg, mem, out = self.cfg, self.mem, []
        blocked = lambda key: mem.blocked.get(key, 0) >= cfg.max_failures
        if self._vanquish_active():
            # once a known route has been walked to its end, whatever is left is leftovers:
            # go straight for every group we know of, wherever it is
            cleanup = self.mode == CLEANUP or (bool(self.guide) and not self.guiding())
            px, py = self.player_xy
            for c in self.clusters:
                if blocked(c.key):
                    continue
                spot = min((mem.enemies[i].xy for i in c.members), key=lambda p: (p[0] - px) ** 2 + (p[1] - py) ** 2)
                # Judge "close" by walking distance, not line of sight: a group up on a ledge can be
                # in plain view and still a long way round. It stays on the list, scored by the real
                # walk, but it cannot interrupt what the party is doing.
                walk = self._walk_distance(spot)
                region = self.rm.region_at(*spot)
                # A known group in ground the sweep has already passed will not be met again on
                # the way: every step onward makes the trip back longer, so deal with it now.
                # One in ground still ahead is left for when the sweep gets there, unless it is
                # a short walk away.
                behind = region >= 0 and mem.region_state(region) == SEARCHED
                if cleanup or behind or walk <= cfg.divert_distance:
                    value = (6.0 if behind else 4.0) + len(c.members)
                    obj = Objective("cluster", c.key, spot, region, value, c)
                    obj.walk = walk
                    # A group's id shifts as members die or two groups merge. If this is the group
                    # we are already fighting, keep the same objective so the walk is not restarted.
                    cur = self.objective
                    if cur is not None and cur.kind == "cluster" and cur.target in c.members:
                        obj.key, obj.target, obj.waited = cur.key, cur.target, cur.waited
                    out.append(obj)
            pending = [rid for rid in self.tour if mem.region_state(rid) != SEARCHED and not blocked(("region", rid))]
            if self.hint_regions and not self.guide:
                hinted = [r for r in pending if r in self.hint_regions]
                self.searching_off_route = not hinted and bool(pending)
                if hinted:
                    pending = hinted         # where the known route goes first; the rest only if foes remain
            if self.guiding():
                # Following a known route: its next point is the plan. The route goes where the
                # enemies are, so the rest of the map is not swept unless foes remain at its end.
                gx, gy = self.guide[self.guide_i]
                out.append(Objective("guide", ("guide", self.guide_i), (gx, gy), self.rm.region_at(gx, gy), 2.5))
                pending = []
            elif self.guide and any(o.kind == "cluster" for o in out):
                pending = []                 # route done and groups still known: those first, searching after
            # the next stop of the round may be a place only the map needs; then no region is "next"
            fogged = {self.rm.node_region[node] for _c, _f, node, _r in self.carto_targets} if self.carto else set()
            self.next_stop = next((r for r in self.tour if mem.region_state(r) != SEARCHED
                                   or (r in getattr(self, "carto_stops", ()) and r in fogged)), None)
            for n, rid in enumerate(pending):
                value = 2.5 if (n == 0 and self.next_stop in (None, rid)) else cfg.off_tour_value   # follow the tour unless something is much closer
                out.append(Objective("region", ("region", rid), mem.unsearched_target(rid), rid, value))
        if cfg.do_cartography and self.carto is not None:
            for cell, footing, node, reveals in self.carto_targets:
                key = ("carto", cell)
                if not blocked(key):
                    obj = Objective("carto", key, footing, self.rm.node_region[node], 0.8, reveals)
                    # Same rule as for enemies: a fogged cell in ground the sweep has already
                    # passed will not be met again, so stand in it now. Returning for it from the
                    # far end of the map costs many times more than the detour does here.
                    behind = obj.region >= 0 and self.mem.region_state(obj.region) == SEARCHED
                    now_cost = self._cost(footing, obj.region)
                    if behind or now_cost <= self.cfg.carto_detour:
                        obj.value = 5.0
                    if self.cfg.carto_in_tour and self._vanquish_active() and not self.guiding():
                        # in the round: the stop whose turn it is goes first; other far corners wait theirs
                        if obj.region == getattr(self, "next_stop", None):
                            obj.value = 6.0
                        elif obj.region in getattr(self, "carto_stops", ()) and now_cost > self.cfg.carto_detour:
                            obj.value = 0.8
                    # A far corner that only the map needs: the cheapest moment to fetch it is
                    # when the rest of the plan brings us as near to it as we will ever be. If no
                    # region still to visit lies closer to it than we stand now, go now - leaving
                    # it for the end means crossing the whole area for a single cell.
                    if self.guiding() and self._vanquish_active():
                        # The route is known in advance, so the best moment for each map cell is
                        # known too: when we reach the route point that passes nearest to it.
                        near = self._guide_near.get(cell)
                        if near is None or near[1] != footing:
                            near = self._guide_near[cell] = (
                                min(range(len(self.guide)), key=lambda i: (self.guide[i][0] - footing[0]) ** 2
                                                                           + (self.guide[i][1] - footing[1]) ** 2), footing)
                        if near[0] <= self.guide_i and now_cost <= self.cfg.carto_route_reach:
                            obj.value = 40.0
                    elif obj.region >= 0 and self._vanquish_active():
                        todo = [r for r in self.tour if self.mem.region_state(r) != SEARCHED]
                        if todo:
                            later = min(self.rm.dist[r][obj.region] for r in todo)
                            if now_cost <= self.cfg.carto_fetch_limit and now_cost <= later * self.cfg.carto_fetch_slack:
                                obj.value = 40.0
                    if (not self._vanquish_active() and self.cfg.carto_leftover_walk > 0
                            and now_cost > self.cfg.carto_leftover_walk):
                        self.carto_skipped_far.add(cell)   # not worth crossing the area for
                        continue
                    out.append(obj)
        return out

    def _score(self, obj):
        cost = obj.walk if obj.walk is not None else self._cost(obj.pos, obj.region)
        return obj.value / (cost + 1500.0)

    def _still_valid(self, obj):
        if obj is None or self.mem.blocked.get(obj.key, 0) >= self.cfg.max_failures:
            return False
        if obj.kind == "goto":
            return math.hypot(obj.pos[0] - self.player_xy[0], obj.pos[1] - self.player_xy[1]) > 300.0
        if obj.kind == "guide":
            return self._vanquish_active() and obj.key[1] == self.guide_i
        if obj.kind == "region":
            if self.guiding():
                return False
            if not self._vanquish_active() or self.mem.region_state(obj.region) == SEARCHED:
                return False
            obj.pos = self.mem.unsearched_target(obj.region)     # aim at what is still unchecked
            return True
        if obj.kind == "cluster":
            if not self._vanquish_active() and self.goto is None:
                return False
            group = next((c for c in self.clusters if obj.target in c.members), obj.data)
            obj.data = group
            alive = [self.mem.enemies[i] for i in group.members
                     if self.mem.enemies[i].alive and not self.mem.enemies[i].lost]
            if not alive:
                return False
            # Walk at one living member (the middle of a spread-out group can be empty ground)
            # and stay on it until it is gone, so two members either side of a wall cannot
            # pull the party back and forth.
            chosen = next((e for e in alive if e.id == obj.target), None)
            if chosen is None:
                px, py = self.player_xy
                chosen = min(alive, key=lambda e: (e.xy[0] - px) ** 2 + (e.xy[1] - py) ** 2)
                obj.target = chosen.id
            obj.pos, obj.region = chosen.xy, self.rm.region_at(*chosen.xy)
            return True
        if obj.kind == "carto":
            if self.grid is None:
                return True
            if all(self.grid.explored(*c) for c in obj.data):
                return False
            # Standing in the cell for a while without credit: give up on those cells.
            if self.carto.proj.cell(*self.player_xy) == obj.key[1]:
                if self._stand_since is None:
                    self._stand_since = self._now
                elif self._now - self._stand_since > self.cfg.carto_patience:
                    lost = {c for c in obj.data if not self.grid.explored(*c)}
                    self.carto.declined |= lost
                    self.note = f"stood in map cell {obj.key[1]} without credit; gave up on {len(lost)} cells"
                    self.carto_given_up += len(lost)
                    self._stand_since = None
                    return False
            else:
                self._stand_since = None
            return True
        return False

    def plan(self):
        """Pick (or keep) the objective. Returns it, or None when there is nothing left to do."""
        if self.mode in (DONE, FAILED):
            return None
        vanquishing = self._vanquish_active()
        if vanquishing:
            counts = self.mem.counts()
            # Cleanup starts when every region has been searched. It used to start whenever few
            # foes "remained", but the game only counts foes found so far, so that number is small
            # at the start of every area too: the mode flipped back and forth and re-planned the
            # visiting order each time, which sent the party off across the map.
            mode = CLEANUP if counts[SEARCHED] == len(self.rm.regions) else SWEEP
            if mode == CLEANUP and self.mode != CLEANUP:
                self._retour()
            self.mode = mode
        else:
            self.mode = EXPLORE
        current = self.objective if self._still_valid(self.objective) else None
        if current is None and vanquishing and self.cfg.retour:
            self._retour()               # fights and detours make the old order stale
        cands = self._candidates()
        if not cands:
            if vanquishing and self.mem.escalation < self.cfg.max_escalations:
                self.mem.tighten_search()
                self._retour()
                self.note = f"nothing left to check with {self.foes_remaining} foes remaining; searching closer"
                cands = self._candidates()
            if not cands and vanquishing and self.fresh_starts < self.cfg.max_fresh_starts:
                # Out of ideas, but leaving throws the whole run away: walk the area again from
                # scratch, including everything that was given up on as unreachable.
                self.fresh_starts += 1
                self.mem.start_over()
                self._retour()
                self.note = (f"{self.foes_remaining} foes remain and nothing is left to check: "
                             f"searching the whole area again ({self.fresh_starts} of {self.cfg.max_fresh_starts})")
                cands = self._candidates()
            if not cands:
                self.objective = None
                self.mode = FAILED if vanquishing else DONE
                return None
        best = max(cands, key=self._score)
        if current is not None and best.key == current.key:
            best = current
        elif current is not None:
            urgent = (best.kind == "cluster" and current.kind != "cluster"
                      and best.walk is not None and best.walk <= self.cfg.interrupt_distance)
            if not urgent and self._score(best) < self.cfg.switch_factor * self._score(current):
                best = current
        self.objective = best
        return best

    def _retour(self):
        """Re-plan the visiting order over what is still unsearched, starting from here."""
        pending = {r for r in range(len(self.rm.regions)) if self.mem.region_state(r) != SEARCHED}
        # Ground that only the map needs is part of the round too. Planned in with the rest, a far
        # corner is visited at the point in the round where it costs least, not after everything.
        self.carto_stops = set()
        if self.cfg.carto_in_tour and self.cfg.do_cartography and self.carto is not None:
            self.carto_stops = {self.rm.node_region[node] for _c, _f, node, _r in self.carto_targets
                                if self.rm.node_region[node] >= 0} - pending
        if self.hint_regions and not self.guide:
            hinted = pending & self.hint_regions
            if hinted:
                pending = hinted
        stops = sorted(pending | self.carto_stops)
        self.tour = self.rm.tour(self.player_region, only=stops)[1:] if stops else []

    def caution(self):
        """0-3: goes up by one for every three deaths in this area. The more the party has died
        here, the less it reaches for (smaller fight radius) and the fuller it rests first."""
        return min(3, self.deaths // 3)

    def in_hazard(self, x, y):
        r2 = self.cfg.hazard_radius ** 2
        return any((x - hx) ** 2 + (y - hy) ** 2 <= r2 for hx, hy in self.hazards)

    def next_step(self):
        """The step to walk, never ending under a known trap: where the way leads through one,
        the step reaches to the first point on the far side so the party crosses without
        stopping. (Only an objective that itself lies under the trap is still walked to.)"""
        step = self._plain_step()
        if step is None or not self.hazards or not self.in_hazard(step[0], step[1]):
            return step
        pts = self.route_ahead
        if not pts:
            return step
        k = min(range(len(pts)), key=lambda i: (pts[i][0] - step[0]) ** 2 + (pts[i][1] - step[1]) ** 2)
        for x, y in pts[k:]:
            if not self.in_hazard(x, y) and not self.nav.in_no_go(x, y):
                return x, y, step[2]
        return step

    def _plain_step(self):
        """Where to walk right now: a point up to `step_length` along the shortest walkable
        path to the objective. Returns (x, y, clear_radius) or None."""
        obj = self.plan()
        if obj is None:
            return None
        radius = max(900.0, self.cfg.engage_radius - 120.0 * self.caution())
        if obj.kind == "cluster":
            # crowded ground: with a dozen or more known enemies round the target, reach for less
            # from the start instead of learning it by dying
            tx, ty = obj.pos
            crowd = sum(1 for e in self.mem.enemies.values()
                        if e.alive and not e.lost and math.hypot(e.xy[0] - tx, e.xy[1] - ty) <= 2200.0)
            if crowd >= 12:
                radius = min(radius, 1000.0)
        goal = self.nav.nearest_node(obj.pos[0], obj.pos[1], allowed=self.rm.reachable)
        here = self.player_node
        if goal is None or here is None:
            return None if self.nav.in_no_go(*obj.pos) else (obj.pos[0], obj.pos[1], radius)
        if obj.kind == "goto":
            route, left = (getattr(self, "_goto_route", None) or self._transit_route()), self.cfg.transit_step_length
        else:
            pen = self._hazard_penalty()
            if self.route_pen:
                if pen:
                    merged = dict(self.route_pen)
                    merged.update(pen)           # trap ground outweighs "off the route"
                    pen = merged
                else:
                    pen = self.route_pen
            route, left = self.nav.path(here, goal, pen), self.cfg.step_length
        if self._hazard_near(left + 600.0):
            left = min(left, 450.0)      # short steps near a trap, so the walk keeps to the path round it
        self.route_ahead = [self.nav.nodes[i] for i in route]
        if not route:
            return None if self.nav.in_no_go(*obj.pos) else (obj.pos[0], obj.pos[1], radius)
        k = 0
        while k + 1 < len(route) and left > 0:
            (ax, ay), (bx, by) = self.nav.nodes[route[k]], self.nav.nodes[route[k + 1]]
            left -= math.hypot(ax - bx, ay - by)
            k += 1
        # The step must actually take the party somewhere. Where the path doubles back over
        # itself (stairs, a ramp down to the level underneath) a point well along it can still
        # be right beside us on the flat, and walking "to" it does nothing: reach further along
        # until the point is a real distance away.
        px, py = self.player_xy
        extra = 2500.0
        while (k + 1 < len(route) and extra > 0
               and math.hypot(self.nav.nodes[route[k]][0] - px, self.nav.nodes[route[k]][1] - py) < 350.0):
            (ax, ay), (bx, by) = self.nav.nodes[route[k]], self.nav.nodes[route[k + 1]]
            extra -= math.hypot(ax - bx, ay - by)
            k += 1
        if obj.kind == "cluster":
            # Do not walk into the middle of a group (that drags in its neighbours):
            # stop at the last point on the path that is still `standoff` away from it.
            # If the group has not come after several plans of waiting there, close in.
            tx, ty = obj.pos
            standoff = self.cfg.standoff if obj.waited < self.cfg.standoff_patience else 0.0
            if standoff and obj.data is not None and len(getattr(obj.data, 'members', ())) >= self.cfg.crowd_size:
                standoff += 400.0            # a big group: stop further out, so it has to come to us
            for j in range(k, -1, -1):
                x, y = self.nav.nodes[route[j]]
                if math.hypot(x - tx, y - ty) >= standoff or j == 0:
                    if standoff and math.hypot(x - self.player_xy[0], y - self.player_xy[1]) < 250.0:
                        obj.waited += 1
                    if standoff:
                        return x, y, radius
                    break
        if k == len(route) - 1 and not self.nav.in_no_go(*obj.pos):
            return obj.pos[0], obj.pos[1], radius
        x, y = self.nav.nodes[route[k]]      # an objective inside a no-go zone is approached, not entered
        return x, y, radius

    def add_wall(self, player_xy, target):
        """The party was pinned here while walking at `target`: something the ground data does
        not show is in the way. Route around it from now on."""
        dx, dy = target[0] - player_xy[0], target[1] - player_xy[1]
        d = math.hypot(dx, dy) or 1.0
        r = min(350.0, d)
        spot = (player_xy[0] + dx / d * r, player_xy[1] + dy / d * r)
        walls = self.walls
        if any(math.hypot(spot[0] - x, spot[1] - y) < 250.0 for x, y in walls):
            return None
        walls.append(spot)
        self.player_node = None
        self.start_detour()
        return spot

    def start_detour(self, points=12):
        """Pinned by something the ground data does not show. If the area has a known route, its
        authors found the way that is really open: pick the route up at its nearest point and
        follow it for a stretch, then go back to planning freely."""
        if not self._route_src or self.goto is not None or self.detours >= 8:
            return False
        if self.guide and not self._detour:
            return False                     # already following the whole route point by point
        pts = self._prepare_guide(self._route_src, self.player_xy)
        while pts and any(math.hypot(pts[0][0] - x, pts[0][1] - y) < 600.0 for x, y in self.walls):
            pts = pts[1:]
        pts = pts[:points]
        if len(pts) < 2:
            return False
        for k in [k for k in self.mem.blocked if k[0] == "guide"]:
            del self.mem.blocked[k]
        self.guide, self.guide_i, self._guide_near = pts, 0, {}
        self._detour, self.objective = True, None
        self.detours += 1
        return True

    def step_failed(self):
        """The runtime could not reach the last step."""
        if self.objective is not None:
            self.mem.blocked[self.objective.key] = self.mem.blocked.get(self.objective.key, 0) + 1
            self.objective = None

    # ---- reporting ----
    def status(self):
        counts = self.mem.counts()
        live = self.mem.live_enemies()
        return {
            "mode": self.mode,
            "objective": self.objective.label() if self.objective else "-",
            "foes_remaining": self.foes_remaining,
            "regions": len(self.rm.regions),
            "searched": counts[SEARCHED], "seen": counts[SEEN], "unknown": counts[UNKNOWN],
            "clusters": len(self.clusters),
            "known_alive": len(live),
            "fog_cells": sum(len(t[3]) for t in self.carto_targets),
            "blocked": [k for k, v in self.mem.blocked.items() if v >= self.cfg.max_failures],
            "walked": round(self.mem.walked),
            "escalation": self.mem.escalation,
            "deaths": self.deaths,
            "carto_stand_cells": len(self.carto.stand) if self.carto else 0,
            "carto_given_up": self.carto_given_up,
            "route_points": len(self.guide), "route_done": self.guide_i,
            "route_regions": len(self.hint_regions), "searching_off_route": bool(getattr(self, "searching_off_route", False)),
            "carto_skipped_far": len(self.carto_skipped_far),
            "danger_zones": sum(1 for d in self.danger if d[2] > self._now),
            "note": self.note,
        }
