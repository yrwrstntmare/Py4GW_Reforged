"""
GWAMM Vanquish  -  adaptive vanquish + cartography for Py4GW Reforged.

Install: copy the `Sources/gwamm` folder into your Py4GW `Sources` folder and load
this script. Enter an explorable area in hard mode with your party ready, then
press Start in the bot window.

Until you press Start it only observes: it builds the map model, tracks enemies
and shows what it WOULD do next. Nothing moves.

Not handled yet (do these by hand): travelling to the area, loading the team build,
changing secondary profession, and using Signet of Capture on a boss.
"""
import PyImGui
import PySystem

from Py4GWCoreLib.BottingTree import BottingTree
from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Py4GWCoreLib.py4gwcorelib_src.Settings import Settings

SCRIPT_VERSION = "0.50.20"
INSTALL_PROBLEM = ""

# Py4GW keeps imported packages in memory when a script is reloaded, so without this an
# updated gwamm folder is ignored until Guild Wars is restarted. Drop the old copy first.
import importlib
import sys
import time
for _name in [n for n in sys.modules if n == "Sources.gwamm" or n.startswith("Sources.gwamm.")]:
    del sys.modules[_name]
importlib.invalidate_caches()

try:
    from Sources.gwamm import __version__
    if __version__ != SCRIPT_VERSION:
        INSTALL_PROBLEM = (f"Version mismatch: this script is {SCRIPT_VERSION} but the Sources/gwamm folder is "
                           f"{__version__}. Replace the whole Sources/gwamm folder with the one from the same zip.")
except ImportError:
    __version__ = "?"
    INSTALL_PROBLEM = ("Could not load a matching Sources/gwamm folder. Replace the whole Sources/gwamm folder "
                       "with the one from the same zip. If you already did, restart Guild Wars once.")

if not INSTALL_PROBLEM:
    from Sources.gwamm.core import elites
    from Sources.gwamm.core.memory import SEARCHED, SEEN
    from Sources.gwamm.runtime import game
    from Sources.gwamm.runtime.node import AdaptiveNode
    from Sources.gwamm.runtime.session import Session
    from Sources.gwamm.runtime.campaign import Campaign, MapRunNode
    from Sources.gwamm.runtime.worlddump import WorldDump, GeometryDump


MODULE_NAME = "GWAMM Vanquish"
INI_PATH = "Widgets/Automation/Bots/Vanquish/GWAMM"
INI_FILENAME = "GWAMM_Vanquish.ini"

session = None if INSTALL_PROBLEM else Session()
campaign = None
world_dump = None if INSTALL_PROBLEM else WorldDump()


def _geometry_ids():
    c = get_campaign()
    ids = set(c.travel)
    if c.world is not None:
        ids |= set(c.world.vanquishable)
    return {int(m) for m in ids}


geo_dump = None if INSTALL_PROBLEM else GeometryDump(_geometry_ids)
built_queue = None            # the queue the current tree was built for
botting_tree = None
initialized = False
ini_key = ""


def _col(r, g, b, a=255):
    return (a << 24) | (b << 16) | (g << 8) | r


COL_GROUND = _col(70, 70, 80)
COL_UNKNOWN = _col(200, 60, 60)
COL_SEEN = _col(230, 170, 50)
COL_SEARCHED = _col(70, 190, 90)
COL_TOUR = _col(120, 120, 255, 110)
COL_WALKED = _col(255, 255, 255, 200)
COL_AHEAD = _col(255, 80, 255)
COL_PLAYER = _col(60, 255, 90)
COL_ENEMY = _col(255, 70, 70)
COL_STALE = _col(255, 150, 60)
COL_BOSS = _col(255, 220, 40)
COL_FOG = _col(80, 200, 255, 70)
COL_FOG_EDGE = _col(80, 200, 255, 200)


class View:
    zoom = 1.0
    pan = [0.0, 0.0]
    prev_mouse = None
    follow = True
    show_ground = True
    show_regions = True
    show_tour = False
    show_fog = True
    sized = False
    main_sized = False
    show_reforged = False
    show_map = True
    pc_counts = {}
    last_dump = ""
    last_error = ""
    search = ""
    build_note = ""


_reported = set()


def report(where):
    """Record an exception once per distinct traceback: console, run log, and the status window."""
    import traceback
    text = traceback.format_exc()
    View.last_error = f"{where}: {text.strip().splitlines()[-1]}"
    if text not in _reported:
        _reported.add(text)
        PySystem.Console.Log(MODULE_NAME, f"{where} failed:\n{text}", PySystem.Console.MessageType.Error)
        try:
            session.log.event("error", where=where, traceback=text)
        except Exception:
            pass


def safe(where, fn):
    def run():
        try:
            fn()
        except Exception:
            report(where)
    return run


# ---- bot wiring -----------------------------------------------------------

def get_campaign():
    global campaign
    if campaign is None:
        campaign = Campaign()
    return campaign


_slot_trees = {}


def _keep_old(slot, tree):
    """Reforged rebuilds a step after a party wipe. The step's old tree may still have a walk whose
    route Py4GW is working out on the game's thread; freeing it then crashes the game when the
    answer is written (Py4GW.dll+0x17d2a2, after wipes, three times). Park the old tree instead."""
    old = _slot_trees.get(slot)
    if old is not None and old is not tree:
        try:
            from Sources.gwamm.runtime.node import retire
            retire(old)
        except Exception:
            pass
    _slot_trees[slot] = tree
    return tree


