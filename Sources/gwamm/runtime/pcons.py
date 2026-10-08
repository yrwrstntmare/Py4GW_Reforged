"""Consumables at run time: gather the facts, ask core/consumables.decide, use one item.

Item and effect ids come from the Reforged library's own table
(BTUpkeepers.CONSUMABLE_UPKEEP_PRESETS); the rule for when is in core/consumables.py.
Nothing is bought or moved, and an item the player has not switched on is never touched.
"""
import time

from ..core import consumables as C


def _presets():
    from Py4GWCoreLib.routines_src.behaviourtrees_src.upkeepers import BTUpkeepers
    return BTUpkeepers.CONSUMABLE_UPKEEP_PRESETS


# Summoning stones, by the names in Reforged's ModelID table. Each calls an extra ally to fight
# beside the party. Order: the reusable crystal first, then the common ones. (The merchant
# stone is not an ally, and the Igneous stone only works below level 20: both left out.)
SUMMON_STONES = ("Legionnaire_Summoning_Crystal", "Mysterious_Summon", "Zaishen_Summon", "Mystical_Summon",
                 "Automaton_Summon", "Tengu_Summon", "Imperial_Guard_Summon", "Shining_Blade_Summon",
                 "Ghastly_Summon", "Celestial_Summon", "Amber_Summon", "Arctic_Summon", "Chitinous_Summon",
                 "Demonic_Summon", "Fossilized_Summon", "Frosty_Summon", "Gelatinous_Summon", "Jadeite_Summon",
                 "Mischievous_Summon")


def _no_restart_path():
    import os
    import PySystem
    return os.path.join(PySystem.Console.get_projects_path(), "gwamm_logs", "no_restart.json")


_NO_RESTART = None


def no_restart_maps():
    """Areas where a full wipe has sent the party home with morale to spare (no shrine)."""
    global _NO_RESTART
    if _NO_RESTART is None:
        import json
        try:
            with open(_no_restart_path()) as f:
                _NO_RESTART = set(int(m) for m in json.load(f))
        except Exception:
            _NO_RESTART = set()
    return _NO_RESTART


def note_defeat(map_id):
    """The party was just defeated. If the leader was nowhere near the -60% floor, the area has
    no way back in after a wipe: remember that. Returns True when newly learnt."""
    import json
    try:
        from Py4GWCoreLib import Player
        morale = int(Player.GetMorale() or 100)
    except Exception:
        return False
    maps = no_restart_maps()
    if morale <= 50 or not map_id or int(map_id) in maps:
        return False
    maps.add(int(map_id))
    try:
        with open(_no_restart_path(), "w") as f:
            json.dump(sorted(maps), f)
    except Exception:
        pass
    return True


def summon_stones():
    """[(name, model id, how many in the bags)] for every summoning stone the library knows."""
    from Py4GWCoreLib import GLOBAL_CACHE
    from Py4GWCoreLib.enums_src.Model_enums import ModelID
    out = []
    for name in SUMMON_STONES:
        member = getattr(ModelID, name, None)
        if member is None:
            continue
        try:
            n = int(GLOBAL_CACHE.Inventory.GetModelCount(int(member.value)) or 0)
        except Exception:
            n = 0
        out.append((name.replace("_", " "), int(member.value), n))
    return out


