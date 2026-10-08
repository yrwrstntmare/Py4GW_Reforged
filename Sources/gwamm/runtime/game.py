import math
"""Every read from the live game goes through this file, so the rest of the package
stays free of Py4GW imports. Calls checked against Py4GW Reforged commit 0b05841."""
from ctypes import c_uint32

from Py4GWCoreLib.Agent import Agent
from Py4GWCoreLib.AgentArray import AgentArray
from Py4GWCoreLib.Context import GWContext
from Py4GWCoreLib.Map import Map
from Py4GWCoreLib.Party import Party
from Py4GWCoreLib.Player import Player
from Py4GWCoreLib.Routines import Routines
from Py4GWCoreLib.Skillbar import SkillBar
from Py4GWCoreLib.native_src.internals.gw_array import GW_Array_Value_View

from ..core.cartography import CartoGrid, Projection

SIGNET_OF_CAPTURE = 3


def map_ready():
    return (not Map.IsMapLoading()) and Routines.Checks.Map.MapValid()


def map_loading_safe():
    try:
        return bool(Map.IsMapLoading())
    except Exception:
        return None


def map_id():
    return Map.GetMapID()


def is_explorable():
    return Map.IsExplorable()


def read_level_links():
    """Pairs of trapezoids (positions in read_trapezoids' list) joined across levels, found the
    way Reforged's own pathing does it: the game's portal records name the trapezoids on each
    side, and the ones that touch are connected."""
    maps = Map.Pathing.GetPathingMaps()
    index, traps, n = {}, {}, 0
    for layer in maps:
        for t in layer.trapezoids:
            index[t.id], traps[t.id] = n, t
            n += 1
    groups = {}
    for plane, layer in enumerate(maps):
        for portal in layer.portals:
            if portal.left_layer_id == portal.right_layer_id:
                continue
            pair = (min(portal.left_layer_id, portal.right_layer_id), max(portal.left_layer_id, portal.right_layer_id))
            for tid in portal.trapezoid_indices:
                if tid in traps:
                    groups.setdefault(pair, {}).setdefault(plane, []).append(tid)

    def touching(a, b, vt=100.2, ht=100.6):
        if abs(a.YB - b.YT) < vt or abs(a.YT - b.YB) < vt:
            if max(a.XBR, a.XTR) >= min(b.XBL, b.XTL) and max(b.XBR, b.XTR) >= min(a.XBL, a.XTL):
                return True
        if abs(a.XBR - b.XBL) < ht or abs(a.XBL - b.XBR) < ht:
            if max(a.YT, a.YB) >= min(b.YT, b.YB) and max(b.YT, b.YB) >= min(a.YT, a.YB):
                return True
        return False

    out = set()
    for planes in groups.values():
        keys = list(planes)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                for a in planes[keys[i]]:
                    for b in planes[keys[j]]:
                        if a != b and touching(traps[a], traps[b]):
                            out.add((min(index[a], index[b]), max(index[a], index[b])))
    return sorted(out)


# Shrine NPCs that hand out blessings and bounties (model ids and effect ids as listed in
# Reforged's Sources/aC_Scripts/aC_api). Dialog ids per campaign follow PyQuishAI_BT.
BLESSING_NPCS = {
    "nightfall": (4778, 4776, 5384, 5383, 5632, 5547, 5548, 5615, 5218, 5683, 5002),
    "kurzick": (593, 912, 3426),
    "luxon": (1947, 3641),
    "north": (5865, 1986, 1987, 6044, 6045, 6043, 6755, 6756, 6775, 6779, 6374, 6380),
}
BLESSING_DIALOG = {"nightfall": 0x85, "kurzick": 0x86, "luxon": 0x86, "north": 0x84}
BLESSING_EFFECTS = (
    1794, 1842, 1971, 1972, 1853, 1963, 1964, 1837, 1838, 1977, 1978, 1839, 1843, 1979, 1980, 1791, 1840, 1841,
    1852, 1961, 1962, 1832, 1969, 1970, 1822, 1823, 1824, 1825, 1850, 1959, 1960, 2043, 2044, 1795, 1833, 1834,
    1973, 1974, 1790, 1835, 1836, 1975, 1976, 1792, 1828, 1965, 1966, 1796, 1854, 1981, 1982, 1967,
    1898, 2040, 1831, 1844, 2030, 2031, 1826, 1827, 1846, 2032, 2033, 1849, 2036, 2037, 1845, 2038, 2039,
    1847, 1848, 2034, 2035, 1851, 2041, 2042, 593, 912, 1947, 1946,
    2445, 2446, 2447, 2448, 2549, 2565, 2566, 2567, 2568, 2457, 2458, 2459, 2460, 2550, 2578,
    2434, 2435, 2436, 2481, 2548, 2552, 2469, 2470, 2471, 2472, 2551, 2591, 2592, 2593, 2594)


