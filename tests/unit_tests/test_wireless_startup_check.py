"""Tests for wireless startup checks."""

import subprocess

from reachy_mini.utils.wireless_version import startup_check


def _fake_venv(tmp_path, version: str = "1.2.3"):
    python_path = tmp_path / "bin" / "python"
    dist_info = (
        tmp_path
        / "lib"
        / "python3.12"
        / "site-packages"
        / f"reachy_mini-{version}.dist-info"
    )
    python_path.parent.mkdir(parents=True)
    python_path.write_text("")
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(f"Name: reachy-mini\nVersion: {version}\n")
    return python_path, dist_info


def test_read_reachy_mini_install_info_from_site_packages_git(tmp_path):  # noqa: D103
    python_path, dist_info = _fake_venv(tmp_path)
    (dist_info / "direct_url.json").write_text(
        '{"vcs_info": {"requested_revision": "abc123"}}'
    )

    assert startup_check._read_reachy_mini_install_info_from_site_packages(
        python_path
    ) == {
        "source": "git",
        "version": "1.2.3",
        "git_ref": "abc123",
    }


def test_check_and_fix_restore_venv_skips_non_editable(  # noqa: D103
    tmp_path, monkeypatch, capsys
):
    python_path, _ = _fake_venv(tmp_path)

    def fail_run(*args, **kwargs):
        raise AssertionError("pip should not run for non-editable restore venv")

    monkeypatch.setattr(subprocess, "run", fail_run)

    startup_check.check_and_fix_restore_venv(python_path)

    assert "Restore venv install is correct" in capsys.readouterr().out


def test_check_and_fix_restore_venv_repairs_editable(  # noqa: D103
    tmp_path, monkeypatch, capsys
):
    python_path, dist_info = _fake_venv(tmp_path)
    (dist_info / "direct_url.json").write_text('{"dir_info": {"editable": true}}')
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    startup_check.check_and_fix_restore_venv(python_path)

    assert calls
    assert "Successfully reinstalled reachy-mini" in capsys.readouterr().out
