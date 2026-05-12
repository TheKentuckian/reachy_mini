# Claude Code Instructions

Read `AGENTS.md` in this directory for full instructions on developing Reachy Mini applications.

## Environment

This project uses the **central `/venvs/mini_daemon`** venv. There is no local `.venv` — do **not** run `uv sync` bare in this directory, as uv will create a local `.venv` and waste ~400MB of disk space on a duplicate install.

Set this once in your shell (or add to `~/.bashrc`):

```bash
export UV_PROJECT_ENVIRONMENT=/venvs/mini_daemon
```

With that set, all `uv` commands route to the central venv. To install or update dependencies:

```bash
uv pip install -e .        # Reinstall after editing pyproject.toml
uv lock                    # Update the lock file
```

To run tests:

```bash
/venvs/mini_daemon/bin/pytest tests/unit_tests/ -v
```

## Git / GitHub rules

- All PRs and pushes must target **`TheKentuckian/reachy_mini`** — never `pollen-robotics/reachy_mini`.
- Always pass `--repo TheKentuckian/reachy_mini` to every `gh` command.
- The upstream remote has been intentionally removed. Do not re-add it or reference it.
