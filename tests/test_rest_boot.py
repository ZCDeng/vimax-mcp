"""Composite server boot smoke tests.

Verify the parent Starlette app boots under uvicorn and both mounts
behave correctly:
  - --transport both:  /api/v1/health 200, /mcp/sse reachable
  - --transport http:  /api/v1/health 200, /mcp/sse 404
  - --transport sse (deprecated alias): behaves like both
  - --transport stdio remains covered by test_stdio_handshake.py

We don't speak full MCP-over-SSE here — that's handled by stdio_handshake.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _spawn_server(tmp_path: Path, transport: str, port: int) -> subprocess.Popen:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "VIMAX_JOBS_DIR": str(tmp_path / "jobs"),
        "VIMAX_QUOTA_FILE": str(tmp_path / "quota.json"),
        "VIMAX_HOME": "/tmp/_intentionally_missing",
        "VIMAX_MCP_LOG": "WARNING",
    }
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "vimax_mcp.server",
            "--transport",
            transport,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _http_status(url: str, timeout: float = 3.0) -> int:
    """GET url and return HTTP status code; 0 on connection error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_transport_both_serves_rest_and_mcp(tmp_path: Path):
    port = _pick_port()
    proc = _spawn_server(tmp_path, "both", port)
    try:
        assert _wait_for_port("127.0.0.1", port, timeout=8.0), (
            f"composite server did not bind 127.0.0.1:{port}"
        )
        # REST health
        assert _http_status(f"http://127.0.0.1:{port}/api/v1/health") == 200
        # MCP SSE — GET handshake. Status is 200 (streaming) but our short
        # urlopen will read headers then close; that's enough to prove the
        # route exists. A 404 here would indicate /mcp wasn't mounted.
        status = _http_status(f"http://127.0.0.1:{port}/mcp/sse")
        assert status not in (0, 404), f"/mcp/sse unreachable (status={status})"
    finally:
        _terminate(proc)


def test_transport_http_only_omits_mcp(tmp_path: Path):
    port = _pick_port()
    proc = _spawn_server(tmp_path, "http", port)
    try:
        assert _wait_for_port("127.0.0.1", port, timeout=8.0)
        assert _http_status(f"http://127.0.0.1:{port}/api/v1/health") == 200
        # No MCP mount → 404
        assert _http_status(f"http://127.0.0.1:{port}/mcp/sse") == 404
    finally:
        _terminate(proc)


def test_transport_sse_alias_warns_and_acts_as_both(tmp_path: Path):
    port = _pick_port()
    proc = _spawn_server(tmp_path, "sse", port)
    try:
        assert _wait_for_port("127.0.0.1", port, timeout=8.0)
        assert _http_status(f"http://127.0.0.1:{port}/api/v1/health") == 200
        assert _http_status(f"http://127.0.0.1:{port}/mcp/sse") not in (0, 404)
    finally:
        _terminate(proc)
    # Deprecation warning surfaced on stderr.
    err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
    assert "deprecated" in err.lower()


def test_rest_health_returns_expected_fields(tmp_path: Path):
    port = _pick_port()
    proc = _spawn_server(tmp_path, "http", port)
    try:
        assert _wait_for_port("127.0.0.1", port, timeout=8.0)
        with urllib.request.urlopen(  # noqa: S310
            f"http://127.0.0.1:{port}/api/v1/health", timeout=3
        ) as r:
            import json
            body = json.loads(r.read())
        assert body["status"] == "ok"
        assert "version" in body
        assert "started_at" in body
    finally:
        _terminate(proc)