def blessing_npcs():
    """Blessing givers in sight: [(agent id, x, y, kind)]."""
    out = []
    for agent_id in AgentArray.GetNPCMinipetArray():
        try:
            model = Agent.GetModelID(agent_id)
            kind = next((k for k, ids in BLESSING_NPCS.items() if model in ids), None)
            if kind and Agent.IsAlive(agent_id):
                x, y = Agent.GetXY(agent_id)
                out.append((agent_id, x, y, kind))
        except Exception:
            continue
    return out


def npcs_in_sight():
    """Every friendly NPC in sight: [(agent id, model id, x, y)]. For working out which ones
    give blessings."""
    out = []
    for agent_id in AgentArray.GetNPCMinipetArray():
        try:
            x, y = Agent.GetXY(agent_id)
            out.append((agent_id, Agent.GetModelID(agent_id), x, y))
        except Exception:
            continue
    return out


def gadgets_near(x, y, radius=1500.0):
    """Interactive/scenery objects near a spot: [(agent id, gadget id, x, y)]. Logged when
    something other than an enemy kills us, to learn what the trap looks like."""
    out = []
    for agent_id in AgentArray.GetGadgetArray():
        try:
            gx, gy = Agent.GetXY(agent_id)
            if (gx - x) ** 2 + (gy - y) ** 2 <= radius * radius:
                out.append((agent_id, Agent.GetGadgetID(agent_id), round(gx), round(gy)))
        except Exception:
            continue
    return out


def exit_game():
    """Close the game the ordinary way: ask its window to close. Returns a note for the log."""
    import ctypes
    import PySystem
    hwnd = int(PySystem.Console.get_gw_window_handle() or 0)
    if not hwnd:
        return "no game window handle"
    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)        # WM_CLOSE
    return "close requested"


def enemy_details(agent_id):
    """(health 0-1, level, carries a caster weapon) for a live enemy; None if it is gone."""
    try:
        if not Agent.IsValid(agent_id) or not Agent.IsAlive(agent_id):
            return None
        hp = float(Agent.GetHealth(agent_id))
        try:
            level = int(Agent.GetLevel(agent_id))
        except Exception:
            level = 0
        try:
            caster = bool(Agent.IsCaster(agent_id))
        except Exception:
            caster = False
        return hp, level, caster
    except Exception:
        return None


def attack(agent_id):
    """Have the leader attack this enemy (the client walks into weapon range and starts)."""
    if agent_id and Agent.IsValid(agent_id) and Agent.IsAlive(agent_id):
        Player.Interact(agent_id, False)
        return True
    return False


def leader_ranged():
    """True when the leader's weapon reaches further than enemies notice (wand, staff, bow)."""
    try:
        me = Player.GetAgentID()
        return bool(Agent.IsCaster(me) or Agent.IsRanged(me)) and not Agent.IsMelee(me)
    except Exception:
        return False


def tracker_roles():
    """What the Enemy Tracker widget (Widgets/System) has learnt about enemy types, as
    {model id: "healer" | "caster" | "fighter"}. That widget watches every enemy near the
    player whenever Py4GW runs and keeps the skills each type was seen using, so it covers
    areas our own fight recordings have not reached. Empty if it has no data."""
    import json
    import os
    import PySystem
    from ..core import tactics
    root = PySystem.Console.get_projects_path()
    # Read the widget's file itself (json/Global/EnemyTracker/Data.json, seen on disk with 320
    # enemy types). Going through the JSON library's shared document came back empty.
    records = {}
    try:
        with open(os.path.join(root, "json", "Global", "EnemyTracker", "Data.json"), encoding="utf-8") as f:
            records = (json.load(f) or {}).get("enemies") or {}
    except Exception:
        try:
            from Py4GWCoreLib.py4gwcorelib_src.JsonFactory import JsonFactory
            records = JsonFactory("EnemyTracker/Data.json", "global").get_json("enemies", {}) or {}
        except Exception:
            records = {}
    try:
        with open(os.path.join(root, "Py4GWCoreLib", "skill_descriptions.json"), encoding="utf-8") as f:
            texts = json.load(f)
    except Exception:
        texts = {}
    out = {}
    for record in records.values():
        if not isinstance(record, dict):
            continue
        skills = record.get("observed_skills") or {}
        heals = sum(1 for key in skills if tactics.heals_allies((texts.get(str(key)) or {}).get("desc_full", "")))
        primary = str(record.get("inferred_primary") or "")
        if heals >= 2 or (heals >= 1 and primary == "Monk"):
            role = "healer"
        elif primary in ("Monk", "Ritualist", "Mesmer", "Elementalist", "Necromancer"):
            role = "caster"
        elif primary:
            role = "fighter"
        else:
            continue
        for model in record.get("model_ids") or ():
            try:
                out[int(model)] = role
            except (TypeError, ValueError):
                continue
    return out


