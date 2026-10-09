# Working on the GWAMM engine

Read `ARCHITECTURE.md` in this folder first; its "Standing rules" are binding.

- `core/` must not import Py4GW. Game calls go in `runtime/game.py` only.
- Verify every Py4GW/Reforged call against the library source in this repository before using it.
- Run `python gwamm_tests/test_core.py`, `python gwamm_tests/smoke_campaign.py` and `python gwamm_tests/check_names.py` before committing.
- Bump the version in both `GWAMM_Vanquish.py` and `Sources/gwamm/__init__.py` together.
- Never stage `gwamm_logs/`. Never edit files outside `GWAMM_Vanquish.py`, `Sources/gwamm/`, `gwamm_tests/`, `gwamm_tools/`.
- No anti-detection, ban-evasion or process-hiding code, ever.
- Before building a feature, check `Widgets/` for one that already does it or already keeps the data (ARCHITECTURE.md, standing rule 8).
- Fix the class of problem, not the one area: when a fix is prompted by one map, make it work from
  data or a general rule (other Reforged bots' data, route evidence, learned files), so other areas
  with the same problem are covered too. Name the area that prompted it only in comments/logs.