def _steps():
    def prepare():
        return BehaviorTree(BehaviorTree.SequenceNode(
            name="Prepare",
            children=[botting_tree.Config.Multibox_Aggressive(auto_loot=True) if session.cfg.multibox
                      else botting_tree.Config.Aggressive(multi_account=False, auto_loot=True)],
        ))

    steps = [("Prepare", prepare)]
    queue = list(get_campaign().queue)
    if not queue:
        # Nothing queued: vanquish the area the party is standing in.
        # Run as a one-area campaign when we can, so the walk into a locked outpost afterwards
        # happens here too; the party is left where it ends up instead of resigning.
        def single():
            try:
                mid = game.map_id()
                if game.is_explorable() and session.cfg.do_vanquish and mid not in game.vanquished_ids():
                    return _keep_old("single", BehaviorTree(MapRunNode(session, get_campaign(), mid, stay=True)))
            except Exception:
                pass
            return _keep_old("single", BehaviorTree(AdaptiveNode(session)))
        steps.append(("Adaptive vanquish", single))
    # One slot per queued area. Which area a slot runs is decided when the slot starts, so an
    # outpost unlocked by an earlier area can change what comes next.
    def area(slot):
        c = get_campaign()
        mid = c.pick_next(slot)
        if mid is None:
            return BehaviorTree(BehaviorTree.ActionNode(name="Nothing left", action_fn=lambda: BehaviorTree.NodeState.SUCCESS))
        session.log.event("campaign", phase="next_area", slot=slot, map_id=mid, name=game.map_name(mid),
                          hops=c.hops(mid), left=[m for m in c.queue if m != mid])
        return _keep_old(slot, BehaviorTree(MapRunNode(session, c, mid)))
    for slot in range(len(queue)):
        steps.append((f"Area {slot + 1} of {len(queue)}", lambda slot=slot: area(slot)))
    if get_campaign().exit_when_done:
        def leave():
            import time as _t
            t0 = [0.0]

            def close():
                t0[0] = t0[0] or _t.time()
                if _t.time() - t0[0] < 8.0:                 # let the last map load settle first
                    return BehaviorTree.NodeState.RUNNING
                session.log.event("campaign", phase="exit_game", note=game.exit_game())
                return BehaviorTree.NodeState.SUCCESS
            return BehaviorTree(BehaviorTree.ActionNode(name="Close the game", action_fn=close))
        steps.append(("Exit game", leave))
    return steps


def ensure_tree():
    """(Re)build the bot's step list whenever the queue has changed and the bot is not running."""
    global botting_tree, built_queue
    queue = list(get_campaign().queue) + (["exit"] if get_campaign().exit_when_done else []) + (["multibox"] if session.cfg.multibox else [])
    session.save_options_if_changed()
    if botting_tree is None or not botting_tree.IsStarted():
        session.refresh_party()
    if botting_tree is not None and not botting_tree.IsStarted():
        get_campaign().__dict__["_plan"] = {}        # not running: the next Start plans afresh
    if botting_tree is not None and queue != built_queue and not botting_tree.IsStarted():
        botting_tree.Stop()
        botting_tree = None
    if botting_tree is None:
        built_queue = queue
        botting_tree = BottingTree.Create(
            MODULE_NAME,
            main_routine=_steps(),
            routine_name="GWAMMVanquish",
            repeat=False,
            reset=False,
            multi_account=bool(session.cfg.multibox),
            auto_loot=True,
            configure_fn=lambda tree: tree.Config.ConfigureUpkeep(),
        )
    return botting_tree


# ---- UI -------------------------------------------------------------------

def _line(label, text, colour=None):
    """One 'LABEL  value' row; the label dimmed so the values are what the eye lands on."""
    try:
        PyImGui.text_colored(label, (0.62, 0.66, 0.72, 1.0))
        PyImGui.same_line(0.0, -1.0)
    except Exception:
        text = f"{label} {text}"
    if colour is not None:
        try:
            PyImGui.text_colored(text, colour)
            return
        except Exception:
            pass
    PyImGui.text_wrapped(text)


GOOD, WARN, BAD = (0.35, 1.0, 0.45, 1.0), (1.0, 0.85, 0.3, 1.0), (1.0, 0.45, 0.4, 1.0)


def draw_summary():
    """Always on show at the top of the bot window: the few things worth a glance."""
    eng = session.engine
    if View.last_error:
        _line("ERROR", View.last_error, BAD)
    if session.error:
        _line("ERROR", session.error, BAD)
    if eng is None:
        _line("MAP", "waiting for the map to load")
        return
    st = eng.status()
    foes = st["foes_remaining"]
    if foes is None:
        _line("FOES", "this is not a hard-mode vanquish area (exploring only)", WARN)
    else:
        _line("FOES", f"{foes} left, {game.foes_killed()} killed", GOOD if foes == 0 else None)
    _line("DOING", f"{st['objective']}" + (f"   (resting: {session.resting})" if session.resting else ""))
    _line("MAP", f"{st['searched']} of {st['regions']} regions searched, {st['fog_cells']} fogged cells left")
    if st["deaths"]:
        _line("DEATHS", str(st["deaths"]), WARN if st["deaths"] < 6 else BAD)
    if session.result:
        _line("RESULT", session.result, GOOD if session.result == "complete" else WARN)


