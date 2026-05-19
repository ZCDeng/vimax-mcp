"""`vimax` CLI — thin shell over the REST API.

Plan KD3, KD4: argparse + httpx, sync. Default human-readable output,
`--json` for structured. Designed to be authorized in agent settings
via `Bash(vimax:*)` and replace the MCP transport for day-to-day work.

Exit codes (loosely sysexits.h):
  0   success
  2   not found (404)
  3   client-side error (4xx other than 404, including quota_exhausted)
  4   server-side error (5xx)
  5   cannot reach daemon (connection refused / DNS / timeout)
  66  invalid user input (e.g. @file path missing)
  64  argparse usage error (handled by argparse itself)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

import httpx

DEFAULT_SERVER = os.environ.get("VIMAX_SERVER", "http://127.0.0.1:7801")
DEFAULT_TIMEOUT = float(os.environ.get("VIMAX_CLI_TIMEOUT", "30"))

EXIT_OK = 0
EXIT_NOT_FOUND = 2
EXIT_CLIENT_ERROR = 3
EXIT_SERVER_ERROR = 4
EXIT_NETWORK = 5
EXIT_INPUT = 66


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_ANSI = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "cyan": "\x1b[36m",
}


def _use_color(stream) -> bool:
    return stream.isatty() and os.environ.get("NO_COLOR") is None


def _color(text: str, name: str, *, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{_ANSI[name]}{text}{_ANSI['reset']}"


def _state_color(state: str) -> str:
    return {
        "done": "green",
        "running": "cyan",
        "queued": "blue",
        "paused_rate_limit": "yellow",
        "failed": "red",
        "cancelled": "dim",
    }.get(state, "reset")


def _read_at_arg(value: Optional[str]) -> Optional[str]:
    """If value starts with '@', read from that file path; else return as-is.

    Used for --script / --idea so callers can pass long inputs via files.
    Returns None for None inputs.
    """
    if value is None:
        return None
    if not value.startswith("@"):
        return value
    path = value[1:]
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        _die(f"cannot read {path}: {exc}", EXIT_INPUT)
        raise  # unreachable


def _die(message: str, code: int) -> None:
    print(f"vimax: {message}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def _request(
    method: str,
    url: str,
    *,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> httpx.Response:
    try:
        with httpx.Client(timeout=timeout) as client:
            return client.request(method, url, json=json_body, params=params)
    except httpx.ConnectError as exc:
        _die(f"cannot reach daemon at {url}: {exc}", EXIT_NETWORK)
    except httpx.TimeoutException as exc:
        _die(f"timeout after {timeout}s: {exc}", EXIT_NETWORK)
    except httpx.HTTPError as exc:
        _die(f"network error: {exc}", EXIT_NETWORK)
    raise AssertionError("unreachable")  # for type checkers


def _status_to_exit(status: int) -> int:
    if status == 404:
        return EXIT_NOT_FOUND
    if 400 <= status < 500:
        return EXIT_CLIENT_ERROR
    if 500 <= status < 600:
        return EXIT_SERVER_ERROR
    return EXIT_OK


# ---------------------------------------------------------------------------
# Response rendering
# ---------------------------------------------------------------------------

def _render_human(body: Any, color: bool, stream) -> None:
    if not isinstance(body, dict):
        print(body, file=stream)
        return

    if "error" in body:
        provider = body.get("provider")
        if body["error"] == "quota_exhausted":
            print(
                _color(
                    f"quota exhausted: {provider} used {body.get('used')} / {body.get('limit')}",
                    "red",
                    enabled=color,
                ),
                file=stream,
            )
            print(
                f"reset in {body.get('reset_in_seconds')}s (UTC midnight)", file=stream
            )
        else:
            print(_color(f"error: {body['error']}", "red", enabled=color), file=stream)
            for k, v in body.items():
                if k == "error":
                    continue
                print(f"  {k}: {v}", file=stream)
        return

    # Health
    if "status" in body and "version" in body:
        print(f"{_color('status:', 'bold', enabled=color)} {body['status']}", file=stream)
        print(f"version:    {body['version']}", file=stream)
        print(f"started_at: {body['started_at']}", file=stream)
        if "pid" in body:
            print(f"pid:        {body['pid']}", file=stream)
        return

    # Quota
    if "date" in body and "chat" in body and "video" in body:
        print(f"date: {body['date']}", file=stream)
        for provider in ("chat", "image", "video"):
            u = body[provider]
            limit = u["limit"] if u["limit"] > 0 else "∞"
            used = u["used_today"]
            line = f"  {provider:6s} {used:>4} / {limit}"
            if isinstance(u["limit"], int) and u["limit"] > 0 and used >= u["limit"]:
                line = _color(line, "red", enabled=color)
            elif isinstance(u["limit"], int) and u["limit"] > 0 and used >= 0.8 * u["limit"]:
                line = _color(line, "yellow", enabled=color)
            print(line, file=stream)
        return

    # Submit response (job_id + working_dir + state, nothing else)
    if set(body.keys()) >= {"job_id", "working_dir", "state"} and "kind" not in body:
        st = body["state"]
        print(f"job_id:      {body['job_id']}", file=stream)
        print(f"state:       {_color(st, _state_color(st), enabled=color)}", file=stream)
        print(f"working_dir: {body['working_dir']}", file=stream)
        return

    # Job status (kind + progress)
    if "kind" in body and "progress" in body:
        st = body["state"]
        print(f"job_id:        {body['job_id']}", file=stream)
        print(f"kind:          {body['kind']}", file=stream)
        print(f"state:         {_color(st, _state_color(st), enabled=color)}", file=stream)
        print(f"submitted_at:  {body['submitted_at']}", file=stream)
        print(f"updated_at:    {body['updated_at']}", file=stream)
        prog = body["progress"]
        done = len(prog["completed_stages"])
        print(
            f"progress:      {prog['current_stage']} "
            f"({done} stages completed)",
            file=stream,
        )
        if body.get("final_video"):
            print(f"final_video:   {body['final_video']}", file=stream)
        if body.get("errors"):
            print(_color("errors:", "red", enabled=color), file=stream)
            for e in body["errors"]:
                tag = "retriable" if e["retriable"] else "fatal"
                print(f"  [{tag}] {e['stage']}: {e['message']}", file=stream)
        return

    # Cancel response
    if "ok" in body and ("cancelled_at" in body or "reason" in body):
        if body["ok"]:
            print(_color("cancelled", "yellow", enabled=color), file=stream)
            print(f"job_id:       {body['job_id']}", file=stream)
            print(f"cancelled_at: {body['cancelled_at']}", file=stream)
        else:
            print(
                _color(f"not cancelled: {body.get('reason', '?')}", "dim", enabled=color),
                file=stream,
            )
            print(f"job_id: {body['job_id']}", file=stream)
        return

    # List jobs response
    if "jobs" in body and isinstance(body["jobs"], list):
        jobs = body["jobs"]
        if not jobs:
            print("(no jobs)", file=stream)
            return
        for j in jobs:
            st = j["state"]
            print(
                f"  {j['job_id']}  "
                f"{_color(st, _state_color(st), enabled=color):<24} "
                f"{j['kind']:<13} "
                f"{j['submitted_at']}",
                file=stream,
            )
            if j.get("summary"):
                print(f"      {_color(j['summary'], 'dim', enabled=color)}", file=stream)
        return

    # Artifacts
    if "artifacts" in body:
        arts = body["artifacts"]
        print(f"job_id:    {body['job_id']}", file=stream)
        print(f"artifacts: {len(arts)}", file=stream)
        for a in arts:
            size_kb = a["size"] / 1024
            print(
                f"  [{a['kind']:12s}] {a['path']}  ({size_kb:,.1f} KB)",
                file=stream,
            )
        return

    # Fallback: dump JSON
    print(json.dumps(body, indent=2, ensure_ascii=False), file=stream)


def _print_response(resp: httpx.Response, json_mode: bool) -> int:
    try:
        body = resp.json()
    except json.JSONDecodeError:
        body = {"error": f"non-json response: {resp.text[:200]}"}

    exit_code = _status_to_exit(resp.status_code)
    out_stream = sys.stdout if exit_code == EXIT_OK else sys.stderr

    if json_mode:
        print(json.dumps(body, ensure_ascii=False, indent=2), file=out_stream)
    else:
        _render_human(body, color=_use_color(out_stream), stream=out_stream)

    return exit_code


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_health(args) -> int:
    r = _request("GET", f"{args.server}/api/v1/health", timeout=args.timeout)
    return _print_response(r, args.json)


def _cmd_quota(args) -> int:
    r = _request("GET", f"{args.server}/api/v1/quota", timeout=args.timeout)
    return _print_response(r, args.json)


def _cmd_list(args) -> int:
    params: dict[str, Any] = {"limit": args.limit}
    if args.kind:
        params["kind"] = args.kind
    if args.state:
        params["state"] = args.state
    r = _request(
        "GET",
        f"{args.server}/api/v1/jobs",
        params=params,
        timeout=args.timeout,
    )
    return _print_response(r, args.json)


def _cmd_status(args) -> int:
    r = _request(
        "GET",
        f"{args.server}/api/v1/jobs/{args.job_id}",
        timeout=args.timeout,
    )
    return _print_response(r, args.json)


def _cmd_artifacts(args) -> int:
    r = _request(
        "GET",
        f"{args.server}/api/v1/jobs/{args.job_id}/artifacts",
        params={"kind": args.kind},
        timeout=args.timeout,
    )
    return _print_response(r, args.json)


def _cmd_cancel(args) -> int:
    r = _request(
        "POST",
        f"{args.server}/api/v1/jobs/{args.job_id}/cancel",
        timeout=args.timeout,
    )
    return _print_response(r, args.json)


def _cmd_submit_idea(args) -> int:
    body: dict[str, Any] = {
        "idea": _read_at_arg(args.idea),
        "user_requirement": args.user_requirement or "",
        "style": args.style or "",
        "profile": args.profile,
    }
    if args.job_id:
        body["job_id"] = args.job_id
    r = _request(
        "POST",
        f"{args.server}/api/v1/jobs/idea2video",
        json_body=body,
        timeout=args.timeout,
    )
    return _print_response(r, args.json)


def _cmd_submit_script(args) -> int:
    body: dict[str, Any] = {
        "script": _read_at_arg(args.script),
        "user_requirement": args.user_requirement or "",
        "style": args.style or "",
        "profile": args.profile,
    }
    if args.job_id:
        body["job_id"] = args.job_id
    r = _request(
        "POST",
        f"{args.server}/api/v1/jobs/script2video",
        json_body=body,
        timeout=args.timeout,
    )
    return _print_response(r, args.json)


# ---------------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vimax",
        description="CLI for the vimax-mcp daemon. Talks REST to the local server.",
    )
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"Daemon URL (default: {DEFAULT_SERVER}; env: VIMAX_SERVER)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON instead of human-readable output.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Request timeout seconds (default: {DEFAULT_TIMEOUT}; env: VIMAX_CLI_TIMEOUT)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_health = sub.add_parser("health", help="Check the daemon is reachable.")
    p_health.set_defaults(func=_cmd_health)

    p_quota = sub.add_parser("quota", help="Show today's per-provider usage.")
    p_quota.set_defaults(func=_cmd_quota)

    p_list = sub.add_parser("list", help="List recent jobs (newest first).")
    p_list.add_argument("--limit", type=int, default=20, help="Max jobs to return (1-500).")
    p_list.add_argument(
        "--kind",
        choices=("idea2video", "script2video"),
        default=None,
        help="Filter by job kind.",
    )
    p_list.add_argument(
        "--state",
        choices=("queued", "running", "paused_rate_limit", "done", "failed", "cancelled"),
        default=None,
        help="Filter by job state.",
    )
    p_list.set_defaults(func=_cmd_list)

    p_status = sub.add_parser("status", help="Get job state + progress.")
    p_status.add_argument("job_id")
    p_status.set_defaults(func=_cmd_status)

    p_arts = sub.add_parser("artifacts", help="List a job's output files.")
    p_arts.add_argument("job_id")
    p_arts.add_argument(
        "--kind",
        choices=("all", "final", "frames", "intermediate"),
        default="all",
    )
    p_arts.set_defaults(func=_cmd_artifacts)

    p_cancel = sub.add_parser("cancel", help="Cancel a running or queued job.")
    p_cancel.add_argument("job_id")
    p_cancel.set_defaults(func=_cmd_cancel)

    p_idea = sub.add_parser("submit-idea", help="Kick off an idea -> video job.")
    p_idea.add_argument(
        "--idea",
        required=True,
        help="The idea text. Prefix with '@' to read from a file (e.g. --idea @prompt.txt).",
    )
    p_idea.add_argument("--style", default="")
    p_idea.add_argument("--user-requirement", default="")
    p_idea.add_argument("--profile", default="default")
    p_idea.add_argument("--job-id", default=None, help="Reuse / resume an existing job_id.")
    p_idea.set_defaults(func=_cmd_submit_idea)

    p_script = sub.add_parser("submit-script", help="Kick off a script -> video job.")
    p_script.add_argument(
        "--script",
        required=True,
        help="The script text. Prefix with '@' to read from a file.",
    )
    p_script.add_argument("--style", default="")
    p_script.add_argument("--user-requirement", default="")
    p_script.add_argument("--profile", default="default")
    p_script.add_argument("--job-id", default=None)
    p_script.set_defaults(func=_cmd_submit_script)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_OK
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