def enemy_model(agent_id):
    """The model number of a live enemy (identifies its type), 0 if it cannot be read."""
    try:
        return int(Agent.GetModelID(agent_id)) if Agent.IsValid(agent_id) else 0
    except Exception:
        return 0


def call_target(agent_id):
    if agent_id and Agent.IsValid(agent_id) and Agent.IsAlive(agent_id):
        Player.CallTarget(agent_id)
        return True
    return False


def has_blessing():
    from Py4GWCoreLib.Effect import Effects
    me = Player.GetAgentID()
    return any(Effects.EffectExists(me, i) for i in BLESSING_EFFECTS)


def player_xy():
    return Player.GetXY()


def read_trapezoids():
    """Plain tuples (plane, xtl, xtr, yt, xbl, xbr, yb); nothing keeps a pointer into game memory."""
    out = []
    for plane, layer in enumerate(Map.Pathing.GetPathingMaps()):
        for t in layer.trapezoids:
            out.append((plane, t.XTL, t.XTR, t.YT, t.XBL, t.XBR, t.YB))
    return out


def read_enemies():
    out = []
    for agent_id in AgentArray.GetEnemyArray():
        try:
            x, y = Agent.GetXY(agent_id)
            if not (math.isfinite(x) and math.isfinite(y)):
                continue                     # the game hands out blank positions for agents mid-spawn
            try:                             # which piece of ground it stands on, and how high
                plane, z = int(Agent.GetZPlane(agent_id)), float(Agent.GetXYZ(agent_id)[2])
            except Exception:
                plane, z = None, None
            out.append((agent_id, x, y, Agent.IsAlive(agent_id), Agent.HasBossGlow(agent_id), plane, z))
        except Exception:
            continue
    return out


def _agent_facts(agent_id, with_energy=False, named=True):
    """Everything the client knows about one living agent that is useful after a fight.
    Each read is guarded on its own: one missing field must not lose the rest."""
    def get(fn, default=None):
        try:
            return fn(agent_id)
        except Exception:
            return default
    x, y, z = Agent.GetXYZ(agent_id)
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    prof = get(Agent.GetProfessionIDs, (0, 0))
    a = {"id": int(agent_id), "xy": (float(x), float(y)), "z": float(z),
         "plane": get(Agent.GetZPlane), "hp": get(Agent.GetHealth), "max_hp": get(Agent.GetMaxHealth),
         "alive": bool(get(Agent.IsAlive, True)), "dead": bool(get(Agent.IsDead, False)),
         "model": get(Agent.GetModelID), "level": get(Agent.GetLevel), "prof": list(prof or (0, 0)),
         "boss": bool(get(Agent.HasBossGlow, False)), "vel": get(Agent.GetVelocityXY),
         "cast": get(Agent.GetCastingSkillID, 0), "atk": bool(get(Agent.IsAttacking, False)),
         "mv": bool(get(Agent.IsMoving, False)), "kd": bool(get(Agent.IsKnockedDown, False)),
         "hex": bool(get(Agent.IsHexed, False)), "ench": bool(get(Agent.IsEnchanted, False)),
         "cond": bool(get(Agent.IsConditioned, False)), "dw": bool(get(Agent.IsDeepWounded, False)),
         "regen": get(Agent.GetHealthRegen), "weapon": get(Agent.GetWeaponType),
         "name": ""}        # no name lookups here: asking the game for names right after a shrine jump crashed it; the model id identifies the type
    if isinstance(a["weapon"], tuple):
        a["weapon"] = a["weapon"][0]
    if with_energy:
        a["en"] = get(Agent.GetEnergy)
    return a


