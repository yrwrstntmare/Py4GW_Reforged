"""Which weapon set to hold for the fights in front of the party. Pure Python.

The leader keeps a main set (whatever he wore when the area began) and carries others in the
Equipment Pack. A set is a two-handed weapon (staff, bow, ...) or a one-handed weapon with an
optional off-hand (wand + focus, spear + shield). Each set is scored for the fights at hand and
a change is only suggested when another set is clearly better than the one held; the main set
is the default and is always what the party ends an area with.

Item dicts (built by runtime.game.gear_items):
    id, type (Item_enums.ItemType int), req_attr, req (attribute and level the item asks for),
    energy, armor (shield armor at / below its requirement as a pair), and `mods`, a list of
    (name, subtype, value): ArmorPlusVsSpecies / ArmorPlusVsElemental / ArmorPlusVsPhysical /
    ArmorPlusVsDamage / ArmorPlus / HealthPlus / EnergyPlus / AttributePlusOne /
    HalvesCastingTimeAttribute / HalvesSkillRechargeAttribute / ... (others are ignored).
Profile (what the area throws at the party):
    species: {name: share of the foes}, elemental, physical: shares of the damage kind.
Me: attrs {attribute id: level}.
"""

TWO_HANDED = {5, 15, 26, 32, 35}          # bow, hammer, staff, daggers, scythe
ONE_HANDED = {2, 22, 27, 36}              # axe, wand, sword, spear
OFF_HAND = {12, 24}                       # focus, shield
SWAP_MARGIN = 6.0                         # how much better another set must be to change to it

SPECIES_WORDS = (                         # agent name word -> the game's bane species
    ("charr", "Charr"), ("troll", "Trolls"), ("dwarf", "Dwarves"), ("dwarven", "Dwarves"),
    ("stone summit", "Dwarves"), ("tengu", "Tengus"), ("caromi", "Tengus"), ("avicara", "Tengus"),
    ("ogre", "Ogres"), ("giant", "Giants"), ("dragon", "Dragons"), ("drake", "Dragons"),
    ("demon", "Demons"), ("abyssal", "Demons"), ("skeleton", "Skeletons"), ("bone ", "Undead"),
    ("zombie", "Undead"), ("lich", "Undead"), ("ghost", "Undead"), ("spectral", "Undead"),
    ("undead", "Undead"),
)

ELEMENTAL_PROF = {"Elementalist"}
PHYSICAL_PROF = {"Warrior", "Ranger", "Assassin", "Dervish", "Paragon"}


def species_of(name):
    n = (name or "").lower()
    for word, sp in SPECIES_WORDS:
        if word in n:
            return sp
    return None


def profile(foes):
    """foes: [(name, primary profession name or "")] for the enemies of this area seen so far."""
    if not foes:
        return {"species": {}, "elemental": 0.0, "physical": 0.0, "count": 0}
    n = float(len(foes))
    sp = {}
    el = ph = 0
    for name, prof in foes:
        s = species_of(name)
        if s:
            sp[s] = sp.get(s, 0) + 1
        if prof in ELEMENTAL_PROF:
            el += 1
        elif prof in PHYSICAL_PROF:
            ph += 1
    return {"species": {k: v / n for k, v in sp.items()}, "elemental": el / n, "physical": ph / n, "count": len(foes)}


def sets_from(items):
    """Every holdable set: (main, off or None)."""
    mains = [i for i in items if i["type"] in ONE_HANDED]
    twos = [i for i in items if i["type"] in TWO_HANDED]
    offs = [i for i in items if i["type"] in OFF_HAND]
    out = [(t, None) for t in twos]
    for m in mains:
        if not offs:
            out.append((m, None))
        for o in offs:
            out.append((m, o))
    return out


def _meets(item, attrs):
    req_attr, req = item.get("req_attr"), item.get("req") or 0
    if not req or req_attr is None:
        return True
    return attrs.get(int(req_attr), 0) >= req


def score_item(item, prof, attrs):
    """(base, situational) worth of one item for a caster. Base: what it is worth anywhere
    (energy, plain armor, health, casting mods for attributes the leader uses). Situational:
    what it is worth against THESE foes (armor against their kind of damage or their species).
    Weapon damage counts for nothing. An item whose requirement is not met keeps its base
    energy and its lower shield armor only - its upgrades do not work."""
    if item is None:
        return 0.0, 0.0
    ok = _meets(item, attrs)
    base = float(item.get("energy") or 0)
    sit = 0.0
    armor = item.get("armor") or (0, 0)
    if armor and any(armor):
        base += 1.2 * (armor[0] if ok else armor[1])
    if not ok:
        return base, sit
    for name, sub, val in item.get("mods") or ():
        v = float(val or 0)
        if name == "ArmorPlusVsSpecies":
            sit += 1.2 * v * prof["species"].get(str(sub), 0.0)
        elif name == "ArmorPlusVsElemental":
            sit += 1.2 * v * prof["elemental"]
        elif name in ("ArmorPlusVsPhysical", "ArmorPlusVsPhysical2"):
            sit += 1.2 * v * prof["physical"]
        elif name == "ArmorPlusVsDamage":
            sit += 0.3 * v * prof["elemental"]          # one element of several: a quarter of the elemental share
        elif name == "ArmorPlus":
            base += 1.2 * v
        elif name in ("HealthPlus", "HealthPlus2"):
            base += v / 10.0
        elif name in ("EnergyPlus", "Energy2"):
            base += v
        elif name in ("HalvesCastingTimeAttribute", "HalvesSkillRechargeAttribute", "AttributePlusOne"):
            if sub is not None and attrs.get(int(sub), 0) >= 8:
                base += 8.0 if name != "AttributePlusOne" else 10.0
        elif name in ("HalvesCastingTimeGeneral", "HalvesSkillRechargeGeneral"):
            base += 5.0
    return base, sit


def score_set(st, prof, attrs):
    a, b = score_item(st[0], prof, attrs), score_item(st[1], prof, attrs)
    return a[0] + b[0], a[1] + b[1]


BASE_LOSS_LIMIT = 15.0      # most base worth (energy, armor, casting mods) a situational set may give up


def choose(items, held_ids, main_ids, prof, attrs, margin=SWAP_MARGIN):
    """The set to hold: (main item, off item or None, gain, base given up, reason), or None when
    nothing should change. The main set is the one you chose; another set is taken only for
    what it does against the foes at hand (situational worth at least `margin` above the main
    set's) and only if it gives up little of what the main set is good for everywhere. With no
    reason any more, back to the main set. With no foes seen yet, nothing changes."""
    sets = sets_from(items)
    if not sets or not prof.get("count"):
        return None
    ids = lambda st: {i["id"] for i in st if i is not None}
    main = next((st for st in sets if ids(st) == set(main_ids)), None)
    if main is None:
        return None                       # the main set is not all here (moved away by hand): leave it
    held = next((st for st in sets if ids(st) == set(held_ids)), None)
    m_base, m_sit = score_set(main, prof, attrs)

    def gain(st):
        b, t = score_set(st, prof, attrs)
        return t - m_sit, m_base - b

    good = [st for st in sets if st is not main and gain(st)[0] >= margin and gain(st)[1] <= BASE_LOSS_LIMIT]
    best = max(good, key=lambda st: gain(st)[0] - 0.5 * max(0.0, gain(st)[1])) if good else main
    if held is not None and ids(best) == ids(held):
        return None
    g, loss = gain(best) if best is not main else (0.0, 0.0)
    return best[0], best[1], round(g, 1), round(loss, 1), "main set" if best is main else "better for these foes"
