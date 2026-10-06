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
        self._sent, self._last_morale_use = {}, 0.0

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
        return C.Facts(deaths=eng.deaths, foes_left=eng.foes_remaining, my_morale=int(Player.GetMorale() or 100),
                       party_morale=party, in_bags=in_bags, running=running, targets=targets)

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
        if item.kind != C.EFFECT:
            self._last_morale_use = now
        log.event("pcon", item=item.name, kind=item.kind, why=why, deaths=facts.deaths, foes=facts.foes_left,
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