def fight_snapshot(radius=3200.0, known=()):
    """What is around the leader right now, for the fight recorder. None when there is nothing
    hostile within `radius` (the common case: one cheap pass over the enemy list)."""
    me = Player.GetAgentID()
    px, py = Player.GetXY()
    r2 = radius * radius
    near = []
    for agent_id in AgentArray.GetEnemyArray():
        try:
            x, y = Agent.GetXY(agent_id)
            if (x - px) ** 2 + (y - py) ** 2 <= r2:
                near.append(agent_id)
        except Exception:
            continue
    if not near:
        return None
    enemies = [f for f in (_agent_facts(i, named=i not in known) for i in near) if f is not None]
    allies = []
    for kind, ids in (("party", AgentArray.GetAllyArray()), ("spirit", AgentArray.GetSpiritPetArray()),
                      ("minion", AgentArray.GetMinionArray())):
        for agent_id in ids:
            try:
                if agent_id == me:
                    continue
                x, y = Agent.GetXY(agent_id)
                if (x - px) ** 2 + (y - py) ** 2 > 3000.0 ** 2:
                    continue
                f = _agent_facts(agent_id, with_energy=True, named=agent_id not in known)
                if f is not None:
                    f["kind"] = kind
                    allies.append(f)
            except Exception:
                continue
    mine = _agent_facts(me, with_energy=True, named=me not in known) or {"id": int(me), "xy": (px, py)}
    mine["kind"] = "leader"
    try:
        mine["bar"] = [int(s) for s in SkillBar.GetSkillbar()]
    except Exception:
        pass
    try:
        if mine.get("dead"):
            raise ValueError("dead")
        from Py4GWCoreLib.Effect import Effects
        mine["effects"] = sorted({int(e.skill_id) for e in Effects.GetEffects(me)})
    except Exception:
        pass
    allies.insert(0, mine)
    out = {"me": {"xy": (px, py), "z": mine.get("z"), "plane": mine.get("plane"), "dead": bool(mine.get("dead"))},
           "allies": allies, "enemies": enemies}
    for key, fn in (("foes", foes_remaining), ("killed", foes_killed), ("morale", my_morale)):
        try:
            out[key] = fn()
        except Exception:
            out[key] = None
    return out


def my_health():
    """The leader's health, 0-1 (1.0 if it cannot be read)."""
    try:
        return float(Agent.GetHealth(Player.GetAgentID()))
    except Exception:
        return 1.0


def player_level():
    """(plane, height) of the leader: the piece of ground he stands on and how high it is."""
    try:
        me = Player.GetAgentID()
        return int(Agent.GetZPlane(me)), float(Agent.GetXYZ(me)[2])
    except Exception:
        return None, None


def foes_remaining():
    """None when the area cannot be vanquished (normal mode, or not a vanquish area)."""
    if not Map.IsVanquishable() or not Party.IsHardMode():
        return None
    return Map.GetFoesToKill()


def foes_killed():
    return Map.GetFoesKilled()


def party_defeated():
    return bool(Party.IsPartyDefeated())


def read_carto_grid():
    ctx = GWContext.World.GetContext()
    if not ctx:
        return None
    dims = list(ctx.h05B4)
    words = [int(w) for w in (GW_Array_Value_View(ctx.cartographed_areas_array, c_uint32).to_list() or [])]
    if not dims[0] or not dims[1] or not words:
        return None
    return CartoGrid(dims[0], dims[1], words)


def read_projection():
    """World-map anchor of the current map, the way Toolbox computes it."""
    left, top, _right, _bottom = Map.GetMapWorldMapBounds()
    min_x, _min_y, _max_x, max_y = Map.GetMapBoundaries()
    if left == 0 and top == 0:
        return None
    return Projection(left, top, min_x, max_y)


def player_professions():
    return Agent.GetProfessionIDs(Player.GetAgentID())


def capture_signets():
    return SkillBar.GetSkillbar().count(SIGNET_OF_CAPTURE)


def skill_learnt(skill_id):
    return bool(SkillBar.IsSkillLearnt(skill_id))


exit_note = ""


def read_exits():
    """Places that lead out of this area (walking into one resets a vanquish).

    Two sources, because the first misses some: (1) portal props Reforged recognises, and
    (2) arrival points that belong to a different map. Those sit just beside a portal, and
    Camp Rankor's portal in Snake Dance was only found this way.
    """
    global exit_note
    out = [(p.x, p.y) for p in Map.Pathing.GetTravelPortals()]
    portals = len(out)
    try:
        own = f"{Map.GetMapID():04d}"
        spawns1, spawns2, _spawns3 = Map.Pathing.GetSpawns()
        for s in list(spawns1) + list(spawns2):
            if s.tag and s.tag != own:
                out.append((s.x, s.y))
        exit_note = (f"{portals} portals, {len(out) - portals} arrival points "
                     f"(spawn lists {len(spawns1)}/{len(spawns2)}, own tag {own})")
    except Exception as e:
        exit_note = f"{portals} portals; arrival points failed: {e!r}"
    return out


