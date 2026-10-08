"""Work out what each enemy type does from the fight recordings, and write
Sources/gwamm/data/enemy_roles.json for the bot's target calling.

    python gwamm_tools/build_enemy_roles.py                 (reads gwamm_logs/fights)
    python gwamm_tools/build_enemy_roles.py file.jsonl ...

The game does not tell us an ordinary enemy's profession, so the bot could not tell a healer
from a fighter. The recordings do: every skill an enemy starts casting is logged. A type that
keeps casting skills that heal or protect ITS ALLIES is a healer; a low-level type that never
casts is a minion or spirit. Skill meanings come from Reforged's skill_descriptions.json.

Run it again whenever there are new recordings: the table only grows more complete.
"""
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fight_report  # noqa: E402

MIN_SEEN = 8            # appearances before a type is judged
HEALER_RATE = 0.6       # ally-healing casts per appearance


def skill_table():
    path = os.path.join(HERE, "Py4GWCoreLib", "skill_descriptions.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


sys.path.insert(0, HERE)
from Sources.gwamm.core.tactics import heals_allies  # noqa: E402  (one definition, shared with the bot)


def build(paths):
    skills = skill_table()
    stats = {}
    for path in paths:
        for fight in fight_report.load(path):
            who, last, seen = fight["who"], {}, set()
            for s in fight["samples"]:
                for e in s["enemies"]:
                    info = who.get(str(e["id"])) or {}
                    model = info.get("model")
                    if not model:
                        continue
                    st = stats.setdefault(model, {"seen": 0, "casts": {}, "level": info.get("level") or 0})
                    if e["id"] not in seen:
                        seen.add(e["id"])
                        st["seen"] += 1
                    c = e.get("cast")
                    if c and last.get(e["id"]) != c:
                        st["casts"][c] = st["casts"].get(c, 0) + 1
                    last[e["id"]] = c
    out = {}
    for model, st in stats.items():
        if st["seen"] < MIN_SEEN:
            continue
        heal = sum(n for c, n in st["casts"].items()
                   if heals_allies((skills.get(str(c)) or {}).get("desc_full", "")))
        total = sum(st["casts"].values())
        if heal / st["seen"] >= HEALER_RATE:
            role = "healer"
        elif total == 0 and st["level"] and st["level"] < 23:
            role = "minion"            # summoned things and spirits: low level, never cast
        elif total / st["seen"] >= 1.0:
            role = "caster"
        else:
            role = "fighter"
        top = sorted(st["casts"].items(), key=lambda kv: -kv[1])[:5]
        out[str(model)] = {"role": role, "seen": st["seen"], "level": st["level"],
                           "skills": [(skills.get(str(c)) or {}).get("name", str(c)) for c, _n in top]}
    return out


def main(paths):
    if not paths:
        paths = sorted(glob.glob(os.path.join(HERE, "gwamm_logs", "fights", "*.jsonl")))
    table = build(paths)
    dest = os.path.join(HERE, "Sources", "gwamm", "data", "enemy_roles.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(table, f, indent=0, sort_keys=True)
    by = {}
    for v in table.values():
        by[v["role"]] = by.get(v["role"], 0) + 1
    print(f"{len(table)} enemy types written to {dest}: {by}")
    for m, v in sorted(table.items(), key=lambda kv: (kv[1]["role"], -kv[1]["seen"])):
        if v["role"] in ("healer", "minion"):
            print(f"  {v['role']:7} model {m} (level {v['level']}, seen {v['seen']}): {', '.join(v['skills'])}")


if __name__ == "__main__":
    main(sys.argv[1:])
