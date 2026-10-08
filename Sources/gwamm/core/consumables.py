"""Consumables: what each one is for, and the rule for when to use it.

Pure logic, no game calls. The runtime (runtime/pcons.py) supplies the facts each tick: what is
in the bags, which effects are running, morale, deaths, foes left. `decide` answers with the
one item to use now, or None.

Item ids, effect ids and the kind of rule each item follows come from the Reforged library's
own table (BTUpkeepers.CONSUMABLE_UPKEEP_PRESETS), looked up by `key`. This file adds what the
library does not have: a plain description, who it helps, and the policy.

Nothing is used unless the player switched that item on. There is no "use whatever is there".

Kinds
  effect        gives a timed bonus. Used only while the bonus is not running, and only under
                the item's own setting: OFF, HARD (the area is going badly) or ALWAYS.
  party_morale  lifts death penalty for the whole party. Used when enough of the party is
                penalised (see `dp_members`, `dp_threshold`).
  self_morale   lifts your own death penalty. Used when yours passes `dp_threshold`.

Morale is the game's number: 100 is neutral, 40 is the -60% at which the party is sent home.
"""
OFF, HARD, ALWAYS = 0, 1, 2
EFFECT, PARTY_MORALE, SELF_MORALE = "effect", "party_morale", "self_morale"


class Item:
    def __init__(self, key, name, kind, helps, what):
        self.key, self.name, self.kind, self.helps, self.what = key, name, kind, helps, what

    @property
    def setting(self):
        return "pc_" + self.key


# helps: "party" = one use covers everyone nearby (only the leader uses it); "self" = each
# character needs its own.
CATALOGUE = [
    Item("essence_of_celerity", "Essence of Celerity", EFFECT, "party", "faster movement, attacks and casting"),
    Item("grail_of_might", "Grail of Might", EFFECT, "party", "more health, energy and attributes"),
    Item("armor_of_salvation", "Armor of Salvation", EFFECT, "party", "more armor, less damage taken"),
    Item("birthday_cupcake", "Birthday Cupcake", EFFECT, "self", "more health and energy, faster movement"),
    Item("candy_apple", "Candy Apple", EFFECT, "self", "more health and energy"),
    Item("candy_corn", "Candy Corn", EFFECT, "self", "higher attributes"),
    Item("golden_egg", "Golden Egg", EFFECT, "self", "higher attributes"),
    Item("slice_of_pumpkin_pie", "Slice of Pumpkin Pie", EFFECT, "self", "faster attacks and casting"),
    Item("war_supplies", "War Supplies", EFFECT, "self", "a general combat bonus"),
    Item("drake_kabob", "Drake Kabob", EFFECT, "self", "a little more armor"),
    Item("bowl_of_skalefin_soup", "Bowl of Skalefin Soup", EFFECT, "self", "health regeneration"),
    Item("pahnai_salad", "Pahnai Salad", EFFECT, "self", "a little more health"),
    Item("blue_rock_candy", "Blue Rock Candy", EFFECT, "self", "speed boost (small)"),
    Item("green_rock_candy", "Green Rock Candy", EFFECT, "self", "speed boost (medium)"),
    Item("red_rock_candy", "Red Rock Candy", EFFECT, "self", "speed boost (large)"),
    Item("four_leaf_clover", "Four-Leaf Clover", PARTY_MORALE, "party", "removes death penalty, whole party"),
    Item("oath_of_purity", "Oath of Purity", PARTY_MORALE, "party", "removes death penalty, whole party"),
    Item("powerstone_of_courage", "Powerstone of Courage", PARTY_MORALE, "party", "removes death penalty and raises morale, whole party"),
    Item("honeycomb", "Honeycomb", PARTY_MORALE, "party", "raises morale, whole party"),
    Item("rainbow_candy_cane", "Rainbow Candy Cane", PARTY_MORALE, "party", "raises morale, whole party"),
    Item("elixir_of_valor", "Elixir of Valor", PARTY_MORALE, "party", "raises morale, whole party"),
    Item("peppermint_candy_cane", "Peppermint Candy Cane", SELF_MORALE, "self", "removes your death penalty"),
    Item("wintergreen_candy_cane", "Wintergreen Candy Cane", SELF_MORALE, "self", "reduces your death penalty"),
    Item("refined_jelly", "Refined Jelly", SELF_MORALE, "self", "reduces your death penalty"),
    Item("shining_blade_ration", "Shining Blade Ration", SELF_MORALE, "self", "reduces your death penalty"),
    Item("pumpkin_cookie", "Pumpkin Cookie", SELF_MORALE, "self", "raises your morale"),
    Item("seal_of_the_dragon_empire", "Seal of the Dragon Empire", SELF_MORALE, "self", "raises your morale"),
]
BY_KEY = {i.key: i for i in CATALOGUE}


