"""Summarise the fight recordings (gwamm_logs/fights/*.jsonl): what the bot expected, what came,
how it went, and from how far each enemy type started running at the party.

    python gwamm_tools/fight_report.py                      (reads gwamm_logs/fights)
    python gwamm_tools/fight_report.py path/to/file.jsonl ...

Plain Python, no game needed. Nothing here changes the bot: it prints numbers to tune it by.
"""
import glob
import json
import math
import os
import statistics
import sys


def load(path):
    fights, cur = [], None
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r["rec"] == "fight":
            cur = {"head": r, "samples": [], "who": dict(r["who"]), "end": None, "file": os.path.basename(path)}
            fights.append(cur)
        elif cur is None:
            continue
        elif r["rec"] == "s":
            cur["samples"].append(r)
        elif r["rec"] == "who":
            cur["who"].update(r["who"])
        elif r["rec"] == "end":
            cur["end"] = r
    return fights


def wake_distances(fight, run_speed=250.0):
    """For each enemy: how far the nearest of ours was the second before it broke into a run."""
    ours_kind = {k for k, v in fight["who"].items() if v["side"] == "ally" and v.get("kind") in ("party", "leader")}
    prev, out = {}, {}
    for s in fight["samples"]:
        ours = [a["xy"] for a in s["allies"] if str(a["id"]) in ours_kind and not a.get("dead")]
        for e in s["enemies"]:
            i = e["id"]
            if i not in out and i in prev and ours and math.hypot(*e.get("vel", (0, 0))) > run_speed:
                out[i] = min(math.dist(prev[i], o) for o in ours)
            prev[i] = e["xy"]
    return out


def main(paths):
    if not paths:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = sorted(glob.glob(os.path.join(here, "gwamm_logs", "fights", "*.jsonl")))
    fights = [f for p in paths for f in load(p)]
    if not fights:
        print("no fight recordings found")
        return
    by_model, rows = {}, []
    for f in fights:
        head, end = f["head"], f["end"] or {}
        plan = head.get("ctx", {}).get("plan") or {}
        fresh = bool(plan.get("t")) and head["contact_t"] - plan["t"] < 30.0
        wakes = wake_distances(f)
        for i, d in wakes.items():
            model = f["who"].get(str(i), {}).get("model")
            by_model.setdefault(model, []).append(d)
        rows.append((f["file"], head["n"], plan.get("verdict") if fresh else "-", plan.get("total") if fresh else "-",
                     end.get("enemies_met"), end.get("most_at_once"), end.get("seconds"), end.get("kills"),
                     end.get("ally_deaths"), end.get("leader_deaths"), end.get("why") or "(cut off)"))
    print(f"{len(fights)} fights\n")
    print("file                        #  plan   expected  met  most  secs  kills  party deaths  leader deaths  ended")
    for r in rows:
        print("{:<26} {:>3}  {:<6} {:>8} {:>4} {:>5} {:>5} {:>6} {:>13} {:>14}  {}".format(*[("-" if v is None else v) for v in r]))
    done = [r for r in rows if isinstance(r[3], int) and isinstance(r[4], int)]
    if done:
        over = sum(1 for r in done if r[4] > r[3])
        print(f"\nplanned fights: {len(done)}; more came than expected in {over} "
              f"(average expected {statistics.mean(r[3] for r in done):.1f}, met {statistics.mean(r[4] for r in done):.1f})")
    print("\nhow far away the party was when each enemy type started running (model id: count, nearest, middle, furthest)")
    for model, ds in sorted(by_model.items(), key=lambda kv: -len(kv[1])):
        ds.sort()
        print(f"  {model}: {len(ds)}, {ds[0]:.0f}, {ds[len(ds) // 2]:.0f}, {ds[-1]:.0f}")
    print("\n(the nearest values are the ones the party itself woke; far ones were called in by others or were already on a run)")


if __name__ == "__main__":
    main(sys.argv[1:])
