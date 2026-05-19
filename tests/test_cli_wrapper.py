"""Tests for the shell wrapper + install-cli.sh.

Uses VIMAX_BIN_DIR to redirect the install target into tmp_path, so the
tests never touch the developer's real ~/.local/bin/vimax.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "vimax"
INSTALL = REPO_ROOT / "scripts" / "install-cli.sh"


def _run(args: list[str], tmp_bin: Path, extra_env: dict | None = None):
    env = {
        **os.environ,
        "VIMAX_BIN_DIR": str(tmp_bin),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(INSTALL), *args],
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Static file checks
# ---------------------------------------------------------------------------

def test_wrapper_is_executable():
    assert WRAPPER.is_file()
    assert WRAPPER.stat().st_mode & stat.S_IXUSR


def test_install_script_is_executable():
    assert INSTALL.is_file()
    assert INSTALL.stat().st_mode & stat.S_IXUSR


def test_wrapper_exec_target_is_uv_run():
    # The wrapper should hand off to `uv run --directory <repo>`. If anyone
    # rewrites it to install the package globally or invoke python directly,
    # the contract breaks.
    text = WRAPPER.read_text()
    assert "uv run --directory" in text
    assert "python -m vimax_mcp.cli" in text


# ---------------------------------------------------------------------------
# install / status / remove flow
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_bin(tmp_path: Path) -> Path:
    d = tmp_path / "bin"
    d.mkdir()
    return d


def test_install_creates_symlink(tmp_bin: Path):
    r = _run(["install"], tmp_bin)
    assert r.returncode == 0, r.stdout + r.stderr
    link = tmp_bin / "vimax"
    assert link.is_symlink()
    target = os.readlink(link)
    assert target == str(WRAPPER)


def test_install_is_idempotent(tmp_bin: Path):
    r1 = _run(["install"], tmp_bin)
    assert r1.returncode == 0
    r2 = _run(["install"], tmp_bin)
    assert r2.returncode == 0
    assert "already installed" in r2.stdout


def test_install_refuses_foreign_existing_file(tmp_bin: Path):
    foreign = tmp_bin / "vimax"
    foreign.write_text("#!/usr/bin/env bash\necho not mine\n")
    foreign.chmod(0o755)
    r = _run(["install"], tmp_bin)
    assert r.returncode != 0
    assert "refusing to overwrite" in r.stderr
    # foreign file is untouched
    assert foreign.read_text().startswith("#!/usr/bin/env bash")


def test_install_refuses_foreign_symlink(tmp_bin: Path, tmp_path: Path):
    other = tmp_path / "other_vimax"
    other.write_text("#!/usr/bin/env bash\necho other\n")
    other.chmod(0o755)
    foreign_link = tmp_bin / "vimax"
    foreign_link.symlink_to(other)
    r = _run(["install"], tmp_bin)
    assert r.returncode != 0
    assert "refusing to overwrite" in r.stderr


def test_remove_deletes_our_symlink(tmp_bin: Path):
    _run(["install"], tmp_bin)
    r = _run(["remove"], tmp_bin)
    assert r.returncode == 0
    assert not (tmp_bin / "vimax").exists()


def test_remove_no_op_when_nothing_installed(tmp_bin: Path):
    r = _run(["remove"], tmp_bin)
    assert r.returncode == 0
    assert "nothing to remove" in r.stdout


def test_remove_will_not_delete_foreign(tmp_bin: Path, tmp_path: Path):
    other = tmp_path / "other_vimax"
    other.write_text("x")
    foreign_link = tmp_bin / "vimax"
    foreign_link.symlink_to(other)
    r = _run(["remove"], tmp_bin)
    assert r.returncode != 0
    assert foreign_link.is_symlink()  # still there


def test_status_reports_uninstalled(tmp_bin: Path):
    r = _run(["status"], tmp_bin)
    assert r.returncode == 0
    assert "not installed" in r.stdout


def test_status_reports_ours_after_install(tmp_bin: Path):
    _run(["install"], tmp_bin)
    r = _run(["status"], tmp_bin)
    assert r.returncode == 0
    assert "ownership:  ours" in r.stdout


def _path_with_uv(extras: str) -> str:
    """Build a PATH that has the requested entries plus the dir that hosts uv.

    Without this the install script's ensure_prereqs trips before we get
    to test PATH-warning behavior.
    """
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not installed")
    return f"{extras}:{os.path.dirname(uv)}:/usr/bin:/bin"


def test_path_warn_when_bin_dir_missing_from_path(tmp_bin: Path):
    r = _run(["install"], tmp_bin, extra_env={"PATH": _path_with_uv("/usr/local/bin")})
    assert r.returncode == 0
    assert "is not on PATH" in r.stdout


def test_no_path_warn_when_bin_dir_on_path(tmp_bin: Path):
    r = _run(
        ["install"],
        tmp_bin,
        extra_env={"PATH": _path_with_uv(str(tmp_bin))},
    )
    assert r.returncode == 0
    assert "is not on PATH" not in r.stdout


# ---------------------------------------------------------------------------
# Wrapper invocation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_wrapper_runs_cli_help(tmp_bin: Path):
    # Install, then invoke through the symlink to prove BASH_SOURCE
    # resolution still finds the repo.
    _run(["install"], tmp_bin)
    link = tmp_bin / "vimax"
    proc = subprocess.run(
        [str(link), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "submit-idea" in proc.stdout
    assert "vimax-mcp daemon" in proc.stdout
