# Data builders

These regenerate the tables in `Sources/gwamm/data/`. The tables themselves are in the
repository and the bot needs nothing from this folder to run. Run from the repository root.

| Script | Writes | From |
|---|---|---|
| `build_routes.py` | `routes.json` | Reforged's PyQuishAI map files (`Sources/aC_Scripts/PyQuishAI_maps`) |
| `build_world.py` | `world.json` | `data/world_dump_v2.json` (made in game: Developer tab > Build world data) and `travel.json` |
| `build_elites.py <geometry folder>` | `elites.json` | `data/elites_*.json` (wiki extracts), Reforged's skill descriptions, and an exported geometry folder (Developer tab > Export ground data) |

`data/pyquish_travel_raw.json` is the raw travel extract `travel.json` was first made from.

## Learning from fight recordings

- `fight_report.py` – prints what the bot expected against what came, deaths, and how far
  away each enemy type noticed the party, from `gwamm_logs/fights/*.jsonl`.
- `build_enemy_roles.py` – rebuilds `Sources/gwamm/data/enemy_roles.json` (which enemy types
  are healers, casters, fighters or minions) from the same recordings. The bot's target
  calling reads that file. Run it after recording new areas, then commit the updated file.
