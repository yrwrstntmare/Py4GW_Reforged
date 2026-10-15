# Status (keep under 40 lines; replace, don't append history)

Version 0.50.90 (2026-10-15).

## Seen working in game
- 444 burrows/riding/pack/capture; dead_unflag, regroup, Druids Overlook exit; Sage Lands complete (0.50.87).
## Shipped, not yet seen working in game
- 0.50.50-78: `step_loop`, teleporters (WORK), `dead_unflag`, door learnt, HM the whole way, `door_line_cut`.
  0.50.79-80: dead kept by body spot; `door_blocks_step`; `_hold_give_up`; own pickup (WORKS).
  0.50.81: `_camp` waits by foes the plan cannot reach; failed area -> `fail_<map>.json`.
  0.50.82: camp keeps off doors; 3rd stall on one "stacked" spot = scenery; no direct walk near a door.
  0.50.84: pull limit + crowd fall-back scaled to party size (`tactics.for_party`; Diessa 5-6 pulls deadly).
  0.50.85-86: stacked stall / step loop -> `cut_climb` (250, `climb_cut` log); loop check by way left;
  game level links saved: gwamm_logs/links/<map>.json.
  0.50.87: progress count restarts when the plan gets 3000+ longer (gave up 30 s after a cut); cuts kept in
  climb_cuts.json, one seen on 2 visits is cut at every build (`cut_joins_at`). Sage Lands bridge east end.
  0.50.88: wipe while crossing an area on a recorded route resumes from that area (gate legs, `phase=resume`),
  not from the outpost; parties of 4 pull groups in the way while crossing. (No pcons in transit: by design.)
  0.50.89: healer/raise judged by a skill's main effect (`main_effect`): Phoenix, Mist Form, Frozen Soil...
  had made 123 types "healers". learned_roles.json now "_v": 2 (old file ignored); a known caster/fighter
  needs 2 different support skills. `gear` log once per session (worn + Equipment Pack, read only).
  0.50.90: `core/gear.py` + `runtime/gear_swap.py` (cfg.gear_swap): main set = held at start (gear_main.json);
  another set only for situational worth (armor vs species / damage kind) +6, base loss <=15; never in a
  fight; back to main when crossing and at area end. UNTESTED in game (EquipItem). Learned arrival points
  and "out" doors only count within 1200-1500 of a listed portal/spawn (a reload and a map travel had
  put two false rings mid-path in Flame Temple Corridor: 22 regions -> 9).
  0.50.83: game portals with no arrival point / learned door / world door within 1500 = `portals_doubted`
  (spawn-size ring, not a door for step checks). 106: 23 portals, file shared; its 10% reach is right.
  NOTE: smoke_campaign stops at the first travel. Griffon's Mouth (27): map file unreadable offline.
## Sulfurous Wastes pillar (NOT yet run in game): route 444 (`gwamm_tools/patch_routes.py`): riding to the spot under the pillar, Smash (4 adrenaline).
- 0.50.46: `weapon_swap` bow/spear at the burrow before the pillar (restored on foot; needs one in
  the bags); HeroAI combat off for the leader through the whole pillar step (`leader_combat`); charge
  only by attacking foes in reach FROM the spot (1150 ranged / 260 melee), never walking off it.
  Combat that goes nowhere: `combat_stuck` -> 1500 step clear, HeroAI off, no wall (it walled the spot).
  Not persisted across a reload: the swap and HeroAI switch. Pillar spot CONFIRMED (-11528,10360).
## Where things are
- Device: `C:\Users\tryst\Py4GW_Reforged` (Windows). Logs in `gwamm_logs/`; the current
  log's name is in `gwamm_logs/latest_run.txt`. Delivery: after `device_commit_files`, re-stage and `cmp`.
- Known crash: Py4GW async path-planner write-after-free (Py4GW.dll+0x17d2a2); abandoned BT
  trees are retired (kept alive) rather than dropped. Don't remove that.