FRONT_LINE = {1: 0, 10: 1, 9: 2, 2: 3, 7: 4, 8: 5}     # profession -> preference: armoured fighters, then spirit layers


def front_line_heroes(limit=2):
    """Heroes fit to meet a group first: [(agent id, profession)]. Warriors, dervishes, paragons,
    rangers and assassins wear real armour; ritualists stand behind a wall of spirits and their
    protection only exists once they have started casting. Mesmers, elementalists, necromancers
    and monks are never sent ahead. Empty if the team has none of the former."""
    out = []
    for h in Party.GetHeroes():
        try:
            agent = int(h.agent_id)
            if not agent or not Agent.IsValid(agent) or not Agent.IsAlive(agent):
                continue
            prof = int(Agent.GetProfessionIDs(agent)[0])
            if prof in FRONT_LINE:
                out.append((FRONT_LINE[prof], agent, prof))
        except Exception:
            continue
    return [(a, p) for _rank, a, p in sorted(out)[:limit]]


def flag_all_heroes(x, y):
    """The party-wide flag: every hero (and henchman) goes to this spot and stays."""
    Party.Heroes.FlagAllHeroes(x, y)


def flag_heroes_spread(places, usable=None):
    """Flag each of our own heroes to its own place (fighters to the first places), so they do
    not stand in one heap. `usable(x, y)`: is this spot walkable; a place that is not is
    replaced by the middle one. Returns how many were placed; 0 means there are no heroes of
    ours to place (henchmen, other players) and the caller should use the party flag."""
    heroes = []
    for h in Party.GetHeroes():
        try:
            agent = int(h.agent_id)
            if agent and Agent.IsValid(agent) and Agent.IsAlive(agent):
                prof = int(Agent.GetProfessionIDs(agent)[0])
                heroes.append((FRONT_LINE.get(prof, 9), agent))
        except Exception:
            continue
    heroes.sort()
    done = 0
    for (_rank, agent), (x, y) in zip(heroes, places):
        if usable is not None and not usable(x, y):
            x, y = places[0]
        try:
            Party.Heroes.FlagHero(agent, x, y)
            done += 1
        except Exception:
            continue
    return done


def flag_hero(agent_id, x, y):
    Party.Heroes.FlagHero(agent_id, x, y)


def unflag_heroes(agent_ids=()):
    """Take down every hero flag, by every route the library offers. Which form a single hero's
    flag wants (its agent id, or its place in the party) differs between Reforged's stubs and
    its own hero panel, so both are sent; each is harmless if it is the wrong one."""
    for a in agent_ids:
        try:
            Party.Heroes.UnflagHero(a)
        except Exception:
            pass
    for position in range(0, 8):
        try:
            Party.Heroes.UnflagHero(position)
        except Exception:
            pass
    try:
        import PyParty
        for index in range(1, 8):
            PyParty.unflag_hero(index)
        PyParty.unflag_all()
    except Exception:
        pass
    Party.Heroes.UnflagAllHeroes()


def heroes_flagged():
    """True if any hero, or the whole group, is standing on a flag."""
    try:
        if Party.Heroes.IsAllFlagged():
            return True
        for position in range(1, int(Party.GetHeroCount()) + 1):
            if Party.Heroes.IsHeroFlagged(position):
                return True
    except Exception:
        pass
    return False


def dead_ally_positions(radius=6000.0):
    """Where the party's dead are lying: [(x, y)] within `radius` of us."""
    px, py = Player.GetXY()
    out = []
    for a in AgentArray.GetDeadAllyArray():
        try:
            x, y = Agent.GetXY(a)
            if (x - px) ** 2 + (y - py) ** 2 <= radius * radius:
                out.append((x, y))
        except Exception:
            continue
    return out


def read_travel_portals():
    """Only the doors the game itself marks as portals."""
    try:
        return [(p.x, p.y) for p in Map.Pathing.GetTravelPortals()]
    except Exception:
        return []


def instance_uptime_ms():
    return Map.GetInstanceUptime()


def party_condition(radius=4500.0):
    """How fit the party is to start another fight."""
    me = Player.GetAgentID()
    px, py = Player.GetXY()

    def near(agent_id):
        x, y = Agent.GetXY(agent_id)
        return (x - px) ** 2 + (y - py) ** 2 <= radius * radius

    dead = 0
    for a in AgentArray.GetDeadAllyArray():
        try:
            dead += 1 if near(a) else 0
        except Exception:
            continue
    hps = []
    for a in AgentArray.GetAllyArray():
        try:
            if near(a) and Agent.IsAlive(a):
                hps.append(Agent.GetHealth(a))
        except Exception:
            continue
    return {"player_dead": bool(Agent.IsDead(me)), "hp": Agent.GetHealth(me), "energy": Agent.GetEnergy(me),
            "dead_allies": dead, "ally_hp": (sum(hps) / len(hps)) if hps else 1.0}


