"""Validate the launchd plist template renders to a syntactically valid plist.

We use plistlib (stdlib) to parse the rendered output. No actual
launchctl bootstrap happens here — that's an OS-side effect we don't
want from tests.
"""

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "launchd" / "com.zcdeng.vimax-mcp.plist"


def _render(tmp_path: Path) -> Path:
    raw = TEMPLATE.read_text()
    rendered = raw.replace("__USER_HOME__", "/Users/example").replace(
        "__UV_PATH__", "/Users/example/.local/bin/uv"
    )
    out = tmp_path / "agent.plist"
    out.write_text(rendered)
    return out


def test_template_has_no_unresolved_placeholders(tmp_path: Path):
    out = _render(tmp_path)
    content = out.read_text()
    assert "__USER_HOME__" not in content
    assert "__UV_PATH__" not in content


def test_template_parses_as_plist(tmp_path: Path):
    out = _render(tmp_path)
    with out.open("rb") as f:
        data = plistlib.load(f)
    assert data["Label"] == "com.zcdeng.vimax-mcp"
    assert data["ProgramArguments"][0] == "/Users/example/.local/bin/uv"
    assert "--transport" in data["ProgramArguments"]
    # post-U2: composite REST + MCP SSE (was 'sse' alone before).
    assert "both" in data["ProgramArguments"]
    assert "sse" not in data["ProgramArguments"], (
        "legacy --transport sse should be replaced by 'both' (U6)"
    )
    assert "7801" in data["ProgramArguments"]
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"]["Crashed"] is True
    env = data["EnvironmentVariables"]
    assert env["VIMAX_HOME"] == "/Users/example/projects/ViMax"
    # secrets must NOT be hardcoded into the template
    assert "MINIMAX_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env


@pytest.mark.skipif(
    shutil.which("plutil") is None,
    reason="plutil (macOS-only) not available",
)
def test_template_passes_plutil_lint(tmp_path: Path):
    out = _render(tmp_path)
    result = subprocess.run(
        ["plutil", "-lint", str(out)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_install_script_executable():
    script = REPO_ROOT / "scripts" / "install-launchd.sh"
    assert script.is_file()
    # mode bits include user-execute
    assert script.stat().st_mode & 0o100
