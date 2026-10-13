# Status (keep under 40 lines; replace, don't append history)

Version 0.50.75 (2026-10-13).

## Seen working in game
- 444 burrows/riding/pack/capture; 0.50.70: dead_unflag, regroup, Cursed Lands route fix, 4 areas done.

## Shipped, not yet seen working in game
- 0.50.69 "Leave your party?" Yes pressed (unseen). 0.50.66/68 carto: `carto_missing`, `_carto_edge_spot`.
- 0.50.50 `step_loop`, `connection_lost`, `auto_reconnect` (unseen); 0.50.51 `leave_to_travel`; `_direct_start`; 0.50.53 `on_the_way`; 0.50.54 death = no health 2 s;
  wipe zones 300 s; 0.50.55 `keep_off_doors`; 30 s energy wait after a raise.
- 0.50.56-65 Teleporters (Prophet's Path, teleporters.json): `_tp_job` pad (no object) + switches 751-754,
  24 orders, KNOWN_CUT, `rank_teleporters`, `_tp_walk` onto the raised pad (WORKS). Not yet seen to jump.
- 0.50.70: `unflag_heroes` all ids; `dead_unflag`; `regroup`; hazard needs no foe 60 s; /stuck fix;
  zone-outs: short steps near doors, ends off doors, door learnt ("out"), vanquish counted; world gates.
  Wrong door in an area -> route switch. `leave_party_prompt` logs "window up, no button".
  0.50.71: never vanquish in normal mode. 0.50.72: HM the whole way when the last step in is a door from another area (`_enters_from_area`);
  walk_in to a shared-file outpost aims at its gate's doorway (Seekers Passage unlock WORKED 15:11).
  0.50.73: next leg waits for the map (a door leg built during the load became a bare CrossNode: stood
  25 min at the Black Curtain arrival, no log); build errors logged + step given up after 60 s;
  town-return count per queue turn (Talmark dropped at once); stale transit cleared on stop.
  0.50.74: a recorded "gate" walk of 3000+ inside an area is crossed with the engine (`gate_walk` log).
  0.50.75: Kessex Peak walked E/W/N/E 1 min for one cluster (cause NOT found: one level there).
  step_start now logs `aim` + `way` length; `_goal_node` keeps the goal on the target's level.
  NOTE: smoke_campaign stops at the first travel - it does not test routes.
## Sulfurous Wastes pillar (NOT yet run in game)
- Route 444 (`gwamm_tools/patch_routes.py`): riding to the spot under the pillar, Smash (4 adrenaline).
- 0.50.46: `weapon_swap` bow/spear at the burrow before the pillar (restored on foot; needs one in
  the bags); HeroAI combat off for the leader through the whole pillar step (`leader_combat`); charge
  only by attacking foes in reach FROM the spot (1150 ranged / 260 melee), never walking off it.
  Combat that goes nowhere: `combat_stuck` -> 1500 step clear, HeroAI off, no wall (it walled the spot).
  Not persisted across a reload: the swap and the HeroAI switch (stop/start restores HeroAI).
- 0.50.47 resume keeps `actions_done`; 0.50.48 pillar spot CONFIRMED (-11528,10360) in routes.json.
## Where things are
- Device: `C:\Users\tryst\Py4GW_Reforged` (Windows). Logs in `gwamm_logs/`; the current
  log's name is in `gwamm_logs/latest_run.txt` (do not list the folder: it is huge).
- Cloud-session delivery: after `device_commit_files`, re-stage and `cmp` every file.
- Known crash: Py4GW async path-planner write-after-free (Py4GW.dll+0x17d2a2); abandoned BT
  trees are retired (kept alive) rather than dropped. Don't remove that.
