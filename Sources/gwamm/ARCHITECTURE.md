# GWAMM engine: how it is built, and how to build on it

Read this before changing or reusing anything here. It is written for people and for coding
assistants alike.

Automating Guild Wars breaks ArenaNet's rules and can get an account suspended. This project
is about navigation and automation architecture. It does not, and must not, contain
anti-detection, ban evasion or process hiding.

## The one rule that makes everything else work

**Perception, planning and execution are separate.**

| Layer | Folder | May import | Job |
|---|---|---|---|
| Planning | `core/` | Python standard library only | Decide what to do from plain data |
| Game access | `runtime/game.py` | Py4GW | Every read of game state, every command sent |
| Execution | `runtime/` (the rest) | `core`, `game.py`, Reforged trees | Turn decisions into behaviour-tree steps |
| Window | `GWAMM_Vanquish.py` | all of the above | Draw, start, stop |

`core/` never imports Py4GW. That is why it can be tested and simulated on any machine with
no game running. If a planning change needs something from the game, add a function to
`game.py`, have the runtime read it, and pass plain values (tuples, numbers) into `core`.

## What is in `core/` (reusable by any bot)

- `geometry.py` – `NavGraph(traps, spacing, links)`: walkable-ground graph built from the
  map's pathing trapezoids. `path(start, goal, penalty)` is A* with an optional per-node cost
  factor. Exit fences ("no-go") live here.
- `regions.py` – `RegionMap`: the graph cut into regions, distances between them, and a tour.
- `memory.py` – `InstanceMemory`: what has been seen this visit (ground covered, enemies,
  groups, objectives that failed).
- `engine.py` – `Config` (every tunable, with a comment) and `Engine`, the planner:
  - `Engine(traps, start_xy, config, projection, exits, goto, links, guide, hints)`
  - `update(player_xy, enemies, foes_remaining, now, carto_grid)` – feed it what the game shows
  - `next_step()` – returns `(x, y, clear_radius)` to walk to, or `None` when finished
  - `step_failed()`, `record_death(xy, hazard)`, `add_wall(player_xy, target)` – feedback
  - `status()` – a dictionary for display and logs
  - `goto=(x, y)` turns the same planner into "get to this point" (used for crossing areas)
- `cartography.py` – which map cells are still fogged and where to stand to clear them.
- `tactics.py` – target choice, crowd size, where to fall back to.
- `consumables.py` – the catalogue of consumables (what each is for, who it helps) and
  `decide(cfg, facts)`: the one item to use now, or none. Nothing is used unless the player
  switched that item on. Ids and rule kinds come from the Reforged library's own table.
- `elites.py`, `builds.py`, `world.py` – elite/boss tables, skill templates and team builds,
  the world graph (which area connects to which, which outpost serves it).

## What is in `runtime/`

- `game.py` – the only place game functions are called. Small functions, each wrapping calls
  that were checked against the Reforged source.
- `session.py` – `Session`: builds the `Engine` for the current map, holds settings, saves and
  restores a run (`resume.json`), saves options (`options.json`).
- `node.py` – `AdaptiveNode(session, target_map_id, transit, arrive_map)`: a Reforged
  `BehaviorTree.Node` that runs one area: perceive → `engine.next_step()` → walk and fight
  that step with Reforged's `MoveAndKill` → report back. Deaths, resting and stuck handling
  are here; the rest is in three mixins so two people rarely need the same file:
  - `node_fight.py` – target calling, falling back, walking to the dead
  - `node_blessing.py` – shrine blessings and bounties
  - `node_capture.py` – elite capture
- `cross.py` – `CrossNode`: step through a door into the next area.
- `campaign.py` – `Campaign`: the queue, its results, saved teams, the order areas are run in.
- `maprun.py` – `MapRunNode`: one queued area from start to finish (travel, walk in, vanquish,
  leave). Its outpost preparation is a mixin:
  - `maprun_setup.py` – secondary profession, team and hero bars, signets, shared-party checks
- `pcons.py` – gathers the facts for `core/consumables.decide` and uses the item it names. `guard.py` – keeps one bad tick from killing the run.
- `runlog.py` – the JSON-lines run log. **Observability first:** anything the bot decides that
  a person might later ask "why?" about gets a `log.event(...)`.

## Building something new on this

- **Another activity in an area** (a farm, a quest step, "explore only"): make a
  `BehaviorTree.Node` like `AdaptiveNode`, give it the `Session`, and use `session.engine` for
  pathing and enemy memory. Do not copy the pathing; ask the engine for steps.
- **Getting somewhere** (between areas, to an NPC): `AdaptiveNode` with `transit={"xy": ...}`
  already walks to a point while avoiding and fighting as needed.
- **A new decision rule** (who to target, when to retreat): put the rule in `core/` as a pure
  function with a test, call it from `node.py`.
- **Something the game must tell us:** one new function in `game.py`.
- **A new setting:** add it to `Config` in `core/engine.py` with a comment, then a control in
  `draw_options`. It is saved automatically.

## Standing rules

1. **Never invent a Py4GW method.** Open the Reforged source, find the function, read its
   signature, then call it. Say in the commit or a comment when something is assumed and not
   yet seen working in game.
2. **Deterministic logic only.** No randomness in decisions, no ML, no LLM calls at run time.
3. **Rules come from game data, not from one person's build.** This is meant to work for any
   profession and party. Read skill data; do not hard-code a bar.
4. **Do not edit Reforged's own files.** Everything lives in `GWAMM_Vanquish.py`,
   `Sources/gwamm/`, `gwamm_tests/` and `gwamm_tools/`, so upstream updates merge cleanly.
5. **Version** is in two places that must match: `SCRIPT_VERSION` in `GWAMM_Vanquish.py` and
   `__version__` in `Sources/gwamm/__init__.py`. Numbering stays 0.x.
6. **Fix, do not remove,** a feature someone wants to test. Hide unfinished ones from the
   window instead of leaving confusing switches.
7. `gwamm_logs/` is personal run data and is ignored by git. Never commit it.

## Working on it together

Git merges changes to different files, and to different parts of one file, by itself. It only
stops and asks when two people changed the same lines. To keep that rare: work on a branch
per task, pull before you start, keep commits small, and say in the pull request which
module you touched. `Config` in `core/engine.py` and `draw_options` in `GWAMM_Vanquish.py`
are the two places everyone adds a line to; add yours next to the feature it belongs to, not
at the end, and conflicts there stay trivial.

## Tests (no game needed)

From the repository root:

```
python gwamm_tests/test_core.py        # planner: geometry, cartography, elites, full simulated runs
python gwamm_tests/smoke_campaign.py   # the campaign flow against a stubbed game
python gwamm_tests/sim_transit.py      # crossing an area
python gwamm_tests/check_names.py      # every name each file uses is imported there
```

Run them before every push. A change to `core/` that has no test is not finished.

## Data files (`data/`)

`travel.json` and `world.json` (area connections, outposts), `elites.json` (bosses and their
elites), `routes.json` (known walking routes for 130 areas, used as a guide and as the way
round when blocked). They are generated; the scripts and source extracts that build them are
in `gwamm_tools/` (see its README). The bot only needs the tables.

## What is proven and what is not

Proven in game: single account with heroes, vanquish plus cartography plus elite capture,
multi-area queues, resume after reload. Not yet run in game: parties with other accounts,
consumables, exit-game, the edge pass. Keep this paragraph honest when that changes.
