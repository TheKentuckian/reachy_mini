# Claude Code Instructions

Read `AGENTS.md` in this directory for full instructions on developing Reachy Mini applications.

## This is a fork

`TheKentuckian/reachy_mini` = upstream `pollen-robotics/reachy_mini` **v1.10.0 + a thin
patch stack**. `COMPATIBILITY.md` lists every patch and why it exists; `MAINTENANCE.md`
covers the out-of-repo robot state. Keep the stack thin: prefer one commit per behaviour
change, and re-evaluate each patch against upstream when re-basing.

## Environment

**On the robot** (`ssh ricci`, checkout `/home/pollen/reachy_mini`) the package is
editable-installed into the central `/venvs/mini_daemon` (daemon) and `/venvs/apps_venv`
(apps) venvs. There is no local `.venv` there.

**Do not use `uv` on the robot.** The Pi's SD card is small (14 GB) and `uv`'s on-disk
cache balloons to multiple gigabytes — it has filled the disk before. Use the venv's pip
directly, and clear `~/.cache` before any install:

```bash
/venvs/mini_daemon/bin/pip install -e .   # Reinstall after editing pyproject.toml
/venvs/apps_venv/bin/pip install -e .     # …and the apps venv
/venvs/mini_daemon/bin/pytest tests/unit_tests -q
/venvs/mini_daemon/bin/python -m ...      # Anything else you'd `uv run`
```

Lock-file maintenance (`uv lock`) — if needed — should be done off-device on a
workstation, then `uv.lock` committed. Do not run `uv lock` on the robot.

**On a workstation** the checkout has its own `.venv` (`.venv/bin/pip install -e
'.[central-consumer]'`).

## Running tests on a workstation — keep the robot out of it

`wireless`-marked tests auto-skip (fork `conftest.py`), but the `audio`/`video`
hardware tests and the named-daemon mDNS tests can still reach a robot on the same LAN
or probe local audio hardware. Use:

```bash
.venv/bin/pytest tests/unit_tests -q -m "not audio and not video and not loopback and not respeaker" \
  --ignore tests/unit_tests/test_daemon.py --ignore tests/unit_tests/test_app.py
```

Never run the unfiltered suite on a machine that shares a LAN with a powered robot.

## Git / GitHub rules

- All PRs and pushes must target **`TheKentuckian/reachy_mini`** — never `pollen-robotics/reachy_mini`.
- Always pass `--repo TheKentuckian/reachy_mini` to every `gh` command.
- `upstream-ro` is a **fetch-only** remote of pollen-robotics (push disabled) used for
  `git fetch upstream-ro --tags` when re-basing. Never push to it.