class Facts:
    """What the runtime saw this tick."""
    def __init__(self, deaths=0, foes_left=None, my_morale=100, party_morale=(), in_bags=(), running=(), targets=None,
                 morale_used=0, timed_used=0, wipes_since_first=0, kills_since_morale=None,
                 no_restart=False, foes_near=0):
        self.deaths = deaths
        self.foes_left = foes_left
        self.my_morale = my_morale or 100
        self.party_morale = [m for m in party_morale if m and m > 0]     # one number per member
        self.in_bags = set(in_bags)          # item keys we hold at least one of
        self.running = set(running)          # item keys whose bonus is active on us now
        self.targets = targets or {}         # item key -> morale it stops helping at (library)
        # what has already been spent in this area, and what it bought
        self.morale_used = morale_used               # death-penalty items used here so far
        self.timed_used = timed_used                 # timed bonuses started here so far
        self.wipes_since_first = wipes_since_first   # party wipes here since the first item was used
        self.kills_since_morale = kills_since_morale # foes killed since the last death-penalty item (None: none used)
        self.no_restart = no_restart                 # a wipe here has ended the run before (no shrine to come back at)
        self.foes_near = foes_near                   # living enemies in sight close to the party right now


def area_is_hard(cfg, facts):
    return facts.deaths >= cfg.pcons_after_deaths


def why_not(cfg, facts):
    """A reason nothing at all may be used right now, or "" when items may be considered."""
    if not cfg.pcons_on or not cfg.do_vanquish:
        return "off"
    if facts.foes_left is None:
        return "not a vanquish"
    if facts.foes_left <= 0:
        return "nothing left to fight"
    # Items are for turning an area round, not for feeding one that is being lost anyway.
    if facts.wipes_since_first >= int(cfg.pcons_stop_wipes):
        return (f"stopped for this area: the party has wiped {facts.wipes_since_first} times since items were "
                f"first used here, they are not turning it round")
    return ""


def decide(cfg, facts):
    """Returns (Item, reason) for the one thing to use now, or (None, reason)."""
    blocked = why_not(cfg, facts)
    if blocked:
        return None, blocked
    limit = 100 - int(cfg.pcons_dp_threshold)            # morale at or under this is "penalised"
    hard = area_is_hard(cfg, facts)
    tail = facts.foes_left < cfg.pcons_min_foes

    # 1. Death penalty first: it is what ends runs.
    party = facts.party_morale or [facts.my_morale]
    low = sum(1 for m in party if m <= limit)
    morale_ok, morale_why = True, ""
    if facts.morale_used >= int(cfg.pcons_max_morale):
        morale_ok, morale_why = False, f"death-penalty items: the limit of {cfg.pcons_max_morale} for one area is used up"
    elif facts.kills_since_morale is not None and facts.kills_since_morale < int(cfg.pcons_morale_worth):
        morale_ok = False
        morale_why = (f"death-penalty items held: the last one bought only {facts.kills_since_morale} kills "
                      f"(another is not used until {cfg.pcons_morale_worth})")
    # Whoever is one death from the floor (-60%) decides it too: a party all the way down that
    # is beaten once more is sent back to the outpost by the game, and the whole run is lost.
    floor = sum(1 for m in party if m <= 55)
    critical = facts.my_morale <= 55 or floor >= 2
    if morale_ok and (low >= max(1, int(cfg.pcons_dp_members)) or critical) and facts.foes_left >= 8:
        for it in CATALOGUE:
            if (it.kind == PARTY_MORALE and getattr(cfg, it.setting) and it.key in facts.in_bags
                    and min(party) < facts.targets.get(it.key, 100)):
                if critical:
                    return it, "one more death and the game sends the party back to the outpost"
                return it, f"{low} of the party at {100 - limit}% death penalty or worse"
    if morale_ok and facts.my_morale <= limit and facts.foes_left >= 8:
        for it in CATALOGUE:
            if (it.kind == SELF_MORALE and getattr(cfg, it.setting) and it.key in facts.in_bags
                    and facts.my_morale < facts.targets.get(it.key, 100)):
                return it, f"your death penalty is {100 - facts.my_morale}%"

    # 2. Timed bonuses, each under its own setting.
    waiting = morale_why if (low or facts.my_morale <= limit) else ""
    if facts.timed_used >= int(cfg.pcons_max_timed):
        return None, waiting or f"timed bonuses: the limit of {cfg.pcons_max_timed} for one area is used up"
    for it in CATALOGUE:
        if it.kind != EFFECT:
            continue
        mode = int(getattr(cfg, it.setting))
        if mode == OFF or it.key in facts.running or it.key not in facts.in_bags:
            continue
        # Where one wipe ends the run, waiting for deaths is waiting too long: the bonus goes on
        # for the first fight big enough to cost lives, and a short tail does not hold it back.
        last_chance = facts.no_restart and facts.foes_near >= int(cfg.pcons_big_fight) and facts.foes_left >= 15
        if last_chance:
            return it, f"a wipe ends the run in this area, and {facts.foes_near} enemies are close"
        if mode == HARD and not hard:
            why = (f"{it.name} held for a fight of {cfg.pcons_big_fight}+ (a wipe ends the run here)" if facts.no_restart
                   else f"{it.name} held until {cfg.pcons_after_deaths} deaths (now {facts.deaths})")
            waiting = waiting or why
            continue
        if tail:                         # a half-hour bonus is not started for the last few foes, however it is going
            waiting = waiting or f"{it.name} not started with only {facts.foes_left} foes left"
            continue
        return it, ("the area is going badly" if mode == HARD else "set to always")
    return None, waiting or "nothing needed"
