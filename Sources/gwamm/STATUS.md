# Status (keep under 40 lines; replace, don't append history)

Version 0.50.41 (2026-10-11).

## Seen working in game
- 444: burrow mounts, heroes mount (7/7), riding kept after a revive, in-order route with
  `route_wait`, pack mode, elite capture. 395: gate crossed by `cross_other_side`.

## Shipped, not yet seen working in game
- 0.50.38 The dead are waited for once (75 s); if nobody was raised the run carries on without
  them (`dead_left`). Before: the wait restarted for ever (402: leader + 1 hero, 10 min).
- 0.50.38 Walls: kept-off ground round a pinned spot 400 -> `Config.wall_radius` 250; a wall
  that would cut >3% of the map away is walked with care instead (`soft_walls`, `wall`
  only_way); standing still on purpose (rest, hold) no longer counts as stalled (it marked
  a wall ahead the moment each rest ended: `step_abandoned` seconds 0).
- 0.50.36 Doorways (402 ended "blocked", 88 left): a portal the game shows keeps its doorway
  ring unless that ring cuts the map; `exit_guard` only refuses steps leading further in.
- 0.50.36 Gate walk (395 from Mihanu Township failed 10 times: the recorded path's last leg
  pushes west along the wall, the portal is beside it). `CrossNode` turns to the nearest
  game portal after 15 s and tries its sides (`cross_other_side`). Seen: got through.
- 0.50.37 Doors remember where they took us (`gwamm_logs/gate_spots.json`, `gate_spot`).

- 0.50.40 Start in an outpost with nothing queued: stops (`nothing_here`) instead of mapping the town.
- 0.50.41 Burrows: walk up, flag heroes onto it until all are within 450 (12 s, `gathered`), then use
  it; on-foot heroes flagged to it at 6 s (`stragglers`); at 18 s leave + re-enter once (`remount_retry`).
## Sulfurous Wastes pillar (NOT yet run in game)
- Route 444 (`gwamm_tools/patch_routes.py`): after (-13595,8507), riding, along the sulfur
  strip to its dead end under the bridge (-11600,10330), Junundu Smash (4 adrenaline), back.
- 0.50.39 Adrenaline: junundu skills build none (Siege included); weapon hits and damage do;
  it drains after 25 s out of a fight. HeroAI is kept off Smash from 4 points before
  (`save_from`); if not charged at the spot, plain attacks on the nearest enemy within 3000
  until it is, then back onto the spot and Smash (`route_action` `skill`: back_after, targets).
- Other Desolation areas: no pillars. 439: centre paths shut until Ruins of Morah is done.
## Watch: 402, six heroes in a clump fell in ~20 s to 4 foes with Energy Surge (no flag error)
## Where things are
- Device: `C:\Users\tryst\Py4GW_Reforged` (Windows). Logs in `gwamm_logs/`; the current
  log's name is in `gwamm_logs/latest_run.txt` (do not list the folder: it is huge).
- Cloud-session delivery: after `device_commit_files`, re-stage and `cmp` every file.
- Known crash: Py4GW async path-planner write-after-free (Py4GW.dll+0x17d2a2); abandoned BT
  trees are retired (kept alive) rather than dropped. Don't remove that.
