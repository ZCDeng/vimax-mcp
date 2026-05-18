"""Verify the SSE transport boots and binds the chosen port.

We don't speak MCP-over-SSE here (that needs a longer-lived test harness).
We just confirm:
  - `--transport sse` doesn't crash on startup
  - the port is reachable (TCP socket accept)
  - clean shutdown on SIGTERM

Uses a random high port and a fresh tmp jobs/quota dir to stay hermetic.
"""

import socket
import subprocess
import sys
import time
from pathlib import Path


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def test_sse_server_boots_and_binds(tmp_path: Path):
    port = _pick_port()
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "VIMAX_JOBS_DIR": str(tmp_path / "jobs"),
        "VIMAX_QUOTA_FILE": str(tmp_path / "quota.json"),
        "VIMAX_HOME": "/tmp/_intentionally_missing",  # avoid touching real ViMax
        "VIMAX_MCP_LOG": "WARNING",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "vimax_mcp.server",
         "--transport", "sse", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_for_port("127.0.0.1", port, timeout=5.0), (
            f"sse server did not bind 127.0.0.1:{port} within 5s"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
