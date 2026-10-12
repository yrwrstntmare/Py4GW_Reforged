"""Builds Sources/gwamm/data/elites.json from the wiki extracts in gwamm_tools/data/ (lists of elite
skills by capture location, per campaign, plus the per-profession lists as a second source),
Reforged's elite skill table (names -> skill ids, professions) and the exported map names."""
import json, re, glob, gzip, os, sys
from collections import defaultdict
PROF = {"warrior":1,"ranger":2,"monk":3,"necromancer":4,"mesmer":5,"elementalist":6,"assassin":7,"ritualist":8,"paragon":9,"dervish":10}
norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower().replace("(explorable area)", "").replace("(explorable)", ""))

# skill table: names -> ids from Reforged's skill_descriptions.json
skills = {}
for sid, d in json.load(open("Py4GWCoreLib/skill_descriptions.json")).items():
    n = d.get("name") or ""
    if n and "(PvP)" not in n:
        skills.setdefault(norm(n), (int(sid), n))

# map names
names, needs_suffix = {}, set()
for f in glob.glob(sys.argv[1] + "/*.json.gz"):
    d = json.load(gzip.open(f, "rt"))
    if d.get("name"):
        names.setdefault(norm(d["name"]), int(d["map_id"]))
        if "(explorable" in d["name"].lower(): needs_suffix.add(norm(d["name"]))
for mid, t in json.load(open("Sources/gwamm/data/travel.json")).items():
    if isinstance(t, dict) and t.get("name"): names.setdefault(norm(t["name"]), int(mid))
w = json.load(open("Sources/gwamm/data/world.json"))
for mid in w["explorable"]:
    n = w["names"].get(str(mid))
    if n:
        names.setdefault(norm(n), int(mid))
        if "(explorable" in n.lower(): needs_suffix.add(norm(n))
names.setdefault(norm("Spearhead Peak"), 93)

rows = []
for camp in ("prophecies", "factions", "nightfall", "eotn"):
    for r in json.load(open(f"gwamm_tools/data/elites_{camp}.json")):
        rows.append((r["skill"], r["boss"], r["location"], r.get("note", ""), r.get("guaranteed", True), "location list", r.get("profession", "")))
for part in "AB":
    for r in json.load(open(f"gwamm_tools/data/elites_by_prof_{part}.json")):
        if not r["skill"] or not r["location"]: continue
        for loc in re.split(r"\s+(?:and|&)\s+", r["location"]):
            rows.append((r["skill"], r["boss"], loc, r.get("note", ""), True, "profession list", r.get("profession", "")))

BEYOND = ("winds of change", "war in kryta", "hearts of the north", "during", "quest", "proof of triumph", "only spawns", "after ", "before ")
def when_of(loc, note):
    """Short words for when a quest-time boss is there: "during A Show of Force", "Winds of Change"."""
    m = re.search(r"\((winds of change|war in kryta|hearts of the north)\)", loc, flags=re.I)
    note = re.sub(r"^only\s+", "", note.strip(), flags=re.I)
    note = re.sub(r"^during (the )?quest:?\s+", "during ", note, flags=re.I)
    return note if len(note) > len("during quest") else (m.group(1).title().replace(" Of ", " of ").replace(" In ", " in ") if m else (note or "during a quest"))

# Bosses that are only there during a quest or Beyond content are kept, marked as not certain
# ("guaranteed": false) with "when": the bot tries them if they are there and never counts on
# them. Dependable rows go first so they win where a boss is listed both ways.
is_beyond = lambda r: any(k in (r[2] + " " + r[3]).lower() for k in BEYOND)
rows.sort(key=is_beyond)
areas, seen, unknown_skill, unknown_loc, skipped = defaultdict(list), {}, set(), set(), 0
for skill, boss, loc, note, guaranteed, source, prof in rows:
    beyond = is_beyond((skill, boss, loc, note))
    if beyond:
        skipped += 1
        guaranteed = False
    sk = skills.get(norm(skill.strip('"')))
    pid = PROF.get(prof.strip().lower(), 0)
    if sk is None or not pid: unknown_skill.add((skill, prof)); continue
    sk = (sk[0], sk[1], pid)
    mid = names.get(norm(re.sub(r"\(.*?\)", "", loc)) ) if "(explorable" not in loc.lower() else names.get(norm(loc))
    if mid is not None and "(explorable" not in loc.lower() and (norm(loc) in needs_suffix or (
            beyond and norm(re.sub(r"\(.*?\)", "", loc)) in needs_suffix)):
        mid = None                                   # the mission of the same name, not the area
    if mid is None: unknown_loc.add(loc); continue
    boss = re.sub(r"\s*\((boss|warrior|ranger|monk|necromancer|mesmer|elementalist|assassin|ritualist|paragon|dervish|golem|djinn|insect)\)", "", boss, flags=re.I).strip()
    key = (mid, sk[0], norm(boss))
    if key in seen:
        old = seen[key]
        if beyond and "when" in old and len(when_of(loc, note)) > len(old["when"]):
            old["when"] = when_of(loc, note)         # the fuller of two notes on the same boss
        continue
    row = {"id": sk[0], "name": sk[1], "prof": sk[2], "boss": boss, "guaranteed": bool(guaranteed), "source": source}
    if beyond:
        row["when"] = when_of(loc, note)
    seen[key] = row
    areas[mid].append(row)

# where each elite can be had at all (vanquish areas only) -> how rare it is
where = defaultdict(set)
for mid, lst in areas.items():
    for e in lst:
        if e["guaranteed"]: where[e["id"]].add(mid)
for lst in areas.values():
    for e in lst: e["areas"] = len(where[e["id"]])
out = {str(k): sorted(v, key=lambda e: (e["prof"], e["name"])) for k, v in sorted(areas.items())}
# Vanquish areas with no row at all: the wiki lists cover every area, so these have no elite
# boss. Written as empty lists, so "none here" can be told from "no data".
for mid in w["explorable"]:
    out.setdefault(str(int(mid)), [])
out = {k: out[k] for k in sorted(out, key=int)}
json.dump(out, open("Sources/gwamm/data/elites.json", "w"), indent=0)
print("skills known", len(skills), "| areas with elites", len(out), "| boss rows", sum(map(len, out.values())), "| quest/Beyond rows (kept as not certain)", skipped,
      "| areas with none", sum(1 for v in out.values() if not v))
print("elites covered", len(where), "of", len(skills))
print("unknown skills:", sorted(unknown_skill)[:40])
print("locations not on the vanquish list (missions, dungeons, etc):", len(unknown_loc), sorted(unknown_loc))
