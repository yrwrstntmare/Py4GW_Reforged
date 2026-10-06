# Working on the GWAMM engine

Read `ARCHITECTURE.md` in this folder first; its "Standing rules" are binding.

- `core/` must not import Py4GW. Game calls go in `runtime/game.py` only.
- Verify every Py4GW/Reforged call against the library source in this repository before using it.
- Run `python gwamm_tests/test_core.py`, `python gwamm_tests/smoke_campaign.py` and `python gwamm_tests/check_names.py` before committing.
- Bump the version in both `GWAMM_Vanquish.py` and `Sources/gwamm/__init__.py` together.
- Never stage `gwamm_logs/`. Never edit files outside `GWAMM_Vanquish.py`, `Sources/gwamm/`, `gwamm_tests/`, `gwamm_tools/`.
- No anti-detection, ban-evasion or process-hiding code, ever.