def map_unlocked(map_id):
    return bool(Map.IsMapUnlocked(map_id))


def map_name(map_id):
    return Map.GetMapName(map_id)


def vanquished_ids():
    """Map ids this character has vanquished (one bit per map id, read the way Toolbox does)."""
    ctx = GWContext.World.GetContext()
    if not ctx:
        return set()
    words = GW_Array_Value_View(ctx.vanquished_areas_array, c_uint32).to_list() or []
    return {w * 32 + b for w, v in enumerate(words) for b in range(32) if (int(v) >> b) & 1}


# ---- elite capture ----------------------------------------------------------
_elite_cache = {}


def agent_name(agent_id):
    """Display name, or "" if not known. The name lookup is a native call that takes the id on
    trust: asking about an agent that no longer exists (a remembered enemy after it despawned,
    or after a wipe) crashes the game, so the id is checked against the live agents first."""
    try:
        if not agent_id or not Agent.IsValid(agent_id):
            return ""
        return Agent.GetNameByID(agent_id) or ""
    except Exception:
        return ""


def agent_primary(agent_id):
    try:
        if not agent_id or not Agent.IsValid(agent_id):
            return 0
        return int(Agent.GetProfessionIDs(agent_id)[0])
    except Exception:
        return 0


def signet_slot():
    """Bar slot (1-8) holding a Signet of Capture, or 0."""
    for slot in range(1, 9):
        if SkillBar.GetSkillIDBySlot(slot) == SIGNET_OF_CAPTURE:
            return slot
    return 0


def use_signet_on(slot, agent_id):
    if not agent_id or not Agent.IsValid(agent_id):       # the corpse is gone: nothing to target
        return
    Player.ChangeTarget(agent_id)
    SkillBar.UseSkill(slot, agent_id)


def unlearnt_elites(profession):
    """Elite skill ids of a profession this character has not learnt (computed once per profession)."""
    from Py4GWCoreLib.Skill import Skill
    if profession not in _elite_cache:
        ids = []
        for sid in range(1, 3500):
            try:
                if Skill.Flags.IsElite(sid) and int(Skill.GetProfession(sid)[0]) == profession:
                    ids.append(sid)
            except Exception:
                continue
        _elite_cache[profession] = ids
    return [sid for sid in _elite_cache[profession] if not SkillBar.IsSkillLearnt(sid)]


def capture_dialog_pick(skill_ids):
    """If the capture window offers one of these skills, click it and return its id (else 0)."""
    import PyGameThread
    from Py4GWCoreLib.FrameTree import Frame
    from Py4GWCoreLib.Skill import Skill
    for sid in skill_ids:
        attribute = Skill.Attribute.GetAttribute(sid)
        frame = Frame.capture_skill(attribute if isinstance(attribute, int) else 1, sid)
        if frame.exists:
            PyGameThread.enqueue(lambda f=frame: f.mouse_click_action(0, 0))
            return sid
    return 0


def capture_dialog_confirm():
    from Py4GWCoreLib.FrameTree import Frame, FrameId
    button = Frame(FrameId.SkillCaptureDialog.Content)
    if not button.exists:
        return False
    button.click()
    return True


# ---- campaign helpers ---------------------------------------------------------
def map_world_pos(map_id):
    """Centre of a map's rectangle on the world map, plus its continent, for ordering a queue."""
    from Py4GWCoreLib.native_src.methods.MapMethods import MapMethods
    info = MapMethods.GetMapInfo(map_id)
    if info is None:
        return None
    x0, y0, x1, y1 = info.icon_start_x, info.icon_start_y, info.icon_end_x, info.icon_end_y
    if not (x0 or y0 or x1 or y1):
        x0, y0, x1, y1 = info.icon_start_x_dupe, info.icon_start_y_dupe, info.icon_end_x_dupe, info.icon_end_y_dupe
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0, int(info.continent)


def bar_template():
    from Py4GWCoreLib.py4gwcorelib_src.Utils import Utils
    return Utils.GenerateSkillbarTemplate() or ""


def load_bar_template(template):
    SkillBar.LoadSkillTemplate(template)


def template_signets(template):
    from Py4GWCoreLib.py4gwcorelib_src.Utils import Utils
    try:
        return list(Utils.ParseSkillbarTemplate(template)[3]).count(SIGNET_OF_CAPTURE)
    except Exception:
        return 0


