"""Watches enemy casting closely enough to tell an interrupted spell from a finished one, and
notes which of our heroes had just used an interrupt. The fight recordings sample once a second,
too coarse for this; here every perception tick (4 a second) is used.

Logged:
  enemy_interrupted  an enemy's cast stopped well short of its cast time while it lived, with
                     whether a hero had used an interrupt skill in the second before
  cast_stats         every few minutes of fighting: per enemy skill [seen starting, not stopped
                     by us, stopped by one of our interrupts]
"""
import time

from . import game

_RUPT = {}


def is_interrupt(skill):
    """A skill that interrupts: "Interrupt" in its own description (Cry of Frustration, Power
    Drain, Distracting Shot and the rest), from Reforged's skill table."""
    if skill not in _RUPT:
        d = game.skill_text(skill).lower()
        # stops a skill being used (not "the next attack is interrupted", which only stops
        # attacks: Wandering Eye)
        _RUPT[skill] = ("interrupt" in d and "attack is interrupted" not in d
                        and ("skill" in d or "spell" in d or "action" in d))
    return _RUPT[skill]


class CastWatch:
    def __init__(self, log):
        self.log = log
        self.casting = {}          # enemy id -> (skill, started)
        self.hero_rupt = {}        # hero agent -> (skill, when)
        self.stats = {}            # skill -> [started, finished, cut]
        self._activation = {}
        self._last_stats = time.time()

    def _act(self, skill):
        if skill not in self._activation:
            try:
                from Py4GWCoreLib.Skill import Skill
                self._activation[skill] = float(Skill.Data.GetActivation(skill) or 0.0)
            except Exception:
                self._activation[skill] = 0.0
        return self._activation[skill]

    def tick(self, now, enemies_near, hero_ids):
        """enemies_near: [(agent id, alive)] within fighting distance; hero_ids: hero agent ids."""
        for h in hero_ids:
            c = game.enemy_casting(h)
            if c and is_interrupt(c):
                self.hero_rupt[h] = (c, now)
        seen = set()
        for aid, alive in enemies_near:
            seen.add(aid)
            cur = game.enemy_casting(aid) if alive else 0
            prev = self.casting.get(aid)
            if prev and prev[0] != cur:
                self._ended(aid, prev[0], now - prev[1], alive, now)
                prev = None
            if cur and not prev:
                self.casting[aid] = (cur, now)
                self.stats.setdefault(cur, [0, 0, 0])[0] += 1
            elif not cur:
                self.casting.pop(aid, None)
        for aid in [a for a in self.casting if a not in seen]:
            self.casting.pop(aid, None)
        if now - self._last_stats > 300.0 and self.stats:
            self._last_stats = now
            self.log.event("cast_stats", skills={game_skill_name(k): v for k, v in
                                                 sorted(self.stats.items(), key=lambda kv: -kv[1][0])[:25]})

    def _ended(self, aid, skill, lasted, alive, now):
        act = self._act(skill)
        row = self.stats.setdefault(skill, [0, 0, 0])
        recent = [(h, s) for h, (s, t) in self.hero_rupt.items() if now - t <= 1.5]
        if act >= 0.75 and lasted < act * 0.7 and alive and recent:
            # cut short right after one of our heroes used an interrupt: counted as ours
            row[2] += 1
            self.log.event("enemy_interrupted", agent=aid, skill=game_skill_name(skill), skill_id=skill,
                           lasted=round(lasted, 2), cast_time=act,
                           hero_interrupt=[game_skill_name(s) for _h, s in recent])
        else:
            # finished - or ended early with no interrupt from us (quickened casting, a cast seen
            # late, or one of their own skills): not counted as ours
            row[1] += 1


def game_skill_name(skill):
    try:
        from Py4GWCoreLib.Skill import Skill
        return str(Skill.GetName(skill)).replace("_", " ")
    except Exception:
        return str(skill)
