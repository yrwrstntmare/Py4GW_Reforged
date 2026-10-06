"""Build data/world.json (which door leads where) from the in-game export. Offline tool."""
import json, math, collections, sys
d = {int(k): v for k, v in json.load(open('gwamm_tools/data/world_dump_v2.json')).items()}
travel = {int(k): v for k, v in json.load(open('Sources/gwamm/data/travel.json')).items()}
G = 3072.0
def frame(v):
    ic = v['icon'] if any(v['icon']) else v['icon_dupe']
    if not any(ic) or 'bounds' not in v: return None
    W = (ic[2]-ic[0])*96.0; H = (ic[3]-ic[1])*96.0
    bx0, by0, bx1, by1 = v['bounds']
    min_x = math.floor(bx0/G)*G
    if min_x+W < bx1-1: min_x = math.ceil(bx1/G)*G-W
    max_y = math.ceil(by1/G)*G
    if max_y-H > by0+1: max_y = math.floor(by0/G)*G+H
    return min_x, max_y, ic[0], ic[1]
def to_wm(f, x, y): return (f[2]+(x-f[0])/96.0, f[3]+(f[1]-y)/96.0)
def to_game(f, wx, wy): return (f[0]+(wx-f[2])*96.0, f[1]-(wy-f[3])*96.0)

groups = collections.OrderedDict()
for m, v in d.items():
    f = frame(v)
    if not f or 'piece_sizes' not in v: continue
    key = (tuple(v['icon']), tuple(round(b) for b in v['bounds']), v['trapezoids'])
    groups.setdefault(key, {'maps': [], 'v': v, 'f': f, 'cont': v['continent']})['maps'].append(m)
G_list = list(groups.values())
for gi, g in enumerate(G_list): g['id'] = gi

# door points
pts = []   # dict(g, piece, xy, wm, tag, kind)
for g in G_list:
    v, f = g['v'], g['f']
    for li, (lst, pcs) in enumerate(zip(v['spawns'][:2], v['spawn_piece'][:2])):
        for (x, y, tag), pc in zip(lst, pcs):
            if pc < 0: continue
            pts.append(dict(g=g['id'], piece=pc, xy=(x, y), wm=to_wm(f, x, y), tag=tag, kind='spawn%d' % (li+1)))
    for (x, y), pc in zip(v['portals'], v['portal_piece']):
        if pc < 0: continue
        pts.append(dict(g=g['id'], piece=pc, xy=(x, y), wm=to_wm(f, x, y), tag='', kind='portal'))

# cross-file links: mutual nearest within T world-map units
T = 45.0
by_cont = collections.defaultdict(list)
for i, p in enumerate(pts): by_cont[G_list[p['g']]['cont']].append(i)
def nearest_other(i):
    p = pts[i]; best = (1e9, -1)
    for j in by_cont[G_list[p['g']]['cont']]:
        q = pts[j]
        if q['g'] == p['g']: continue
        dd = math.dist(p['wm'], q['wm'])
        if dd < best[0]: best = (dd, j)
    return best
near = [nearest_other(i) for i in range(len(pts))]
links = {}   # (g,piece) -> {(g2,piece2): [ (door xy on this side, gap) ]}
for i, (dd, j) in enumerate(near):
    if j < 0 or dd > T: continue
    a, b = pts[i], pts[j]
    links.setdefault((a['g'], a['piece']), {}).setdefault((b['g'], b['piece']), []).append((a, b, dd))
# internal links: points in different pieces of one file within 2500 game units
for i, a in enumerate(pts):
    for j, b in enumerate(pts):
        if a['g'] == b['g'] and a['piece'] != b['piece'] and math.dist(a['xy'], b['xy']) < 2500:
            links.setdefault((a['g'], a['piece']), {}).setdefault((b['g'], b['piece']), []).append((a, b, 0.0))

def gname(g): return '/'.join(d[m]['name'] for m in G_list[g]['maps'])
print('files', len(G_list), 'door points', len(pts), 'pieces with links', len(links), 'links', sum(len(v) for v in links.values()))

# ---- label pieces with map ids ------------------------------------------------
gid_of = {m: g['id'] for g in G_list for m in g['maps']}
EXPLORABLE = 2
label, conflicts, unresolved = {}, [], []
def set_label(node, mid, why):
    if d.get(mid, {}).get('type') != EXPLORABLE: return
    if node in label and label[node] != mid: conflicts.append((node, label[node], mid, why))
    label.setdefault(node, mid)

# outposts standing inside a piece: their own numbered arrival points (second list) are there
outposts_in = collections.defaultdict(set)
outpost_xy = {}
for g in G_list:
    v = g['v']
    for (x, y, tag), pc in zip(v['spawns'][1], v['spawn_piece'][1]):
        if tag.isdigit() and pc >= 0 and int(tag) in d and d[int(tag)]['type'] != EXPLORABLE:
            outposts_in[(g['id'], pc)].add(int(tag))
            outpost_xy.setdefault(int(tag), [round(x), round(y)])
piece_of_outpost = {o: node for node, os_ in outposts_in.items() for o in os_}

