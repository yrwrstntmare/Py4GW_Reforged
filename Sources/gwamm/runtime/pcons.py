"""Consumables ("pcons") kept up during a vanquish.

Uses the Reforged library's own helpers (routines_src/behaviourtrees_src/botting_consumables):
which item gives which effect, the check that the effect is not already on, and the message that
asks the other accounts of a multibox party to use theirs. Nothing is bought or moved: an item
that is not in the bags is simply skipped.

When they are used is this module's rule (cfg.pcons_mode):
  0  never
  1  only once the area is proving hard: after cfg.pcons_after_deaths deaths in this run
  2  from the first fight on
and never for the tail of an area (fewer than cfg.pcons_min_foes foes left), so a half-hour
item is not spent on the last few foes.
"""
import time

GROUPS = (("conset", "Conset (Essence, Grail, Armor): whole party, used by you"),
          ("pcons", "Personal items (cupcake, egg, candy, pie, kabob, soup, salad, war supplies)"))


def _specs(group):
    from Py4GWCoreLib.routines_src.behaviourtrees_src import botting_consumables as bc
    return bc.consumable_specs(group)


class Pcons:
    def __init__(self, session):
        self.session = session
        self._next = 0.0
        self._sent = {}          # model id -> when the other accounts were last asked
        self._specs = None
        self._missing = set()
        self.note = ""
        self.used = 0

    def wanted(self, eng):
        cfg = self.session.cfg
        if cfg.pcons_mode <= 0 or not cfg.do_vanquish:
            return False, "off"
        left = eng.foes_remaining
        if left is None or left <= 0:
            return False, "nothing left to fight"
        if left < cfg.pcons_min_foes and eng.deaths < cfg.pcons_after_deaths:
            return False, f"only {left} foes left"
        if cfg.pcons_mode == 1 and eng.deaths < cfg.pcons_after_deaths:
            return False, f"held back until {cfg.pcons_after_deaths} deaths (now {eng.deaths})"
        return True, "in use"

    def _load(self):
        from Py4GWCoreLib import GLOBAL_CACHE
        cfg, out = self.session.cfg, []
        for group, _label in GROUPS:
            if not getattr(cfg, "pcons_" + group):
                continue
            for model_id, effect_name in _specs(group):
                effect_id = int(GLOBAL_CACHE.Skill.GetID(effect_name) or 0)
                if effect_id > 0:
                    out.append((group, int(model_id), effect_id, effect_name))
        return out

    def reset(self):
        self._specs, self._missing, self._sent = None, set(), {}

    def tick(self, eng, in_fight_or_walking=True):
        """Call every tick while vanquishing. Uses at most one item per call, a few seconds apart."""
        now = time.time()
        if now < self._next:
            return
        self._next = now + 3.0
        ok, why = self.wanted(eng)
        self.note = why
        if not ok:
            return
        from Py4GWCoreLib.routines_src.behaviourtrees_src import botting_consumables as bc
        if self._specs is None:
            self._specs = self._load()
        cfg, log = self.session.cfg, self.session.log
        for group, model_id, effect_id, name in self._specs:
            if cfg.multibox and group == "pcons" and now - self._sent.get(model_id, 0.0) > 90.0:
                # each account eats its own; the receiving side skips it if the effect is already on
                self._sent[model_id] = now
                try:
                    refs = bc.send_consumable_to_accounts(model_id, effect_id)
                    if refs:
                        log.event("pcon", item=name, sent_to=len(refs))
                except Exception as e:
                    log.event("pcon", item=name, error=repr(e))
            if bc.local_effect_active(effect_id):
                continue
            if bc.use_local_consumable(model_id, effect_id):
                self.used += 1
                self._missing.discard(model_id)
                log.event("pcon", item=name, used=True, deaths=eng.deaths, foes=eng.foes_remaining)
                self._next = now + 1.5           # one at a time: the game drops uses sent together
                return
            if model_id not in self._missing:
                self._missing.add(model_id)
                log.event("pcon", item=name, used=False, note="none in the bags")
