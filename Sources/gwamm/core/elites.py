"""Which elites to capture on this run. Pure Python.

One run has one primary and one secondary profession, so the choice is:
pick a secondary, then up to `signets` elites from primary + that secondary only.
"""
PROFESSIONS = {1: "Warrior", 2: "Ranger", 3: "Monk", 4: "Necromancer", 5: "Mesmer",
               6: "Elementalist", 7: "Assassin", 8: "Ritualist", 9: "Paragon", 10: "Dervish"}

# How hard the elite is to get somewhere else.
ONLY_HERE, COSTLY, FREE = 3.0, 2.0, 1.0


class Elite:
    def __init__(self, skill_id, name, profession, boss, guaranteed=True, scarcity=COSTLY):
        self.skill_id, self.name, self.profession = skill_id, name, profession
        self.boss, self.guaranteed, self.scarcity = boss, guaranteed, scarcity

    def score(self):
        return self.scarcity * (1.0 if self.guaranteed else 0.6)


def choose(primary, area_elites, learnt, signets=2, unlocked=None):
    """Returns (secondary_profession_or_0, [Elite, ...]).
    `learnt` is a set of skill ids. `unlocked` is the set of secondaries the character
    can switch to (None = assume all)."""
    wanted = [e for e in area_elites if e.skill_id not in learnt]
    if signets <= 0 or not wanted:
        return 0, []
    own = sorted((e for e in wanted if e.profession == primary), key=Elite.score, reverse=True)
    best = (sum(e.score() for e in own[:signets]), 0, own[:signets])
    for prof in sorted({e.profession for e in wanted if e.profession != primary}):
        if unlocked is not None and prof not in unlocked:
            continue
        pool = sorted((e for e in wanted if e.profession in (primary, prof)), key=Elite.score, reverse=True)
        pick = pool[:signets]
        total = sum(e.score() for e in pick)
        if total > best[0] + 1e-9:
            best = (total, prof if any(e.profession == prof for e in pick) else 0, pick)
    return best[1], best[2]


