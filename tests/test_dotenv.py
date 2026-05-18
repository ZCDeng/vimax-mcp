import os
from pathlib import Path

import pytest

from vimax_mcp.dotenv import load_env_file, parse_env_file


def test_parse_basic(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("FOO=bar\nBAZ=qux\n")
    assert parse_env_file(f) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_strips_quotes(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text('A="hello"\nB=\'world\'\n')
    parsed = parse_env_file(f)
    assert parsed == {"A": "hello", "B": "world"}


def test_parse_keeps_hash_inside_quotes(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text('KEY="abc#def"\n')
    assert parse_env_file(f) == {"KEY": "abc#def"}


def test_parse_strips_inline_comment(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("X=raw   # a trailing note\n")
    assert parse_env_file(f) == {"X": "raw"}


def test_parse_skips_blank_and_comment(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("\n# just a comment\nA=1\n\n")
    assert parse_env_file(f) == {"A": "1"}


def test_parse_missing_file(tmp_path: Path):
    assert parse_env_file(tmp_path / "nope") == {}


def test_load_does_not_clobber_existing_env(tmp_path: Path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("VIMAX_TEST_VAR=fromfile\n")
    monkeypatch.setenv("VIMAX_TEST_VAR", "preset")
    load_env_file(f)
    assert os.environ["VIMAX_TEST_VAR"] == "preset"


def test_load_with_override(tmp_path: Path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("VIMAX_TEST_VAR2=fromfile\n")
    monkeypatch.setenv("VIMAX_TEST_VAR2", "preset")
    load_env_file(f, override=True)
    assert os.environ["VIMAX_TEST_VAR2"] == "fromfile"


def test_load_real_vimax_env_format():
    """Sanity-check that our parser handles the actual ViMax .env shape (skips if absent)."""
    home = os.path.expanduser("~/projects/ViMax")
    f = Path(home) / ".env"
    if not f.is_file():
        pytest.skip("ViMax .env not present")
    parsed = parse_env_file(f)
    # We don't assert specific values (secrets); just shape.
    assert "MINIMAX_API_KEY" in parsed
    assert "GOOGLE_API_KEY" in parsed
    assert parsed["MINIMAX_API_KEY"]  # non-empty
