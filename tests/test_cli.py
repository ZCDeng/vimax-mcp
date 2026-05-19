"""CLI tests — parser shape, output rendering, error paths, exit codes.

Network calls are intercepted with httpx.MockTransport so tests stay
hermetic. The CLI uses httpx.Client(...) so we monkey-patch httpx.Client
inside vimax_mcp.cli to use the mock.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import httpx
import pytest

from vimax_mcp import cli as cli_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_mock(monkeypatch, handler):
    """Patch httpx.Client used inside cli._request to use a MockTransport.

    Snapshot the real class *before* patching so the factory doesn't recurse
    into its own replacement.
    """
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cli_mod.httpx, "Client", _client_factory)


def _run(argv, monkeypatch=None, handler=None):
    """Run cli.main(argv); capture stdout/stderr and return (exit_code, out, err)."""
    if handler is not None:
        assert monkeypatch is not None, "pass monkeypatch when using a handler"
        _install_mock(monkeypatch, handler)

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_mod.main(argv)
        except SystemExit as e:
            code = int(e.code) if e.code is not None else 0
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Parser sanity
# ---------------------------------------------------------------------------

def test_no_command_prints_help_and_exits_zero(monkeypatch):
    code, out, err = _run([])
    assert code == 0
    assert "vimax" in out
    assert "submit-idea" in out


def test_unknown_subcommand_errors(monkeypatch):
    # argparse exits with code 2 by default on parse errors.
    code, out, err = _run(["bogus-cmd"])
    assert code == 2
    assert "invalid choice" in err or "unrecognized" in err


# ---------------------------------------------------------------------------
# Happy paths (mocked transport)
# ---------------------------------------------------------------------------

def test_health_human_output(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/health"
        return httpx.Response(
            200,
            json={"status": "ok", "version": "0.1.0", "started_at": "2026-05-19T00:00:00+00:00", "pid": 12345},
        )

    code, out, err = _run(["health"], monkeypatch=monkeypatch, handler=h)
    assert code == 0
    assert "status: ok" in out
    assert "version:    0.1.0" in out
    assert "pid:        12345" in out


def test_health_json_mode(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "0.1.0", "started_at": "x", "pid": 1})

    code, out, err = _run(["--json", "health"], monkeypatch=monkeypatch, handler=h)
    assert code == 0
    parsed = json.loads(out)
    assert parsed["status"] == "ok"


def test_quota_human_output(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "date": "2026-05-19",
                "chat": {"used_today": 12, "limit": 100},
                "image": {"used_today": 0, "limit": 200},
                "video": {"used_today": 10, "limit": 10},
            },
        )

    code, out, err = _run(["quota"], monkeypatch=monkeypatch, handler=h)
    assert code == 0
    assert "date: 2026-05-19" in out
    assert "chat" in out
    assert "12" in out and "100" in out
    assert "10 / 10" in out  # video saturated


def test_submit_idea_serializes_body(monkeypatch):
    captured = {}

    def h(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={"job_id": "ABC123", "working_dir": "/tmp/ABC123", "state": "queued"},
        )

    code, out, err = _run(
        ["submit-idea", "--idea", "a cat on a roof", "--style", "Cartoon"],
        monkeypatch=monkeypatch,
        handler=h,
    )
    assert code == 0
    assert captured["url"].endswith("/api/v1/jobs/idea2video")
    assert captured["body"]["idea"] == "a cat on a roof"
    assert captured["body"]["style"] == "Cartoon"
    assert captured["body"]["profile"] == "default"
    assert "job_id:      ABC123" in out
    assert "state:" in out and "queued" in out


def test_submit_script_reads_at_file(monkeypatch, tmp_path: Path):
    script_path = tmp_path / "script.txt"
    script_path.write_text("FADE IN: ...")
    captured = {}

    def h(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200, json={"job_id": "S1", "working_dir": "/tmp/S1", "state": "queued"}
        )

    code, out, err = _run(
        ["submit-script", "--script", f"@{script_path}"],
        monkeypatch=monkeypatch,
        handler=h,
    )
    assert code == 0
    assert captured["body"]["script"] == "FADE IN: ..."


def test_list_human_output(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v1/jobs"
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "job_id": "J1",
                        "kind": "idea2video",
                        "state": "running",
                        "submitted_at": "2026-05-19T00:00:00+00:00",
                        "updated_at": "2026-05-19T00:01:00+00:00",
                        "summary": "a cat on a roof",
                        "final_video": None,
                    },
                    {
                        "job_id": "J2",
                        "kind": "script2video",
                        "state": "done",
                        "submitted_at": "2026-05-18T00:00:00+00:00",
                        "updated_at": "2026-05-18T00:30:00+00:00",
                        "summary": "FADE IN: something",
                        "final_video": "/tmp/J2/final.mp4",
                    },
                ]
            },
        )

    code, out, err = _run(["list"], monkeypatch=monkeypatch, handler=h)
    assert code == 0
    assert "J1" in out and "J2" in out
    assert "running" in out and "done" in out
    assert "a cat on a roof" in out
    assert "FADE IN" in out


def test_list_empty_output(monkeypatch):
    def h(req):
        return httpx.Response(200, json={"jobs": []})

    code, out, err = _run(["list"], monkeypatch=monkeypatch, handler=h)
    assert code == 0
    assert "no jobs" in out


def test_list_forwards_filters(monkeypatch):
    captured = {}

    def h(req: httpx.Request) -> httpx.Response:
        captured["params"] = dict(req.url.params)
        return httpx.Response(200, json={"jobs": []})

    code, out, err = _run(
        ["list", "--limit", "5", "--kind", "script2video", "--state", "done"],
        monkeypatch=monkeypatch,
        handler=h,
    )
    assert code == 0
    assert captured["params"] == {"limit": "5", "kind": "script2video", "state": "done"}


def test_status_human_output(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "job_id": "J1",
                "kind": "idea2video",
                "state": "running",
                "submitted_at": "2026-05-19T00:00:00+00:00",
                "updated_at": "2026-05-19T00:05:00+00:00",
                "progress": {"current_stage": "render_shots", "completed_stages": ["develop_story", "render_shots"]},
                "final_video": None,
                "working_dir": "/tmp/J1",
                "errors": [],
            },
        )

    code, out, err = _run(["status", "J1"], monkeypatch=monkeypatch, handler=h)
    assert code == 0
    assert "job_id:        J1" in out
    assert "state:" in out and "running" in out
    assert "render_shots" in out
    assert "2 stages completed" in out


def _status_body(state: str, stage: str = "render_shots") -> dict:
    return {
        "job_id": "J1",
        "kind": "idea2video",
        "state": state,
        "submitted_at": "2026-05-19T00:00:00+00:00",
        "updated_at": "2026-05-19T00:01:00+00:00",
        "progress": {"current_stage": stage, "completed_stages": [stage]},
        "final_video": None if state != "done" else "/tmp/J1/final.mp4",
        "working_dir": "/tmp/J1",
        "errors": []
        if state != "failed"
        else [{"stage": "pipeline", "message": "boom", "retriable": False, "at": "x"}],
    }


def test_status_watch_polls_until_done(monkeypatch):
    sequence = iter(["running", "running", "done"])

    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_status_body(next(sequence)))

    code, out, err = _run(
        ["status", "J1", "--watch", "0.05"],
        monkeypatch=monkeypatch,
        handler=h,
    )
    assert code == 0
    # state changed from running -> done, so 2 distinct human-mode blocks
    assert out.count("state:") >= 1
    assert "done" in out


def test_status_watch_failed_returns_exit_3(monkeypatch):
    sequence = iter(["running", "failed"])

    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_status_body(next(sequence)))

    code, out, err = _run(
        ["status", "J1", "--watch", "0.05"],
        monkeypatch=monkeypatch,
        handler=h,
    )
    assert code == cli_mod.EXIT_CLIENT_ERROR
    assert "failed" in out


def test_status_watch_cancelled_returns_exit_0(monkeypatch):
    sequence = iter(["running", "cancelled"])

    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_status_body(next(sequence)))

    code, _, _ = _run(
        ["status", "J1", "--watch", "0.05"], monkeypatch=monkeypatch, handler=h
    )
    assert code == 0


def test_status_watch_404_exits_immediately(monkeypatch):
    calls = {"n": 0}

    def h(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"error": "job NOPE not found"})

    code, _, err = _run(
        ["status", "NOPE", "--watch", "0.05"], monkeypatch=monkeypatch, handler=h
    )
    assert code == cli_mod.EXIT_NOT_FOUND
    assert calls["n"] == 1  # didn't keep retrying


def test_status_watch_json_emits_ndjson(monkeypatch):
    sequence = iter(["queued", "running", "done"])

    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_status_body(next(sequence)))

    code, out, err = _run(
        ["--json", "status", "J1", "--watch", "0.05"],
        monkeypatch=monkeypatch,
        handler=h,
    )
    assert code == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)
        assert parsed["job_id"] == "J1"


def test_status_watch_rejects_tiny_interval(monkeypatch):
    code, _, err = _run(["status", "J1", "--watch", "0.001"])
    assert code == cli_mod.EXIT_INPUT
    assert "interval" in err


def test_status_watch_default_interval(monkeypatch):
    # `--watch` without value → defaults to 5s. Use a state that's already
    # terminal so the test doesn't actually sleep.
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_status_body("done"))

    code, _, _ = _run(
        ["status", "J1", "--watch"], monkeypatch=monkeypatch, handler=h
    )
    assert code == 0


def test_status_without_watch_unchanged(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_status_body("running"))

    code, out, err = _run(["status", "J1"], monkeypatch=monkeypatch, handler=h)
    assert code == 0
    assert "running" in out


def test_artifacts_human_output(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.params.get("kind") == "final"
        return httpx.Response(
            200,
            json={
                "job_id": "J1",
                "artifacts": [
                    {"path": "/tmp/J1/final_video.mp4", "kind": "final", "size": 18 * 1024 * 1024, "mtime": 0.0}
                ],
            },
        )

    code, out, err = _run(
        ["artifacts", "J1", "--kind", "final"], monkeypatch=monkeypatch, handler=h
    )
    assert code == 0
    assert "artifacts: 1" in out
    assert "final_video.mp4" in out


def test_cancel_ok(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "cancelled_at": "2026-05-19T00:00:00+00:00", "job_id": "J1"},
        )

    code, out, err = _run(["cancel", "J1"], monkeypatch=monkeypatch, handler=h)
    assert code == 0
    assert "cancelled" in out
    assert "J1" in out


def test_cancel_already_done(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "reason": "already done", "job_id": "J1"},
        )

    code, out, err = _run(["cancel", "J1"], monkeypatch=monkeypatch, handler=h)
    assert code == 0
    assert "not cancelled" in out
    assert "already done" in out


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_status_404_returns_exit_2(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "job NOPE not found"})

    code, out, err = _run(["status", "NOPE"], monkeypatch=monkeypatch, handler=h)
    assert code == cli_mod.EXIT_NOT_FOUND
    assert "not found" in err


def test_submit_quota_exhausted_returns_exit_3(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error": "quota_exhausted",
                "provider": "video",
                "used": 9,
                "requested": 9,
                "limit": 10,
                "reset_in_seconds": 3600,
            },
        )

    code, out, err = _run(
        ["submit-idea", "--idea", "x"], monkeypatch=monkeypatch, handler=h
    )
    assert code == cli_mod.EXIT_CLIENT_ERROR
    assert "quota exhausted" in err
    assert "video" in err
    assert "3600s" in err


def test_server_500_returns_exit_4(monkeypatch):
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal: boom"})

    code, out, err = _run(["quota"], monkeypatch=monkeypatch, handler=h)
    assert code == cli_mod.EXIT_SERVER_ERROR


def test_connection_refused_returns_exit_5(monkeypatch):
    def fail(*a, **kw):
        raise httpx.ConnectError("connection refused")

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli_mod.httpx, "Client", lambda *a, **kw: FakeClient())
    code, out, err = _run(["health"])
    assert code == cli_mod.EXIT_NETWORK
    assert "cannot reach daemon" in err


def test_missing_at_file_returns_exit_66(monkeypatch):
    # No handler needed; @ resolution fails before any HTTP.
    code, out, err = _run(["submit-idea", "--idea", "@/nonexistent/path.txt"])
    assert code == cli_mod.EXIT_INPUT
    assert "cannot read" in err


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_no_ansi_when_piped(monkeypatch):
    """Output stream is StringIO (not TTY) → no ANSI escapes."""

    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "ok", "version": "0.1.0", "started_at": "x", "pid": 1},
        )

    code, out, err = _run(["health"], monkeypatch=monkeypatch, handler=h)
    assert "\x1b[" not in out


def test_json_flag_accepted_before_or_after_subcommand(monkeypatch):
    """Globals must work in both positions so agents don't memorize order."""

    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "ok", "version": "0.1.0", "started_at": "x", "pid": 1},
        )

    # parent-position (legacy)
    code1, out1, _ = _run(["--json", "health"], monkeypatch=monkeypatch, handler=h)
    # subcommand-position (the form agents naturally type)
    code2, out2, _ = _run(["health", "--json"], monkeypatch=monkeypatch, handler=h)

    assert code1 == 0 and code2 == 0
    for out in (out1, out2):
        parsed = json.loads(out)
        assert parsed["status"] == "ok"