def follow(g, xy, from_piece=None, r=3000):
    """(piece we leave, piece we arrive in) for an exit at xy. `from_piece` is where we are
    known to be standing; recorded exit points often lie just past the door, so the nearest
    door point can already be on the far side."""
    cand = sorted((p for p in pts if p['g'] == g and math.dist(p['xy'], xy) < r), key=lambda p: math.dist(p['xy'], xy))
    if not cand: return None, None
    if from_piece is not None:
        if cand[0]['piece'] != from_piece:
            return (g, from_piece), (g, cand[0]['piece'])
        cand = [p for p in cand if p['piece'] == from_piece]
    for p in cand:
        for dest, lst in links.get((p['g'], p['piece']), {}).items():
            for a, b, dd in lst:
                if a is p: return (p['g'], p['piece']), dest
    return (g, cand[0]['piece']), None

gates = {}      # outpost id -> dict(node, path) : the recorded walk out of the outpost into its area
for mid, t in travel.items():
    cur_map, cur_xy = t['outpost'], (t['outpost_path'][-1] if t['outpost_path'] else None)
    chain = [(leg['map'], leg['path'][-1] if leg['path'] else None) for leg in t['transit']] + [(mid, None)]
    first, cur_node = True, None
    for nxt_map, nxt_exit in chain:
        if cur_map not in gid_of or cur_xy is None: unresolved.append((mid, t['file'], 'no data for %s' % cur_map)); break
        known = piece_of_outpost.get(cur_map) if first else cur_node
        src, dest = follow(gid_of[cur_map], tuple(cur_xy), known[1] if known and known[0] == gid_of[cur_map] else None)
        if dest is None:
            # no separate piece on the far side: the gate is inside one walkable piece
            dest = src if src is not None else known
            if dest is None: unresolved.append((mid, t['file'], 'cannot place exit of %s' % d[cur_map]['name'])); break
        if first:
            if not any(x['node'] == dest and x['to_map'] == nxt_map for x in gates.get(cur_map, [])):
                gates.setdefault(cur_map, []).append(dict(to_map=nxt_map, node=dest, path=t['outpost_path']))
        else:
            set_label(src, cur_map, t['file'])
        set_label(dest, nxt_map, t['file'])
        cur_map, cur_xy, first, cur_node = nxt_map, nxt_exit, False, dest
# a file holding exactly one explorable map: its biggest unlabelled piece is that map
for g in G_list:
    ex = [m for m in g['maps'] if d[m]['type'] == EXPLORABLE]
    if len(ex) == 1 and ex[0] not in label.values():
        sizes = g['v']['piece_sizes']; big = max(range(len(sizes)), key=lambda i: sizes[i])
        if (g['id'], big) not in label: label[(g['id'], big)] = ex[0]
print('labelled pieces', len(label), 'conflicts', len(conflicts), 'unresolved travel entries', len(unresolved))
for c in conflicts[:12]: print('  CONFLICT', gname(c[0][0]), 'piece', c[0][1], d[c[1]]['name'], 'vs', d[c[2]]['name'], c[3])
for u in unresolved[:60]: print('  UNRESOLVED', u)

# ---- write world.json ---------------------------------------------------------
def nid(node): return f"{node[0]}:{node[1]}"
nodes = {}
for node in set(links) | set(label) | set(outposts_in):
    g = G_list[node[0]]
    doors = []
    for dest, lst in links.get(node, {}).items():
        a, b, dd = min(lst, key=lambda t: t[2])
        # where the far side's arrival point would be in THIS map's coordinates: walk at it to cross
        beyond = to_game(g['f'], *b['wm']) if dest[0] != node[0] else b['xy']
        px = [p for p in pts if p['g'] == node[0] and p['piece'] == node[1] and p['kind'] == 'portal' and math.dist(p['xy'], a['xy']) < 2500]
        doors.append(dict(to=nid(dest), xy=[round(v) for v in a['xy']], beyond=[round(v) for v in beyond],
                          portal=[round(v) for v in px[0]['xy']] if px else None, tag=a['tag']))
    nodes[nid(node)] = dict(map=label.get(node), file_maps=g['maps'], outposts=sorted(outposts_in.get(node, [])),
                            size=g['v']['piece_sizes'][node[1]], doors=doors)
out = dict(nodes=nodes,
           area_node={str(m): nid(n) for n, m in label.items()},
           outpost_node={str(o): nid(n) for o, n in piece_of_outpost.items()},
           gates={str(o): [dict(to_map=x['to_map'], node=nid(x['node']), path=x['path']) for x in lst] for o, lst in gates.items()},
           outpost_xy={str(o): xy for o, xy in outpost_xy.items()},
           names={str(m): v['name'] for m, v in d.items()},
           explorable=[m for m, v in d.items() if v['type'] == EXPLORABLE and v['flags'] & 0x10000000])
json.dump(out, open('Sources/gwamm/data/world.json', 'w'))
print('world.json: nodes', len(nodes), 'areas placed', len(out['area_node']), 'outposts placed', len(out['outpost_node']), 'gates', len(gates), 'vanquishable maps', len(out['explorable']))
missing = [d[m]['name'] for m in out['explorable'] if str(m) not in out['area_node']]
print('vanquishable areas not placed:', len(missing), missing)
