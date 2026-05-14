# Claude Code Instructions

Read `AGENTS.md` in this directory for full instructions on developing Reachy Mini applications.

## Environment

This project uses the **central `/venvs/mini_daemon`** venv. There is no local `.venv`.

**Do not use `uv` on this device.** The Pi's SD card is small (14 GB) and `uv`'s on-disk cache balloons to multiple gigabytes — it has filled the disk before. Use the venv's pip directly instead:

```bash
/venvs/mini_daemon/bin/pip install -e .   # Reinstall after editing pyproject.toml
/venvs/mini_daemon/bin/pytest tests/unit_tests/ -v
/venvs/mini_daemon/bin/python -m ...      # Anything else you'd `uv run`
```

Lock-file maintenance (`uv lock`) — if needed — should be done off-device on a workstation, then `uv.lock` committed. Do not run `uv lock` here.

## Git / GitHub rules

- All PRs and pushes must target **`TheKentuckian/reachy_mini`** — never `pollen-robotics/reachy_mini`.
- Always pass `--repo TheKentuckian/reachy_mini` to every `gh` command.
- The upstream remote has been intentionally removed. Do not re-add it or reference it.