class Pcons:
    def __init__(self, session):
        self.session = session
        self._next = 0.0
        self._ids = None             # key -> (model id, effect id or 0, morale target or None)
        self._sent = {}              # key -> when the other accounts were last asked
        self._last_morale_use = 0.0
        self.note = ""
        self.used = 0
        self.last = ""
        self.spent = {"morale": 0, "timed": 0, "summons": 0, "wipes0": None, "killed_at_morale": None}

    # ---- ids, from the library ----
    def ids(self):
        if self._ids is None:
            from Py4GWCoreLib import GLOBAL_CACHE
            out, table = {}, _presets()
            for it in C.CATALOGUE:
                p = table.get(it.key)
                if not p:
                    continue
                name = str(p.get("effect_name", "") or "")
                effect = int(p.get("effect_id", 0) or 0) or (int(GLOBAL_CACHE.Skill.GetID(name) or 0) if name else 0)
                target = p.get("target_morale")
                out[it.key] = (int(p["model_id"]), effect, int(target) if target is not None else None)
            self._ids = out
        return self._ids

    def reset(self):
        """A new area: nothing spent here yet."""
        self._sent, self._last_morale_use = {}, 0.0
        self.spent = {"morale": 0, "timed": 0, "summons": 0, "wipes0": None, "killed_at_morale": None}

    @staticmethod
    def _wipes(eng):
        return sum(int(w[2]) for w in getattr(eng, "wipes", ()))

    def count(self, key):
        """How many of this item are in the bags (for the options window)."""
        try:
            from Py4GWCoreLib import GLOBAL_CACHE
            return int(GLOBAL_CACHE.Inventory.GetModelCount(self.ids()[key][0]) or 0)
        except Exception:
            return 0

    # ---- facts ----
    def facts(self, eng):
        from Py4GWCoreLib import GLOBAL_CACHE, Party, Player
        from Py4GWCoreLib.Map import Map
        from Py4GWCoreLib.routines_src.behaviourtrees_src import botting_consumables as bc
        cfg, ids = self.session.cfg, self.ids()
        in_bags, running, targets = set(), set(), {}
        for it in C.CATALOGUE:
            if it.key not in ids or not getattr(cfg, it.setting):
                continue                              # switched off: not even looked at
            model, effect, target = ids[it.key]
            if int(GLOBAL_CACHE.Inventory.GetFirstModelID(model) or 0) > 0:
                in_bags.add(it.key)
            if effect and bc.local_effect_active(effect):
                running.add(it.key)
            if target is not None:
                targets[it.key] = target
        try:
            party = [int(m) for _agent, m in Party.GetPartyMorale()]
        except Exception:
            party = []
        sp = self.spent
        killed = int(Map.GetFoesKilled() or 0)
        px, py = eng.player_xy
        near = sum(1 for e in eng.mem.enemies.values()
                   if e.alive and e.in_range and (e.xy[0] - px) ** 2 + (e.xy[1] - py) ** 2 <= 1800.0 ** 2)
        return C.Facts(no_restart=self.session.map_id in no_restart_maps(), foes_near=near,
                       deaths=eng.deaths, foes_left=eng.foes_remaining, my_morale=int(Player.GetMorale() or 100),
                       party_morale=party, in_bags=in_bags, running=running, targets=targets,
                       morale_used=sp["morale"], timed_used=sp["timed"],
                       wipes_since_first=0 if sp["wipes0"] is None else max(0, self._wipes(eng) - sp["wipes0"]),
                       kills_since_morale=None if sp["killed_at_morale"] is None else max(0, killed - sp["killed_at_morale"]))

    def _summon(self, eng, now):
        """Call a summoned ally when none is out and the game allows another (a stone leaves
        "Summoning Sickness" for a while). Only with a real fight ahead: not for the last few foes."""
        if now < self.__dict__.get("_summon_next", 0.0):
            return False
        self._summon_next = now + 10.0
        if eng.foes_remaining is not None and eng.foes_remaining < self.session.cfg.pcons_min_foes:
            return False
        from Py4GWCoreLib import GLOBAL_CACHE
        from Py4GWCoreLib.Item import has_active_party_summon, has_summoning_sickness
        if has_summoning_sickness() or has_active_party_summon():
            return False
        for name, model, count in summon_stones():
            if count <= 0:
                continue
            item_id = int(GLOBAL_CACHE.Inventory.GetFirstModelID(model) or 0)
            if item_id <= 0:
                continue
            GLOBAL_CACHE.Inventory.UseItem(item_id)
            self.used += 1
            self.last = name
            self.session.log.event("pcon", item=name, item_kind="summon", left=count - 1, foes=eng.foes_remaining)
            self._next = now + 1.5
            return True
        return False

    def state(self, eng):
        """What the consumable rules can see right now, for the log (written at every death so
        "why was nothing used?" can be answered afterwards)."""
        try:
            f = self.facts(eng)
            item, why = C.decide(self.session.cfg, f)
            return {"my_morale": f.my_morale, "party_morale": f.party_morale, "in_bags": sorted(f.in_bags),
                    "would_use": None if item is None else item.name, "why": why, "spent": dict(self.spent)}
        except Exception as e:
            return {"error": repr(e)}

    # ---- act ----
    def tick(self, eng):
        """Call every tick while vanquishing. At most one item per call, a few seconds apart."""
        now = time.time()
        if now < self._next:
            return
        self._next = now + 3.0
        cfg, log = self.session.cfg, self.session.log
        if not cfg.pcons_on:
            self.note = "off"
            return
        stopped = C.why_not(cfg, self.facts(eng))
        if cfg.summon_stones and not stopped and self.spent["summons"] < 2 and self._summon(eng, now):
            self.spent["summons"] += 1
            if self.spent["wipes0"] is None:
                self.spent["wipes0"] = self._wipes(eng)
            return
        facts = self.facts(eng)
        item, why = C.decide(cfg, facts)
        self.note = why if item is None else f"using {item.name}: {why}"
        if item is None:
            return
        if item.kind != C.EFFECT and now - self._last_morale_use < 20.0:
            return                                    # give the game time to show the new morale
        from Py4GWCoreLib import GLOBAL_CACHE
        from Py4GWCoreLib.routines_src.behaviourtrees_src import botting_consumables as bc
        model, effect, _target = self.ids()[item.key]
        item_id = int(GLOBAL_CACHE.Inventory.GetFirstModelID(model) or 0)
        if item_id <= 0:
            return
        GLOBAL_CACHE.Inventory.UseItem(item_id)
        self.used += 1
        self.last = item.name
        if self.spent["wipes0"] is None:
            self.spent["wipes0"] = self._wipes(eng)
        if item.kind != C.EFFECT:
            self._last_morale_use = now
            self.spent["morale"] += 1
            try:
                from Py4GWCoreLib.Map import Map
                self.spent["killed_at_morale"] = int(Map.GetFoesKilled() or 0)
            except Exception:
                pass
        else:
            self.spent["timed"] += 1
        log.event("pcon", spent=dict(self.spent), item=item.name, item_kind=item.kind, why=why, deaths=facts.deaths, foes=facts.foes_left,
                  my_morale=facts.my_morale, party_morale=facts.party_morale)
        self._next = now + 1.5                        # one at a time: the game drops uses sent together
        # A bonus that only covers its user: the other accounts of a shared party each use their
        # own. Their side checks that the bonus is not already running before using anything.
        if cfg.multibox and item.kind == C.EFFECT and item.helps == "self" and now - self._sent.get(item.key, 0.0) > 90.0:
            self._sent[item.key] = now
            try:
                refs = bc.send_consumable_to_accounts(model, effect)
                if refs:
                    log.event("pcon", item=item.name, sent_to=len(refs))
            except Exception as e:
                log.event("pcon", item=item.name, error=repr(e))
