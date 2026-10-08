GWAMM Vanquish 0.50.4

INSTALL
  1. Copy the folder  Sources/gwamm  into your Py4GW Reforged  Sources  folder.
  2. Put GWAMM_Vanquish.py anywhere you load scripts from and load it.

USE
  1. Load your team and bar by hand, enter the explorable area in hard mode.
  2. Two windows appear. Until you press Start, nothing moves: it only shows
     the plan (regions, objective, path ahead, enemies, fogged map cells).
  3. Press Start in the bot window to let it walk and fight.

NOT DONE YET (by hand for now)
  travel to the area, loading builds, changing secondary, using Signet of Capture.

OFFLINE TESTS (no game needed)
  python tests/test_core.py

PLAYING WITH OTHER ACCOUNTS (multibox, with or without heroes as well)
Nothing to set: the bot looks at the party. If other players are in it, it leaves the party
as you formed it: nobody added or dismissed. Your own heroes get the bars saved under Builds;
the other accounts keep theirs, and their HeroAI is switched on (follow + fight). The options
tab lists each account it can see. It skips areas whose outpost allows fewer
members than you have, and leaves each area by having everyone resign. Form the party
yourself, with HeroAI on the other accounts following the leader; run this script on the
leader only. Every account needs the outposts on the route unlocked. Options > Party type
overrides the detection. Not yet tested with real multibox parties: please report.

CONSUMABLES
Options > "Use consumables", then switch on the ones you want. Nothing is used unless you
chose it, and only from your bags. Each timed bonus has its own setting: off, when the area
is going badly (after N deaths), or always. Death-penalty items are used when the penalty
passes the level you set: whole-party items when several members are that far down, personal
ones for you. The list shows what each item does and how many you carry.

RUN LOG
  Every run writes  gwamm_logs/run_<map id>_<date>_<time>.jsonl  in your Py4GW folder.
  One line per event: start, objective changes, each walking step, stalls, exit-guard
  trips, search tightening, a heartbeat every 5 seconds, and the result.
  "Dump debug file" in the bot window writes a full snapshot (gwamm_debug_<time>.json).

VERSIONS
  0.8.8  steadier fights (a group keeps one objective as it shrinks); walked-distance figure fixed
  0.8.7  only exits reachable from where you stand are kept; overlapping ones merged
  0.8.6  remembers where you arrive in each map and avoids that portal from then on
  0.8.5  Start, Stop and status are also in the map window
  0.8.4  errors while drawing no longer hide the windows; tracebacks go to the run log
  0.8.3  also avoids exits found from arrival points (the Camp Rankor portal was being missed)
  0.8.2  picks up an updated gwamm folder without restarting the game
  0.8.1  clear message when the script and the gwamm folder do not match
  0.8.0  run log
  0.7    stall recovery after 45 seconds
  0.6    debug dump button
  0.5    enemy groups judged by walking distance
  0.4    pull control: stop short of groups, smaller fight radius
  0.3    area exits avoided
  0.2    sight-based search radius
  0.1    first build