# ---- area data ---------------------------------------------------------
# Snake Dance (map 91). Boss list from the Guild Wars Wiki area page, read once and
# NOT yet checked against the individual boss pages. Skill ids are from Reforged's
# skill_descriptions.json. Scarcity is a placeholder (COSTLY) until the full
# elite-location list is imported.
AREAS = {
    91: [
        Elite(226, "Mind Shock", 6, "Old Red Claw"),
        Elite(236, "Mist Form", 6, "Sala Chillbringer"),
        Elite(409, "Punishing Shot", 2, "Thul Boulderrain", guaranteed=False),
        Elite(448, "Escape", 2, "Whuup Buumbuul"),
        Elite(82, "Mantra of Recall", 5, "Featherclaw"),
        Elite(33, "Illusionary Weaponry", 5, "Didn Hopestealer"),
        Elite(119, "Blood is Power", 4, "Cry Darkday"),
        Elite(121, "Spiteful Spirit", 4, "Sapph Blacktracker"),
        Elite(151, "Feast of Corruption", 4, "Maw the Mountain Heart"),
        Elite(273, "Spell Breaker", 3, "Raptorhawk"),
        Elite(266, "Peace and Harmony", 3, "Marnta Doomspeaker"),
        Elite(294, "Signet of Judgment", 3, "Fawl Driftstalker"),
        Elite(354, "Earth Shaker", 1, "Kor Stonewrath", guaranteed=False),
        Elite(317, "Battle Rage", 1, "Smukk Foombool"),
    ],
    # Spearhead Peak (map 93). From the wiki area page, same caveat as above.
    93: [
        Elite(209, "Mind Freeze", 6, "Maak Frostfriend"),
        Elite(227, "Glimmering Mark", 6, "Edibbo Kepkep"),
        Elite(442, "Ferocious Strike", 2, "Thul The Bull"),
        Elite(404, "Poison Arrow", 2, "Kekona Pippip"),
        Elite(329, "Skull Crack", 1, "Hail Blackice"),
        Elite(365, "Victory is Mine!", 1, "Jono Yawpyawl"),
        Elite(63, "Keystone Signet", 5, "Rune Ethercrash"),
        Elite(79, "Energy Drain", 5, "Sniik Hungrymind"),
        Elite(268, "Unyielding Aura", 3, "Kaia Wupwup"),
        Elite(132, "Plague Signet", 4, "Allobo Dimdim"),
        Elite(151, "Feast of Corruption", 4, "Maw the Mountain Heart", guaranteed=False),
    ],
    # Dreadnought's Drift (map 97). From the wiki's list of elites by capture location.
    97: [
        Elite(151, 'Feast of Corruption', 4, 'Maw the Mountain Heart', guaranteed=False),
    ],
    # Frozen Forest (map 98). From the wiki's list of elites by capture location.
    98: [
        Elite(335, 'Cleave', 1, 'Linka Goldensteel'),
        Elite(318, 'Defy Pain', 1, 'Obrhit Barkwood'),
        Elite(429, "Melandru's Arrows", 2, 'Rensar Mountainsight'),
        Elite(270, 'Life Barrier', 3, 'Esnhal Hardwood'),
        Elite(269, 'Mark of Protection', 3, 'Mesqul Ironhealer'),
        Elite(146, 'Offering of Blood', 4, 'Jollen Steelblight'),
        Elite(107, 'Virulence', 4, 'Unthet Rotwood'),
        Elite(54, 'Crippling Anguish', 5, 'Barl Stormsiege'),
        Elite(239, 'Ward Against Harm', 6, 'Arkhel Havenwood'),
        Elite(237, 'Water Trident', 6, 'Boreal Kubeclaw'),
    ],
    # Grenth's Footprint (map 191). From the wiki's list of elites by capture location.
    191: [
        Elite(375, 'Dwarven Battle Stance', 1, 'Thorgall Bludgeonhammer'),
        Elite(429, "Melandru's Arrows", 2, 'Gargash Thornbeard'),
        Elite(269, 'Mark of Protection', 3, 'Wroth Yakslapper'),
        Elite(151, 'Feast of Corruption', 4, 'Maw the Mountain Heart', guaranteed=False),
        Elite(146, 'Offering of Blood', 4, 'Morgriff Shadestone'),
        Elite(54, 'Crippling Anguish', 5, 'Gorrel Rockmolder'),
        Elite(237, 'Water Trident', 6, 'Flint Fleshcleaver'),
    ],
    # Ice Floe (map 94). From the wiki's list of elites by capture location.
    94: [
        Elite(355, 'Devastating Hammer', 1, 'Jade Armor'),
        Elite(405, 'Oath Shot', 2, 'Jade Bow'),
        Elite(260, 'Aura of Faith', 3, 'Mursaat Monk'),
        Elite(294, 'Signet of Judgment', 3, 'Frostbite'),
        Elite(126, 'Life Transfer', 4, 'Mursaat Necromancer'),
        Elite(79, 'Energy Drain', 5, 'Gambol Headrainer'),
        Elite(39, 'Energy Surge', 5, 'Mursaat Mesmer'),
        Elite(209, 'Mind Freeze', 6, 'Skitt Skizzle'),
        Elite(228, 'Thunderclap', 6, 'Mursaat Elementalist'),
    ],
    # Lornar's Pass (map 90). From the wiki's list of elites by capture location.
    90: [
        Elite(318, 'Defy Pain', 1, 'Clobberhusk'),
        Elite(270, 'Life Barrier', 3, 'Quickroot'),
        Elite(151, 'Feast of Corruption', 4, 'Maw the Mountain Heart', guaranteed=False),
        Elite(146, 'Offering of Blood', 4, 'Tonfor Copperblood'),
        Elite(54, 'Crippling Anguish', 5, 'Erzek Runebreaker'),
        Elite(237, 'Water Trident', 6, 'Chunk Clumpfoot'),
    ],
    # Mineral Springs (map 96). From the wiki's list of elites by capture location.
    96: [
        Elite(389, 'Flourish', 1, 'Syr Honorcrest'),
        Elite(449, 'Practiced Stance', 2, 'Ryk Arrowwing'),
        Elite(262, 'Shield of Judgment', 3, 'Myd Springclaw'),
        Elite(151, 'Feast of Corruption', 4, 'Maw the Mountain Heart', guaranteed=False),
        Elite(91, 'Well of Power', 4, 'Nhy Darkclaw'),
        Elite(47, 'Ineptitude', 5, 'Wyt Sharpfeather'),
        Elite(199, 'Glyph of Energy', 6, 'Hyl Thunderwing'),
        Elite(236, 'Mist Form', 6, 'Ice Beast'),
    ],
}