def buy_signet():
    Player.BuySkill(SIGNET_OF_CAPTURE)


def move_to(x, y):
    Player.Move(x, y)


def bar_with_signets(template, count, slots, secondary=None):
    """The saved bar with `count` Signets of Capture on it. Signets already on the saved bar are
    kept; any more go into `slots` (1-8), in that order. Returns "" if it cannot be built."""
    from Py4GWCoreLib.py4gwcorelib_src.Utils import Utils
    try:
        primary, saved_secondary, attributes, skills = Utils.ParseSkillbarTemplate(template)
        secondary = saved_secondary if secondary is None else secondary     # keep a secondary we switched to
        skills = (list(skills) + [0] * 8)[:8]
        have = skills.count(SIGNET_OF_CAPTURE)
        for slot in slots:
            if have >= count:
                break
            if 1 <= slot <= 8 and skills[slot - 1] != SIGNET_OF_CAPTURE:
                skills[slot - 1] = SIGNET_OF_CAPTURE
                have += 1
        return Utils.GenerateSkillbarTemplateFrom(primary, secondary, attributes, skills) or ""
    except Exception:
        return ""


def unlocked_secondaries():
    """Professions this character may use as a secondary (empty set if it cannot be read)."""
    try:
        ctx = GWContext.World.GetContext()
        me = Player.GetAgentID()
        for state in (ctx.party_profession_states or []):
            if int(state.agent_id) == me:
                return {p for p in range(1, 11) if state.IsProfessionUnlocked(p)}
    except Exception:
        pass
    return set()


def template_with_secondary(template, secondary):
    from Py4GWCoreLib.py4gwcorelib_src.Utils import Utils
    try:
        primary, _old, attributes, skills = Utils.ParseSkillbarTemplate(template)
        return Utils.GenerateSkillbarTemplateFrom(primary, secondary, attributes, list(skills)) or ""
    except Exception:
        return ""


def change_secondary_direct(profession):
    """The native call is named for heroes; index 0 is the player in the library it mirrors."""
    SkillBar.ChangeHeroSecondary(0, profession)


def template_secondary(template):
    from Py4GWCoreLib.py4gwcorelib_src.Utils import Utils
    try:
        return int(Utils.ParseSkillbarTemplate(template)[1])
    except Exception:
        return None


# ---- team builds -------------------------------------------------------------------
def max_party_size():
    return int(Map.GetMaxPartySize() or 0)


def party_hero_ids():
    try:
        return [int(h.hero_id) for h in Party.GetHeroes()]
    except Exception:
        return []


def map_max_party(map_id):
    """Largest party the given map allows (0 if unknown)."""
    try:
        from Py4GWCoreLib.native_src.methods.MapMethods import MapMethods
        info = MapMethods.GetMapInfo(map_id)
        return int(info.max_party_size) if info is not None else 0
    except Exception:
        return 0


def player_dead_safe():
    try:
        return bool(Agent.IsDead(Player.GetAgentID()))
    except Exception:
        return True


def my_morale():
    """The game's morale number: 100 is neutral, 40 is the -60% limit, above 100 is a bonus."""
    try:
        return int(Player.GetMorale() or 100)
    except Exception:
        return 100


def party_low_morale():
    try:
        vals = [int(m) for _a, m in Party.GetPartyMorale() if m]
        return min(vals) if vals else my_morale()
    except Exception:
        return my_morale()


def party_makeup():
    """Who is in the party, as the game reports it: human players (you included), heroes
    (anyone's) and henchmen."""
    try:
        return {"players": int(Party.GetPlayerCount()), "heroes": int(Party.GetHeroCount()),
                "henchmen": int(Party.GetHenchmanCount())}
    except Exception:
        return {"players": 1, "heroes": 0, "henchmen": 0}


def my_hero_ids():
    """Hero ids of the heroes *I* brought, in party order (other players' heroes left out)."""
    try:
        me = Player.GetLoginNumber()
        return [int(h.hero_id) for h in Party.GetHeroes() if h.owner_player_id == me]
    except Exception:
        return []