def draw_status():
    """The Run tab: everything about the area in progress, grouped."""
    eng = session.engine
    if eng is None:
        PyImGui.text("Nothing to show until a map is loaded.")
        return
    st = eng.status()
    if st["note"]:
        PyImGui.text_wrapped(st["note"])
    if getattr(session, "resumed", ""):
        _line("RESUMED", session.resumed)

    if PyImGui.collapsing_header("Elites in this area", PyImGui.TreeNodeFlags.DefaultOpen):
        for cap in session.captures[-4:]:
            _line("CAPTURED" if cap["result"] == "captured" else "CAPTURE", f"{cap['boss']} - {cap['result']}")
        if session.targets:
            have = game.capture_signets()
            why = {3.0: "only here", 2.0: "elsewhere only in done areas", 1.0: "also in an area still to do"}
            for i, e in enumerate(session.targets[:5]):
                PyImGui.text_wrapped(f"{i + 1}. {e.name} from {e.boss} ({why.get(e.scarcity, '')}"
                                     f"{'' if e.guaranteed else '; may not spawn'})")
            PyImGui.text(f"Signets on your bar: {have}. It goes for the top {have}.")
        elif session.elite_plan is not None:
            PyImGui.text("None you still need.")
        else:
            PyImGui.text_wrapped("No boss list for this area; any boss of your professions is tried.")

    if PyImGui.collapsing_header("Party and consumables", PyImGui.TreeNodeFlags.DefaultOpen):
        p = getattr(session, "party", None) or game.party_makeup()
        _line("PARTY", f"you + {max(0, p['players'] - 1)} other player(s), {p['heroes']} hero(es), {p['henchmen']} henchmen")
        if session.cfg.multibox:
            _line("SHARED", session.party_note or f"{len(game.party_accounts_cached())} of the other accounts run Py4GW")
        if session.cfg.pcons_on:
            _line("CONSUMABLES", f"{session.pcons.note or '-'} ({session.pcons.used} used this session"
                                 + (f", last {session.pcons.last})" if session.pcons.last else ")"))
            _line("MORALE", f"yours {game.my_morale()}, party lowest {game.party_low_morale()} (100 is no penalty, 40 ends the run)")
        else:
            _line("CONSUMABLES", "off (Options tab)")

    if PyImGui.collapsing_header("Progress in detail"):
        _line("MODE", st["mode"])
        _line("REGIONS", f"{st['searched']} searched, {st['seen']} seen, {st['unknown']} unknown of {st['regions']}")
        _line("ENEMY GROUPS", f"{st['clusters']} known ({st['known_alive']} enemies)")
        _line("WALKED", str(st["walked"]))
        if eng.carto is not None:
            _line("MAP CELLS", f"{st['carto_stand_cells']} standable, {st['fog_cells']} fogged, {st['carto_given_up']} given up")
        if st["escalation"]:
            _line("SEARCH", f"tightened {st['escalation']}x")
        if st["blocked"]:
            _line("UNREACHABLE", f"{len(st['blocked'])} objectives given up")
        if eng._deferred:
            _line("FENCED CELLS", f"{len(eng._deferred)} map cell(s) beside an exit, left until the last foe is down")
        if getattr(eng, "no_path_given_up", 0):
            _line("NO PATH", f"{eng.no_path_given_up} far objective(s) dropped: no walkable way to them is known")
        if getattr(eng, "walls", None):
            _line("BLOCKED SPOTS", f"{len(eng.walls)} found, {eng.detours} detour(s) along the known route")
        if st["deaths"]:
            _line("DEATHS", f"{st['deaths']}, avoiding {st['danger_zones']} spots for now")
        if st["patrols"]:
            _line("PATROLS", f"{st['patrols']} enemies seen walking a beat (their paths are kept and checked before a fight is started)")
        if st["wipe_zones"]:
            _line("PARTY WIPES", f"{st['wipes']} at {st['wipe_zones']} place(s): left for last and walked round (red rings on the map)", BAD)
        if session.stalls:
            _line("STALLS", f"{session.stalls} recovered")
        if getattr(session, "route_note", ""):
            _line("ROUTE", session.route_note + (f" - at point {eng.guide_i} of {len(eng.guide)}" if eng.guide else ""))
        if session.exits:
            _line("EXITS", f"{len(session.exits)} avoided (keeping {eng.cfg.exit_avoid_radius:.0f} away)")
        else:
            _line("EXITS", "none detected on this map: exit avoidance is OFF", WARN)
        if eng.snags:
            _line("SNAGS", f"{len(eng.snags)} bits of scenery learnt here (grey on the map; kept between runs, walked round)")
            if not botting_tree.IsStarted() and PyImGui.button("Forget the snags learnt in this area"):
                eng.snags = []
                session.save_snags()
        if eng.hazards:
            _line("TRAPS", f"{len(eng.hazards)} learnt here (purple on the map; kept between runs)")
            if not botting_tree.IsStarted() and PyImGui.button("Forget the traps learnt in this area"):
                eng.hazards = []
                session.save_hazards()

    if PyImGui.collapsing_header("Log and bug reports"):
        if session.log.path:
            PyImGui.text_wrapped(f"Run log: {session.log.path}")
        if PyImGui.button("Write a debug file"):
            dump_debug()
        if View.last_dump:
            PyImGui.text_wrapped(f"Debug file: {View.last_dump}")


