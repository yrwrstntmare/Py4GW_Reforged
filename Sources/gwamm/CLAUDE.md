# GWAMM engine: rules for coding assistants

Start with `STATUS.md` (current version, open issues). Read `ARCHITECTURE.md` only when you
change how modules fit together.

## Binding rules
- No anti-detection, ban-evasion or process-hiding code, ever.
- `core/` imports the standard library only; every game call lives in `runtime/game.py`.
- Never invent a Py4GW/Reforged call: grep its signature in this repo first.
- Check `Widgets/` for a widget/bot that already does it before building (e.g. the Sulfurous
  Wastes farm bot for junundu handling).
- Fix the class of problem, not the one area: drive fixes from data or a general rule so other
  areas are covered too. Name the prompting area only in comments/logs.
- Deterministic only. Never leave an area mid-vanquish. Don't waste consumables.
- Edit only `GWAMM_Vanquish.py`, `Sources/gwamm/`, `gwamm_tests/`, `gwamm_tools/`. Never stage `gwamm_logs/`.
- Log decisions with `log.event(...)`; no personal identifiers (emails, character names) in logs.
- Version 0.x, bumped together in `GWAMM_Vanquish.py` (SCRIPT_VERSION), `Sources/gwamm/__init__.py`
  and the first line of `README.txt`.

## Keep context small (big files: engine.py 83K, node.py 49K, game.py 49K, maprun.py 42K)
- Grep for the symbol, then Read only that line range. Never read a large file whole.
- Run tests with output piped to `tail -3`; check the exit code rather than reading logs.
- Run logs (`gwamm_logs/run_*.jsonl`) are large: filter by `event` with a script, never cat them.
- Reply briefly; don't restate code or diffs in chat.

## Before shipping
`python gwamm_tests/check_names.py`, `test_core.py`, `smoke_campaign.py`, `sim_transit.py`,
`sim_combat.py --assert` (all exit 0). A `core/` change needs a test.