def party_accounts():
    """The other accounts of this party that Py4GW is running on, from the shared memory the
    Reforged widgets keep: [{email, name, same_map, following, combat}]. A player in the party
    who is not in this list is one the bot cannot steer (no Py4GW there, or a real person)."""
    out = []
    try:
        from Py4GWCoreLib import GLOBAL_CACHE
        from Py4GWCoreLib.routines_src.behaviourtrees_src import botting_consumables as bc
        me, here, pid = str(Player.GetAccountEmail() or ""), int(Map.GetMapID() or 0), int(Party.GetPartyID() or 0)
        for a in GLOBAL_CACHE.ShMem.GetAllAccountData():
            email = str(getattr(a, "AccountEmail", "") or "")
            if not email or email == me or not bool(getattr(a, "IsAccount", True)):
                continue
            same_map = bc.account_map_id(a) == here
            if not same_map or int(getattr(a.AgentPartyData, "PartyID", -1)) != pid:
                continue
            opt = GLOBAL_CACHE.ShMem.GetHeroAIOptionsFromEmail(email)
            out.append({"email": email, "name": str(getattr(a.AgentData, "CharacterName", "") or getattr(a, "AccountName", "") or email),
                        "same_map": same_map,
                        "following": bool(opt.Following) if opt is not None else None,
                        "combat": bool(opt.Combat) if opt is not None else None})
    except Exception:
        pass
    return out


def party_accounts_cached():
    import time as _t
    c = party_accounts_cached.__dict__
    if _t.time() - c.get("t", 0.0) > 2.0:
        c["t"], c["v"] = _t.time(), party_accounts()
    return c.get("v", [])


def set_heroai_option(email, name, value):
    from Py4GWCoreLib import GLOBAL_CACHE
    GLOBAL_CACHE.ShMem.SetHeroAIPropertyByEmail(email, name, value)


def return_to_outpost():
    """The party leader's 'Return to Outpost' after the party has resigned: takes everyone."""
    Party.ReturnToOutpost()


def other_accounts_here():
    """Other accounts of this multibox set that are in this map (from the shared memory the
    Reforged widgets keep). Empty when playing one account."""
    import time as _t
    c = other_accounts_here.__dict__
    if _t.time() - c.get("t", 0.0) < 2.0:
        return c.get("v", [])
    c["t"] = _t.time()
    out = c["v"] = []
    try:
        from Py4GWCoreLib import GLOBAL_CACHE
        from Py4GWCoreLib.routines_src.behaviourtrees_src import botting_consumables as bc
        me, here = str(Player.GetAccountEmail() or ""), int(Map.GetMapID() or 0)
        for a in GLOBAL_CACHE.ShMem.GetAllAccountData():
            email = str(getattr(a, "AccountEmail", "") or "")
            if email and email != me and bc.account_map_id(a) == here:
                out.append(email)
    except Exception:
        pass
    return out


def party_count():
    try:
        return int(Party.GetPartySize())
    except Exception:
        return 0


def shrink_party():
    """Send every hero and henchman home. The game refuses to map-travel a party to an outpost
    that allows fewer members than it has (it shows a 'party too large' window and stays put)."""
    Party.Heroes.KickAllHeroes()
    try:
        for h in Party.GetHeroes():               # and one by one, in case the group call is ignored
            Party.Heroes.KickHero(int(h.hero_id))
    except Exception:
        pass
    try:
        for h in Party.GetHenchmen():
            Party.Henchmen.KickHenchman(h.agent_id)
    except Exception:
        pass


def kick_all_heroes():
    Party.Heroes.KickAllHeroes()


def add_hero(hero_id):
    Party.Heroes.AddHero(hero_id)


def load_hero_template(position, template):
    """`position` is the hero's place in the party, 1 = first hero."""
    SkillBar.LoadHeroSkillTemplate(position, template)


def find_toolbox_file(hint=""):
    """The newest GWToolbox++ team-build file: herobuilds.json (current Toolbox, under
    configs/<profile>/) or the old herobuilds.ini. Looks around `hint` (a path given before)
    and in the usual Documents folders. Returns a path or ""."""
    import os
    roots = [os.path.join(os.path.expanduser("~"), "Documents", "GWToolboxpp"),
             os.path.join(os.path.expanduser("~"), "OneDrive", "Documents", "GWToolboxpp")]
    if hint:
        folder = hint if os.path.isdir(hint) else os.path.dirname(hint)
        for _ in range(3):                    # the file's folder and up to two levels above it
            if folder and folder not in roots:
                roots.insert(0, folder)
            if os.path.basename(folder).lower() == "gwtoolboxpp":
                break
            folder = os.path.dirname(folder)
    best = ("", 0.0)
    for root in roots:
        for folder, _dirs, files in os.walk(root):
            for name in ("herobuilds.json", "herobuilds.ini"):
                if name in files:
                    path = os.path.join(folder, name)
                    try:
                        mtime = os.path.getmtime(path)
                    except OSError:
                        continue
                    if mtime > best[1]:
                        best = (path, mtime)
    return best[0]