def dump_debug():
    """Write everything needed to diagnose a stall to a JSON file in the Py4GW folder."""
    import json, os, time
    eng = session.engine
    try:
        data = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "map_id": session.map_id,
                "error": session.error, "result": session.result, "started": botting_tree.IsStarted(), "stalls": session.stalls}
        if eng is not None:
            o, mem = eng.objective, eng.mem
            data.update({
                "status": eng.status(),
                "player_xy": eng.player_xy,
                "player_node_xy": eng.nav.nodes[eng.player_node] if eng.player_node is not None else None,
                "player_on_mesh": eng.nav.on_mesh(*eng.player_xy),
                "player_in_no_go": eng.nav.in_no_go(*eng.player_xy),
                "objective": None if o is None else {"kind": o.kind, "key": list(map(str, o.key)), "pos": o.pos,
                                                     "region": o.region, "waited": o.waited, "walk": o.walk},
                "next_step": eng.next_step(),
                "route_ahead": eng.route_ahead[:60],
                "exits": eng.exits,
                "config": {k: getattr(eng.cfg, k) for k in dir(eng.cfg) if not k.startswith("_")},
                "blocked": {str(k): v for k, v in mem.blocked.items()},
                "route_tail": mem.route[-80:],
                "enemies": [{"id": e.id, "xy": e.xy, "alive": e.alive, "in_range": e.in_range,
                             "lost": e.lost, "boss": e.boss} for e in mem.enemies.values()],
                "foes_killed": game.foes_killed(),
                "party_defeated": game.party_defeated(),
            })
        try:
            bb = botting_tree.blackboard
            data["blackboard"] = {str(k): repr(v)[:200] for k, v in bb.items()}
        except Exception as e:
            data["blackboard"] = repr(e)
        path = os.path.join(PySystem.Console.get_projects_path(), f"gwamm_debug_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)
        View.last_dump = path
    except Exception as e:
        View.last_dump = f"failed: {e!r}"



def draw_developer():
    """Data-building tools used while writing the bot. Nobody needs them to run it."""
    running = botting_tree is not None and botting_tree.IsStarted()
    cfg = session.cfg
    cfg.record_fights = PyImGui.checkbox("Record every fight in detail (gwamm_logs/fights), to learn from", cfg.record_fights)
    if session.fights.written or session.fights.error:
        PyImGui.text(f"Fights recorded this session: {session.fights.written}" + (f"   (problem: {session.fights.error})" if session.fights.error else ""))
    View.show_reforged = PyImGui.checkbox("Show Reforged's own bot window (behaviour tree view)", View.show_reforged)
    if world_dump.running():
        PyImGui.text(f"WORLD DATA: reading map {world_dump.progress} of 900...")
    else:
        if PyImGui.button("Build world data v2 (a few minutes, the game will stutter)"):
            world_dump.start()
        if world_dump.done:
            PyImGui.text_wrapped(f"WORLD DATA: written to {world_dump.path}")
        if world_dump.error:
            PyImGui.text_wrapped(f"WORLD DATA failed: {world_dump.error}")
    if geo_dump.running():
        PyImGui.text(f"MAP GEOMETRY: area {geo_dump.progress} of {geo_dump.total}...")
    else:
        if not running and PyImGui.button("Export ground data for every area (a few minutes, the game will stutter)"):
            geo_dump.start()
        if geo_dump.done:
            PyImGui.text_wrapped(f"MAP GEOMETRY: {geo_dump.written} of {geo_dump.total} areas written to {geo_dump.path}")
        if geo_dump.error:
            PyImGui.text_wrapped(f"MAP GEOMETRY failed: {geo_dump.error}")
    if not running and PyImGui.button("Rebuild map model now"):
        session.drop()



_PC_MODES = ["Off", "When the area is going badly", "Always"]


def draw_consumables():
    """One row per consumable: what it does, how many you carry, and when it may be used."""
    from Sources.gwamm.core import consumables as C
    cfg, pc = session.cfg, session.pcons
    cfg.pcons_after_deaths = PyImGui.slider_int('Deaths in an area that count as "going badly"', cfg.pcons_after_deaths, 1, 10)
    cfg.pcons_big_fight = PyImGui.slider_int("Where a wipe ends the run: start timed bonuses for a fight of", cfg.pcons_big_fight, 4, 15)
    cfg.pcons_min_foes = PyImGui.slider_int("Do not start a timed bonus with fewer foes left than", cfg.pcons_min_foes, 0, 80)
    PyImGui.separator()
    PyImGui.text("Limits, so nothing is wasted on an area that is being lost anyway")
    cfg.pcons_max_morale = PyImGui.slider_int("Death-penalty items per area, at most", cfg.pcons_max_morale, 0, 6)
    cfg.pcons_morale_worth = PyImGui.slider_int("...and not another until the last one bought this many kills", cfg.pcons_morale_worth, 0, 60)
    cfg.pcons_max_timed = PyImGui.slider_int("Timed bonuses started per area, at most", cfg.pcons_max_timed, 0, 18)
    cfg.pcons_stop_wipes = PyImGui.slider_int("Stop using items in an area after this many wipes since the first one", cfg.pcons_stop_wipes, 1, 6)
    sp = pc.spent
    PyImGui.text(f"This area so far: {sp['morale']} death-penalty, {sp['timed']} timed, {sp['summons']} summons.   {pc.note}")
    groups = (("Timed bonuses for the whole party (you use them)", lambda i: i.kind == C.EFFECT and i.helps == "party"),
              ("Timed bonuses for one character", lambda i: i.kind == C.EFFECT and i.helps == "self"),
              ("Death penalty: whole party", lambda i: i.kind == C.PARTY_MORALE),
              ("Death penalty: yourself", lambda i: i.kind == C.SELF_MORALE))
    for title, pick in groups:
        PyImGui.separator()
        PyImGui.text(title)
        if "Death penalty: whole party" == title:
            cfg.pcons_dp_threshold = PyImGui.slider_int("Use at this much death penalty (%)", cfg.pcons_dp_threshold, 15, 55)
            cfg.pcons_dp_members = PyImGui.slider_int("...when this many of the party are that far down", cfg.pcons_dp_members, 1, 8)
        for it in [i for i in C.CATALOGUE if pick(i)]:
            have = View.pc_counts.get(it.key, 0)
            label = f"{it.name} (you have {have}): {it.what}"
            if it.kind == C.EFFECT:
                setattr(cfg, it.setting, PyImGui.combo(f"{label}##{it.key}", int(getattr(cfg, it.setting)), _PC_MODES))
            else:
                setattr(cfg, it.setting, PyImGui.checkbox(f"{label}##{it.key}", bool(getattr(cfg, it.setting))))
    PyImGui.separator()
    PyImGui.text("Summoning stones (an extra ally)")
    stones = [(n, c) for n, c in View.pc_counts.get("stones", []) if c > 0]
    have = ", ".join(f"{n} x{c}" for n, c in stones) if stones else "none in your bags"
    cfg.summon_stones = PyImGui.checkbox(f"Call a summoned ally whenever none is out (you have: {have})##summon", cfg.summon_stones)
    import time as _t
    if _t.time() - View.pc_counts.get("t", 0) > 5.0:          # recount the bags every few seconds
        View.pc_counts = {"t": _t.time()}
        for it in C.CATALOGUE:
            View.pc_counts[it.key] = pc.count(it.key)
        try:
            from Sources.gwamm.runtime.pcons import summon_stones
            View.pc_counts["stones"] = [(n, c) for n, _m, c in summon_stones()]
        except Exception:
            View.pc_counts["stones"] = []
    if cfg.multibox:
        PyImGui.text_wrapped("Shared party: single-character bonuses are also requested from the other accounts, "
                             "which use their own. Death-penalty items for one character are yours only.")

def draw_options():
    cfg = session.cfg
    PyImGui.text("Changes apply the next time you enter an area.")
    PyImGui.separator()
    PyImGui.text("PARTY")
    cfg.party_mode = PyImGui.combo("Party type", int(cfg.party_mode),
                                   ["Work it out from my party (recommended)", "Heroes and henchmen only", "Other players are in my party"])
    p = getattr(session, "party", None) or game.party_makeup()
    PyImGui.text(f"Right now: you + {max(0, p['players'] - 1)} other player(s), {p['heroes']} hero(es), {p['henchmen']} henchmen")
    if cfg.multibox:
        accounts = game.party_accounts_cached()
        others = max(0, p["players"] - 1)
        for a in accounts:
            state = "HeroAI following and fighting" if a["following"] and a["combat"] else (
                "HeroAI options off (switched on when the area starts)" if a["following"] is not None else "HeroAI state unknown")
            PyImGui.text(f"   {a['name']}: Py4GW running, {state}")
        if others > len(accounts):
            PyImGui.text_wrapped(f"   {others - len(accounts)} other player(s) are not seen by Py4GW in this party: the bot "
                                 "cannot steer them. Start Py4GW on those accounts (same map, same party).")
        PyImGui.text_wrapped("Other players are in the party, so nobody is added or dismissed. Your own heroes get the "
                             "bars saved under Builds; the other accounts keep their own bars and HeroAI is switched on "
                             "for them when the bot starts. They need the outposts unlocked. Areas whose outpost allows "
                             "fewer members than the party has are skipped. After an area everyone resigns and returns "
                             "to the outpost together.")
    else:
        PyImGui.text_wrapped("Only you, heroes and henchmen: the bot loads the team saved under Builds for each area.")
    PyImGui.separator()
    PyImGui.text("CONSUMABLES")
    cfg.pcons_on = PyImGui.checkbox("Use consumables (only the ones switched on below, only from your bags)", cfg.pcons_on)
    if cfg.pcons_on and PyImGui.collapsing_header("Choose consumables", PyImGui.TreeNodeFlags.DefaultOpen):
        safe("consumables list", draw_consumables)()
    PyImGui.separator()
    cfg.do_vanquish = PyImGui.checkbox("Vanquish", cfg.do_vanquish)
    cfg.do_cartography = PyImGui.checkbox("Cartography", cfg.do_cartography)
    cfg.capture_elites = PyImGui.checkbox("Capture elites from bosses", cfg.capture_elites)
    cfg.take_blessings = PyImGui.checkbox("Take blessings and bounties from shrine NPCs", cfg.take_blessings)
    cfg.retreat_when_losing = PyImGui.checkbox("Pull right back when several of the party are down", cfg.retreat_when_losing)
    cfg.route_hints = PyImGui.checkbox("Use the area's known route as a guide (and as the way round when blocked)", cfg.route_hints)
    cfg.edge_pass = PyImGui.checkbox("After the vanquish, also clear map cells beside exits (takes extra time)", cfg.edge_pass)
    cfg.call_targets = PyImGui.checkbox("Call targets: healers, bosses and casters first", cfg.call_targets)
    cfg.keep_at_it = PyImGui.checkbox("Testing: keep at the fight in front of us (deaths and wipes do not send the party elsewhere; never give the area up)", cfg.keep_at_it)
    cfg.pull_groups = PyImGui.checkbox("New, untested: pull the nearest group back to the party before fighting (heroes and henchmen only)", cfg.pull_groups)
    cfg.pull_ranged = PyImGui.checkbox("Pull from weapon range (wand, staff or bow): hit one enemy from outside their notice range, so only its group comes", cfg.pull_ranged)
    if cfg.pull_groups:
        cfg.pull_min_group = PyImGui.slider_int("Pull groups of at least this many", cfg.pull_min_group, 2, 10)
        cfg.pull_max_take = PyImGui.slider_int("Hold off if more than this many would come at once", cfg.pull_max_take, 4, 20)
        cfg.pull_wait_seconds = PyImGui.slider_float("Longest wait for a patrol to move on (seconds)", cfg.pull_wait_seconds, 10.0, 120.0)
    cfg.fall_back = PyImGui.checkbox("Fall back along cleared ground when a pull is too big", cfg.fall_back)
    cfg.crowd_size = PyImGui.slider_int("Enemies around us that count as too big a pull", cfg.crowd_size, 5, 20)
    cfg.buy_signets = PyImGui.checkbox("Buy Signets of Capture when short (campaigns)", cfg.buy_signets)
    cfg.change_secondary = PyImGui.checkbox("Change secondary profession in the outpost for the best elites (campaigns)", cfg.change_secondary)
    cfg.unlock_outposts = PyImGui.checkbox("After a vanquish, walk into a locked outpost if one is reachable", cfg.unlock_outposts)
    cfg.heroes_first, cfg.use_routes = False, False      # trial features, withdrawn from the window
    if not PyImGui.collapsing_header("Fine tuning (the defaults are right for most parties)"):
        return
    cfg.search_radius = PyImGui.slider_float("Search radius", cfg.search_radius, 800.0, 4500.0)
    cfg.engage_radius = PyImGui.slider_float("Fight radius", cfg.engage_radius, 800.0, 3500.0)
    cfg.standoff = PyImGui.slider_float("Stop short of groups by", cfg.standoff, 0.0, 1500.0)
    cfg.exit_avoid_radius = PyImGui.slider_float("Keep away from exits", cfg.exit_avoid_radius, 400.0, 2500.0)
    cfg.rest_hp = PyImGui.slider_float("Rest until health", cfg.rest_hp, 0.0, 1.0)
    cfg.rest_energy = PyImGui.slider_float("Rest until energy", cfg.rest_energy, 0.0, 1.0)
    cfg.rest_max_seconds = PyImGui.slider_float("Longest rest (seconds)", cfg.rest_max_seconds, 0.0, 120.0)


_vanq_cache = {"t": 0.0, "ids": set()}


def _vanquished():
    import time
    if time.time() - _vanq_cache["t"] > 5.0:
        _vanq_cache["t"] = time.time()
        try:
            _vanq_cache["ids"] = game.vanquished_ids()
        except Exception:
            report("vanquished list")
    return _vanq_cache["ids"]


def draw_builds():
    """Which team (heroes, their bars, your bar) to load for each party size and signet count."""
    c = get_campaign()
    running = botting_tree is not None and botting_tree.IsStarted()
    PyImGui.text_wrapped("Before each area the bot loads the team saved for that area's party size and the number "
                         "of signets it wants. If there is none for that many signets, it uses the next one down.")
    if running:
        PyImGui.text("Stop the bot to change builds.")
        return
    c.toolbox_path = PyImGui.input_text("Toolbox team builds file (blank = find it)", c.toolbox_path)
    if PyImGui.button("Import team builds from Toolbox"):
        c.import_toolbox(c.toolbox_path.strip())
    PyImGui.same_line(0.0, -1.0)
    if PyImGui.button("Save my current party and bar"):
        View.build_note = f"saved to slot {c.save_current_team()}"
    if c.import_note:
        PyImGui.text_wrapped(c.import_note)
    if View.build_note:
        PyImGui.text(View.build_note)
    names = ["(none)"] + [t["name"] for t in c.library]
    for size in (8, 6, 4):
        PyImGui.separator()
        for n in (0, 1, 2):
            key = f"{size}:{n}"
            cur = c.builds.get(key)
            label = f"{size}-player areas, {n} signet{'s' if n != 1 else ''}"
            if c.library:
                idx = names.index(cur["name"]) if cur and cur["name"] in names else 0
                pick = PyImGui.combo(f"{label}##b{key}", idx, names)
                if pick != idx:
                    if pick == 0:
                        c.builds.pop(key, None)
                    else:
                        c.builds[key] = c.library[pick - 1]
                    c.save()
                if cur:
                    try:
                        from Sources.gwamm.core.builds import Team as _Team
                        have = _Team.from_dict(cur).signets
                        if have != n:
                            PyImGui.same_line(0.0, -1.0)
                            PyImGui.text(f"(this build has {have} signet{'s' if have != 1 else ''})")
                    except Exception:
                        pass
                if cur and cur["name"] not in names:
                    PyImGui.same_line(0.0, -1.0)
                    PyImGui.text(f"(using: {cur['name']})")
            else:
                PyImGui.text(f"{label}: {cur['name'] + ', ' + str(len(cur['heroes'])) + ' heroes' if cur else '(none)'}")


def draw_campaign():
    """Pick the areas to run. With a queue, Start works through it, travel included."""
    c = get_campaign()
    done = _vanquished()
    running = botting_tree is not None and botting_tree.IsStarted()
    if c.current is not None:
        PyImGui.text(f"NOW: {game.map_name(c.current)} - {c.phase}")
    PyImGui.text(f"QUEUE ({len(c.queue)}): " + (", ".join(game.map_name(m) for m in c.queue[:6]) or "empty")
                 + (" ..." if len(c.queue) > 6 else ""))
    if not c.queue:
        PyImGui.text_wrapped("Empty queue: Start vanquishes the area you are standing in.")
    PyImGui.text("Team builds are set under Builds. The settings below are only used for a party size with no saved team.")
    PyImGui.text(f"FALLBACK BAR: {'saved' if c.capture_template else 'not saved yet (the bar you have at the first Start is used)'}")
    try:
        from Sources.gwamm.core import builds as _b
        sl, src = _b.detect_signet_slots(c.builds, game.max_party_size(), c.signet_slots)
        PyImGui.text(f"SIGNET SLOTS: {sl[0]} then {sl[1]} - " + (f"read from '{src}'" if src else "default (no saved build carries a signet)"))
    except Exception:
        pass
    if not running:
        if PyImGui.button("Save my current bar as the fallback bar"):
            c.capture_template = game.bar_template()
            c.save()
        ex = PyImGui.checkbox("Exit game when current queue ends", c.exit_when_done)
        if ex != c.exit_when_done:
            c.exit_when_done = ex
            c.save()
        m = PyImGui.slider_int("Most signets to carry", c.max_signets, 0, 2)
        n = PyImGui.slider_int("Signets when I have no boss list for the area", c.nodata_signets, 0, 2)
        if m != c.max_signets or n != c.nodata_signets:
            c.max_signets, c.nodata_signets = m, n
            c.save()
    if running:
        PyImGui.text("Stop the bot to change the queue.")
    else:
        if PyImGui.button("Queue everything I can do"):
            c.queue_all(done)
        PyImGui.same_line(0.0, -1.0)
        if PyImGui.button("Clear queue"):
            c.queue = []
            c.save()
    View.search = PyImGui.input_text("Search areas", View.search)
    needle = View.search.strip().lower()

    def row(mid):
        why = c.available(mid, done)
        res = c.results.get(mid)
        hops = c.hops(mid) if not why else None
        note = why or (f"walk through {hops} area{'s' if hops != 1 else ''} to get there" if hops else "")
        if res:
            note = (note + "; " if note else "") + f"last: {res['result']} ({res['minutes']} min)"
        if why or running:
            PyImGui.text(("[queued] " if mid in c.queue else "         ") + game.map_name(mid) + (f"  - {note}" if note else ""))
        else:
            now = mid in c.queue
            if PyImGui.checkbox(f"{game.map_name(mid)}##q{mid}", now) != now:
                c.toggle(mid)
            if note:
                PyImGui.same_line(0.0, -1.0)
                PyImGui.text(f"- {note}")

    if needle:
        # searching: one flat list of matches across every region
        hits = [m for ids in c.regions().values() for m in ids if needle in game.map_name(m).lower()]
        if not hits:
            PyImGui.text("No area matches that.")
        for mid in hits[:40]:
            row(mid)
        return
    for region, ids in c.regions().items():
        left = sum(1 for m in ids if m not in done)
        if not PyImGui.collapsing_header(f"{region}  ({left} of {len(ids)} left)"):
            continue
        if not running:
            if PyImGui.button(f"Queue all in {region}"):
                c.queue_all(done, region)
        for mid in ids:
            row(mid)

def draw_active_area():
    """The one line that says which area the current run is for, in large type at the top."""
    c = get_campaign()
    running = botting_tree is not None and botting_tree.IsStarted()
    try:
        here = game.map_id() if game.map_ready() else 0
    except Exception:
        here = 0
    target = c.current if c.current is not None else (session.map_id if running and session.engine is not None else None)
    if not running:
        head, colour = "ACTIVE AREA: none (not running)", (0.65, 0.65, 0.65, 1.0)
        if c.queue:
            sub = "Start will run: " + ", ".join(game.map_name(m) for m in c.queue[:4]) + (" ..." if len(c.queue) > 4 else "")
        else:
            sub = f"Start will vanquish where you stand: {game.map_name(here)}" if here else ""
    elif target is None:
        head, colour, sub = "ACTIVE AREA: choosing the next one", (1.0, 0.85, 0.3, 1.0), ""
    else:
        head, colour = f"ACTIVE AREA: {game.map_name(target)}", (0.35, 1.0, 0.45, 1.0)
        bits = []
        if c.phase:
            bits.append(c.phase)
        if here and here != target:
            bits.append(f"currently in {game.map_name(here)}")
        if c.queue:
            left = [m for m in c.queue if m != target]
            bits.append(f"{len(left)} more queued" if left else "last in the queue")
        sub = " - ".join(bits)
    try:
        PyImGui.text_colored(head, colour)
    except Exception:
        PyImGui.text(head)
    if sub:
        PyImGui.text(sub)
    PyImGui.separator()


def _tab(label, fn, where):
    if PyImGui.begin_tab_item(label):
        try:
            safe(where, fn)()
        finally:
            PyImGui.end_tab_item()


def draw_main():
    """The bot window: what it is doing on top, Start/Stop, then one tab per subject."""
    if not View.main_sized:
        PyImGui.set_next_window_size(560, 640)
        View.main_sized = True
    if PyImGui.begin(f"{MODULE_NAME} {__version__}"):
        safe("active area banner", draw_active_area)()
        tree = botting_tree
        if tree is not None:
            if tree.IsStarted():
                if PyImGui.button("  Stop  "):
                    tree.Stop()
            elif PyImGui.button("  Start  "):
                tree.Start()
            PyImGui.same_line(0.0, -1.0)
            View.show_map = PyImGui.checkbox("Map window", View.show_map)
        safe("summary", draw_summary)()
        PyImGui.separator()
        if PyImGui.begin_tab_bar("gwamm_tabs"):
            try:
                _tab("Run", draw_status, "run tab")
                _tab("Areas", draw_campaign, "area list")
                _tab("Builds", draw_builds, "builds")
                _tab("Options", draw_options, "options tab")
                _tab("Developer", draw_developer, "developer tools")
            finally:
                PyImGui.end_tab_bar()
    PyImGui.end()


def draw_map():
    """The map window: the ground, what has been searched, enemies, and the planned route."""
    if not View.show_map:
        return
    eng = session.engine
    if not View.sized:
        PyImGui.set_next_window_size(640, 700)
        View.sized = True
    visible, still_open = PyImGui.begin_with_close(MODULE_NAME + " - Map", True)
    if visible:
        View.follow = PyImGui.checkbox("Follow me", View.follow)
        PyImGui.same_line(0.0, -1.0)
        View.show_ground = PyImGui.checkbox("Ground", View.show_ground)
        PyImGui.same_line(0.0, -1.0)
        View.show_regions = PyImGui.checkbox("Regions", View.show_regions)
        PyImGui.same_line(0.0, -1.0)
        View.show_tour = PyImGui.checkbox("Planned order", View.show_tour)
        PyImGui.same_line(0.0, -1.0)
        View.show_fog = PyImGui.checkbox("Fogged cells", View.show_fog)
        if eng is None:
            PyImGui.text("The map appears once an area has loaded.")
        else:
            try:
                _canvas(eng)
            except Exception:
                report("map window")
                PyImGui.text(View.last_error)
    PyImGui.end()
    if not still_open:
        View.show_map = False



def _canvas(eng):
    avail_w, avail_h = PyImGui.get_content_region_avail()
    if avail_w < 50 or avail_h < 50:
        return
    flags = (PyImGui.WindowFlags.NoMove | PyImGui.WindowFlags.NoScrollbar
             | PyImGui.WindowFlags.NoScrollWithMouse)
    try:
        if PyImGui.begin_child("GWAMMMap", (avail_w, avail_h), border=False, flags=flags):
            ox, oy = PyImGui.get_window_pos()
            w, h = PyImGui.get_window_size()
            mouse = PyImGui.get_mouse_pos()
            if PyImGui.is_window_hovered():
                wheel = PyImGui.get_io().mouse_wheel
                if wheel:
                    View.zoom = max(0.2, min(60.0, View.zoom * (1.15 ** wheel)))
                if PyImGui.is_mouse_down(0) and View.prev_mouse:
                    View.pan[0] += mouse[0] - View.prev_mouse[0]
                    View.pan[1] += mouse[1] - View.prev_mouse[1]
                    View.follow = False
            View.prev_mouse = mouse

            nav, rm, mem = eng.nav, eng.rm, eng.mem
            xs = [n[0] for n in nav.nodes]
            ys = [n[1] for n in nav.nodes]
            min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
            scale = min(w / (max_x - min_x + 1), h / (max_y - min_y + 1)) * 0.95 * View.zoom
            px, py = eng.player_xy
            if View.follow:
                mid_x, mid_y, View.pan = px, py, [0.0, 0.0]
            else:
                mid_x, mid_y = (min_x + max_x) / 2, (min_y + max_y) / 2
            cx, cy = ox + w / 2 + View.pan[0], oy + h / 2 + View.pan[1]

            def S(x, y):
                return cx + (x - mid_x) * scale, cy - (y - mid_y) * scale

            def visible(sx, sy):
                return ox <= sx <= ox + w and oy <= sy <= oy + h

            if View.show_ground:
                for (_p, xtl, xtr, yt, xbl, xbr, yb) in nav.traps:
                    ax, ay = S(xtl, yt)
                    bx, by = S(xtr, yt)
                    ex, ey = S(xbr, yb)
                    dx, dy = S(xbl, yb)
                    if max(ax, bx, ex, dx) < ox or min(ax, bx, ex, dx) > ox + w or ey < oy or ay > oy + h:
                        continue
                    PyImGui.draw_list_add_quad_filled(ax, ay, bx, by, ex, ey, dx, dy, COL_GROUND)

            if View.show_fog and eng.carto is not None:
                for _cell, footing, _node, reveals in eng.carto_targets:
                    for c in reveals:
                        x0, y0, x1, y1 = eng.carto.proj.cell_corners(*c)
                        ax, ay = S(x0, y1)
                        bx, by = S(x1, y0)
                        PyImGui.draw_list_add_rect_filled(ax, ay, bx, by, COL_FOG)
                        PyImGui.draw_list_add_rect(ax, ay, bx, by, COL_FOG_EDGE)
                    fx, fy = S(*footing)
                    PyImGui.draw_list_add_circle_filled(fx, fy, 4.0, _col(80, 200, 255), 8)

            if View.show_tour:
                pts = [S(*rm.regions[r].pos) for r in eng.tour]
                for a, b in zip(pts, pts[1:]):
                    PyImGui.draw_list_add_line(a[0], a[1], b[0], b[1], COL_TOUR, 1.0)

            if View.show_regions:
                for region in rm.regions:
                    sx, sy = S(*region.pos)
                    if not visible(sx, sy):
                        continue
                    state = mem.region_state(region.id)
                    col = COL_SEARCHED if state == SEARCHED else COL_SEEN if state == SEEN else COL_UNKNOWN
                    PyImGui.draw_list_add_circle_filled(sx, sy, 3.0, col, 6)

            route = mem.route
            for i in range(max(1, len(route) - 4000), len(route)):
                if abs(route[i][0] - route[i - 1][0]) + abs(route[i][1] - route[i - 1][1]) > 1500.0:
                    continue                                 # a jump to a shrine: not a walk, no line across the map
                a, b = S(*route[i - 1]), S(*route[i])
                PyImGui.draw_list_add_line(a[0], a[1], b[0], b[1], COL_WALKED, 1.5)

            last = S(px, py)
            for p in eng.route_ahead:
                q = S(*p)
                PyImGui.draw_list_add_line(last[0], last[1], q[0], q[1], COL_AHEAD, 2.0)
                last = q
            if eng.objective is not None:
                tx, ty = S(*eng.objective.pos)
                PyImGui.draw_list_add_circle(tx, ty, 9.0, COL_AHEAD, 12, 2.5)

            for e in mem.enemies.values():
                if not e.alive or e.lost:
                    continue
                sx, sy = S(*e.xy)
                if visible(sx, sy):
                    if e.id in eng.walled:               # other level / behind a wall: hollow yellow
                        PyImGui.draw_list_add_circle(sx, sy, 4.5, _col(255, 220, 60), 8, 2.0)
                    elif e.in_range:
                        PyImGui.draw_list_add_circle_filled(sx, sy, 5.0 if e.boss else 3.5, COL_BOSS if e.boss else COL_ENEMY, 8)
                    else:
                        PyImGui.draw_list_add_circle(sx, sy, 4.0, COL_STALE, 8, 1.5)

            for dx_, dy_, until in eng.danger:
                if until > eng._now:
                    sx, sy = S(dx_, dy_)
                    PyImGui.draw_list_add_circle(sx, sy, eng.cfg.danger_radius * scale, _col(255, 140, 0, 220), 32, 2.0)

            # (These two loops used to store their screen position in cx, cy: the very names the
            # map's own centre is kept in. Every wipe ring or snag drawn moved the centre, so
            # everything drawn after it, the player marker included, landed in the wrong place.)
            for wx, wy, _n in eng.wipes:                     # where the whole party went down: red ring, left for last
                rx, ry = S(wx, wy)
                PyImGui.draw_list_add_circle(rx, ry, max(6.0, eng.cfg.danger_radius * scale), _col(255, 70, 60), 32, 3.0)
            for gx, gy in eng.snags:                         # scenery we were caught on: grey
                rx, ry = S(gx, gy)
                PyImGui.draw_list_add_circle(rx, ry, max(3.0, 300.0 * scale), _col(170, 170, 170, 230), 12, 1.5)
            for hx, hy in eng.hazards:                       # traps learnt from deaths: purple
                sx, sy = S(hx, hy)
                PyImGui.draw_list_add_circle(sx, sy, max(4.0, eng.cfg.hazard_radius * scale), _col(200, 60, 255, 255), 16, 2.5)

            for ex, ey, er in nav.no_go:
                sx, sy = S(ex, ey)
                PyImGui.draw_list_add_circle_filled(sx, sy, er * scale, _col(255, 40, 40, 60), 32)
                PyImGui.draw_list_add_circle(sx, sy, er * scale, _col(255, 40, 40, 230), 32, 2.0)

            sx, sy = S(px, py)
            PyImGui.draw_list_add_circle_filled(sx, sy, 5.0, COL_PLAYER, 10)
            PyImGui.draw_list_add_circle(sx, sy, mem.search_radius * scale, _col(60, 255, 90, 110), 32, 1.0)
    finally:
        PyImGui.end_child()


# ---- entry point ----------------------------------------------------------

_slow = {"at": 0.0}


def _note_slow_frame(_unused, lv):
    """Log any frame that held the game up for more than 0.4 s, with where the time went, so a
    freeze can be traced to its cause (at most one entry every 10 s)."""
    try:
        t0, t1, t2, t3 = lv.get("_t0"), lv.get("_t1"), lv.get("_t2"), lv.get("_t3")
        if t0 is None or t3 is None:
            return
        end = time.perf_counter()
        total = end - t0
        if total < 0.4 or end - _slow["at"] < 10.0 or session is None:
            return
        _slow["at"] = end
        eng = session.engine
        session.log.event("slow_frame", seconds=round(total, 2), tree=round((t1 or t0) - t0, 2),
                          observe=round(t2 - (t1 or t0), 2), window=round(t3 - t2, 2), map=round(end - t3, 2),
                          phase=getattr(getattr(session, "campaign", None), "phase", None),
                          mode=None if eng is None else eng.mode,
                          objective=None if eng is None or eng.objective is None else eng.objective.label())
    except Exception:
        pass


def main():
    global initialized, ini_key
    if INSTALL_PROBLEM:
        if PyImGui.begin(MODULE_NAME + " - install problem"):
            PyImGui.text_wrapped(INSTALL_PROBLEM)
        PyImGui.end()
        return
    try:
        if not initialized:
            if not ini_key:
                ini_key = Settings(f"{INI_PATH}/{INI_FILENAME}", "account").name
                if not ini_key:
                    return
            ensure_tree()
            initialized = True

        tree = ensure_tree()
        world_dump.tick()
        geo_dump.tick()
        try:
            _t0 = time.perf_counter()
            tree.tick()
            _t1 = time.perf_counter()
            if not tree.IsStarted():
                # Observe only: keep the model current and show the plan, without moving.
                if session.ensure_engine() and game.is_explorable():
                    session.perceive()
                    session.engine.next_step()
                    session.log.observe(session.engine, {"running": False})
        except Exception:
            report("tick")
        _t2 = time.perf_counter()
        try:
            draw_main()
        except Exception:
            report("bot window")
        if View.show_reforged:
            try:                 # Reforged's own window (its tree view): for developers
                tree.UI.draw_window()
            except Exception:
                report("reforged window")
        _t3 = time.perf_counter()
        try:
            draw_map()
        except Exception:
            report("map window")
        _note_slow_frame(_t0 if "_t0" in dir() else None, locals())
    except Exception as e:
        PySystem.Console.Log(MODULE_NAME, f"main() error: {e!r}", PySystem.Console.MessageType.Error)


if __name__ == "__main__":
    main()
