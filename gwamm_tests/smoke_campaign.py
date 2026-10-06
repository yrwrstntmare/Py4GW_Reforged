"""Runs the campaign's per-area logic against stand-in game modules (no game needed).
python tests/smoke_campaign.py"""
import sys, types, tempfile, enum, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
d = tempfile.mkdtemp()
def mod(name, **kw):
    m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m; return m
mod('PySystem', Console=types.SimpleNamespace(get_projects_path=lambda: d))
class NS(enum.Enum): RUNNING = 1; SUCCESS = 2; FAILURE = 3
class Node:
    def __init__(self, name="", node_type="", node_category=""): self.name = name; self.blackboard = {}
    def tick(self): return self._tick_impl()
    def reset(self): pass
class BTree:
    NodeState = NS; Node = Node
    def __init__(self, root): self.root = root
    def reset(self): pass
    class ActionNode(Node):
        def __init__(self, action_fn=None, aftercast_ms=0, name=""): Node.__init__(self, name); self.fn = action_fn
        def _tick_impl(self): return self.fn()
    class SubtreeNode(Node):
        def __init__(self, subtree_fn=None, name=""): Node.__init__(self, name); self.fn = subtree_fn; self.t = None
        def _tick_impl(self):
            if self.t is None: self.t = self.fn(self)
            return self.t.root.tick()
mod('Py4GWCoreLib'); mod('Py4GWCoreLib.py4gwcorelib_src'); mod('Py4GWCoreLib.py4gwcorelib_src.BehaviorTree', BehaviorTree=BTree)
class Fake(Node):
    def __init__(self, label, ticks, result, effect=None): super().__init__(label); self.n = ticks; self.result = result; self.effect = effect
    def _tick_impl(self):
        self.n -= 1
        if self.n > 0: return NS.RUNNING
        if self.effect: self.effect()
        return self.result
class Seq(Node):
    def __init__(self, kids): super().__init__('seq'); self.k = [k.root if isinstance(k, BTree) else k for k in kids]; self.i = 0
    def _tick_impl(self):
        while self.i < len(self.k):
            st = self.k[self.i].tick()
            if st != NS.SUCCESS: return st
            self.i += 1
        return NS.SUCCESS
W = {'map': 155, 'expl': False, 'vq': set(), 'unlocked': {155, 642}, 'bar': '', 'owned': 0, 'heroes': [], 'sec': 1, 'xy': (0.0, 0.0)}
calls = []
def arrive(m, expl):
    W.update(map=m, expl=expl)
    if not expl: W['unlocked'].add(m)
def Sequence(name, map_id_or_name=0, hard_mode=None, children=None, **k):
    kids = list(children or [])
    if map_id_or_name: kids = [Fake('travel', 2, NS.SUCCESS, lambda m=map_id_or_name: (calls.append(('travel', m)), arrive(m, False)))] + kids
    return BTree(Seq(kids))
def MoveAndExitMap(path, target_map_id=0, timeout_ms=0, log=False): return BTree(Fake('x', 1, NS.SUCCESS, lambda: (calls.append(('gate', target_map_id)), arrive(target_map_id, True))))
def Resign(**k): return BTree(Fake('resign', 2, NS.SUCCESS, lambda: (calls.append(('resign', k.get('target_map_id'))), arrive(k['target_map_id'], False))))
def Wait(duration_ms=0, **k): return BTree(Fake('wait', 1, NS.SUCCESS))
def Travel(target_map_id=0, hard_mode=None, **k): return BTree(Fake('t', 1, NS.SUCCESS, lambda: (calls.append(('travel', target_map_id)), arrive(target_map_id, False))))
def MoveAndDialog(pos=None, dialog_id=0, **k): return BTree(Fake('dlg', 1, NS.SUCCESS, lambda: calls.append(('trainer',))))
mod('Sources.ApoSource'); mod('Sources.ApoSource.ApoBottingLib')
w = mod('Sources.ApoSource.ApoBottingLib.wrappers', Sequence=Sequence, MoveAndExitMap=MoveAndExitMap, Resign=Resign, Wait=Wait, Travel=Travel, MoveAndDialog=MoveAndDialog)
sys.modules['Sources.ApoSource.ApoBottingLib'].wrappers = w
import Sources.gwamm.runtime as rt
from Sources.gwamm.core import builds as B
def signets_on(bar):
    dec = B.decode_template(bar) if bar else None
    return min(dec[2].count(3), W['owned']) if dec else 0