def test_server_flag_accepted_before_or_after_subcommand(monkeypatch):
    captured: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        captured.append(str(req.url))
        return httpx.Response(200, json={"status": "ok", "version": "x", "started_at": "x", "pid": 1})

    _run(
        ["--server", "http://parent.test:8080", "health"],
        monkeypatch=monkeypatch,
        handler=h,
    )
    _run(
        ["health", "--server", "http://child.test:8081"],
        monkeypatch=monkeypatch,
        handler=h,
    )
    assert "parent.test:8080" in captured[0]
    assert "child.test:8081" in captured[1]


def test_subcommand_flag_does_not_clobber_when_omitted(monkeypatch):
    """Subcommand SUPPRESS default means omitting --json on the subparser
    must not overwrite a parent-set --json value with False."""

    def h(req):
        return httpx.Response(200, json={"status": "ok", "version": "x", "started_at": "x", "pid": 1})

    code, out, _ = _run(["--json", "health"], monkeypatch=monkeypatch, handler=h)
    assert code == 0
    json.loads(out)  # must be valid JSON, proves --json survived


def test_server_flag_overrides_default(monkeypatch):
    captured = {}

    def h(req: httpx.Request) -> httpx.Response:
        captured["host"] = req.url.host
        captured["port"] = req.url.port
        return httpx.Response(200, json={"status": "ok", "version": "x", "started_at": "x", "pid": 1})

    code, out, err = _run(
        ["--server", "http://example.test:9999", "health"],
        monkeypatch=monkeypatch,
        handler=h,
    )
    assert code == 0
    assert captured["host"] == "example.test"
    assert captured["port"] == 9999


def test_artifacts_default_kind_is_all(monkeypatch):
    captured = {}

    def h(req: httpx.Request) -> httpx.Response:
        captured["kind"] = req.url.params.get("kind")
        return httpx.Response(200, json={"job_id": "J", "artifacts": []})

    code, out, err = _run(["artifacts", "J"], monkeypatch=monkeypatch, handler=h)
    assert code == 0
    assert captured["kind"] == "all"