def _load_generated():
    """The full list (data/elites.json, built by tools_build_elites.py from the Guild Wars
    Wiki's lists of elite skills by capture location). Areas typed in by hand above keep their
    'may not spawn' flags; everything else comes from the file."""
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "elites.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    for mid, rows in data.items():
        mid = int(mid)
        by_hand = {(e.skill_id, e.boss.lower()): e for e in AREAS.get(mid, [])}
        merged = []
        for r in rows:
            old = by_hand.pop((r["id"], r["boss"].lower()), None)
            merged.append(Elite(r["id"], r["name"], r["prof"], r["boss"],
                                guaranteed=(old.guaranteed if old else r.get("guaranteed", True))))
        AREAS[mid] = merged + list(by_hand.values())


_load_generated()


def _fill_scarcity():
    """With the boss lists we have, an elite carried by a single boss is the scarce one."""
    count = {}
    for area in AREAS.values():
        for e in area:
            count[e.skill_id] = count.get(e.skill_id, 0) + 1
    for area in AREAS.values():
        for e in area:
            e.scarcity = ONLY_HERE if count[e.skill_id] == 1 else COSTLY if count[e.skill_id] == 2 else FREE


_fill_scarcity()


def rarity(skill_id, here_map, vanquished=()):
    """How hard this elite is to get somewhere else, judged from the boss lists we have:
      ONLY_HERE  no other listed area has a boss carrying it
      COSTLY     others do, but all of those are already vanquished (a special trip back)
      FREE       another area still to be vanquished has it, so it can be picked up then
    Bosses that only sometimes spawn do not count as a dependable alternative."""
    elsewhere = [m for m, area in AREAS.items() if m != here_map
                 and any(e.skill_id == skill_id and e.guaranteed for e in area)]
    if not elsewhere:
        return ONLY_HERE
    return FREE if any(m not in vanquished for m in elsewhere) else COSTLY


def prioritise(primary, secondary, area_elites, learnt, here_map=None, vanquished=()):
    """Every elite here that this character can capture right now and has not learnt, best first.
    Ranked by: how hard it is to get elsewhere, then whether the boss is certain to be there,
    then primary profession before secondary."""
    mine = [p for p in (primary, secondary) if p]
    wanted = [e for e in area_elites if e.profession in mine and e.skill_id not in learnt]
    for e in wanted:
        e.scarcity = rarity(e.skill_id, here_map, vanquished)
    return sorted(wanted, key=lambda e: (-e.score(), 0 if e.profession == primary else 1, e.name))


def should_capture(elite, ordered, captured, signets):
    """With `signets` left: take this elite only if there are not already enough better, certain
    targets still to come to use them all."""
    if elite not in ordered or elite.skill_id in captured:
        return False
    better = [e for e in ordered[:ordered.index(elite)] if e.skill_id not in captured and e.guaranteed]
    return len(better) < signets


def best_secondary(primary, current, area_elites, learnt, unlocked, signets=2, here_map=None, vanquished=()):
    """The secondary profession that lets the most valuable elites be captured here with the
    signets carried. Returns (profession, [elites it would take]). Keeps the current secondary
    unless another is strictly better, since changing it empties those skills off the bar."""
    best_prof, best_value, best_take = current, -1.0, []
    options = [current] + sorted(p for p in unlocked if p and p != primary and p != current)
    for prof in options:
        ordered = prioritise(primary, prof, area_elites, learnt, here_map, vanquished)
        take = [e for e in ordered if should_capture(e, ordered, set(), signets)][:signets]
        value = sum(e.score() for e in take)
        if value > best_value + 1e-9:
            best_prof, best_value, best_take = prof, value, take
    return best_prof, best_take