def load_bar(t): W['bar'] = t; calls.append(('load bar', (B.decode_template(t) or (0, 0, []))[2].count(3), 'signets in template'))
game = mod('Sources.gwamm.runtime.game',
    map_ready=lambda: True, map_id=lambda: W['map'], is_explorable=lambda: W['expl'], vanquished_ids=lambda: W['vq'],
    map_unlocked=lambda m: m in W['unlocked'], map_name=lambda m: f'map{m}', player_professions=lambda: (6, W['sec']),
    skill_learnt=lambda s: False, bar_template=lambda: W['bar'], load_bar_template=load_bar,
    capture_signets=lambda: signets_on(W['bar']), buy_signet=lambda: (W.update(owned=W['owned'] + 1), calls.append(('buy signet',))),
    map_world_pos=lambda m: (m, m, 1), player_xy=lambda: W['xy'], max_party_size=lambda: 8, map_max_party=lambda m: 8, party_count=lambda: 8, shrink_party=lambda: None,
    party_hero_ids=lambda: list(W['heroes']), kick_all_heroes=lambda: (W.update(heroes=[]), calls.append(('kick heroes',))),
    add_hero=lambda h: W['heroes'].append(h), load_hero_template=lambda p, t: calls.append(('hero bar', p)),
    unlocked_secondaries=lambda: {1, 2, 3, 4, 5}, template_with_secondary=lambda t, s: t, template_secondary=lambda t: 1,
    change_secondary_direct=lambda p: W.update(sec=p), find_toolbox_file=lambda name='': '',
    bar_with_signets=lambda t, n, slots, secondary=None: t, template_signets=lambda t: 0)
rt.game = game
class AN(Node):
    def __init__(self, session, target_map_id=None, transit=None, arrive_map=None): super().__init__('adaptive'); self.n = 2; self.s = session; self.tr = transit; self.t = target_map_id
    def _tick_impl(self):
        self.n -= 1
        if self.n > 0: return NS.RUNNING
        calls.append(('cross area' if self.tr else 'VANQUISH', self.t))
        if not self.tr: W['vq'].add(self.t)
        self.s.result = 'arrived' if self.tr else 'complete'; return NS.SUCCESS
class CN(Node):
    def __init__(self, session, leg, from_map, timeout_s=0): super().__init__('cross'); self.leg = leg; self.f = from_map
    def _tick_impl(self):
        dest = self.leg.get('expect'); calls.append(('door', self.f, '->', dest)); arrive(dest, dest not in (156, 159, 155)); return NS.SUCCESS
mod('Sources.gwamm.runtime.node', AdaptiveNode=AN, CrossNode=CN)
import Sources.gwamm.runtime.campaign as C

def run(map_id, herobuilds_path=None):
    c = C.Campaign()
    if herobuilds_path:
        c.import_toolbox(herobuilds_path); print('import:', c.import_note)
        print('slots:', {k: v['name'] for k, v in sorted(c.builds.items())})
    events = []
    sess = types.SimpleNamespace(result='', cfg=types.SimpleNamespace(capture_elites=True, buy_signets=True, unlock_outposts=True, change_secondary=True),
                                 log=types.SimpleNamespace(event=lambda *a, **k: events.append((a, k))))
    n = C.MapRunNode(sess, c, map_id); t = 0
    while True:
        t += 1; st = n.tick()
        if st != NS.RUNNING or t > 400: break
    return c, st, t, events

if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else None
    c, st, t, events = run(93, path)
    print('ticks', t, st, c.results.get(93), '| now in map', W['map'], '| heroes', W['heroes'], '| signets owned', W['owned'])
    for x in calls: print('  ', x)
    for a, k in events:
        if k.get('phase') in ('team', 'secondary'): print('  log:', k)
