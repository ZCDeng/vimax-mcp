"""MCP server exposing tools wrapping ViMax pipelines.

Transports supported: stdio (default) and SSE/HTTP. CLI flags:
  --transport {stdio,sse}  (default: stdio)
  --host HOST              (default: 127.0.0.1, sse only)
  --port PORT              (default: 7801, sse only)

Tools (proposal §2.2 + §4.3 get_quota):
  submit_idea2video, submit_script2video, get_job_status,
  list_artifacts, cancel_job, get_quota
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from . import artifacts as artifacts_mod
from .jobs import JobRegistry, default_jobs_root
from .quota import (
    QuotaExhausted,
    QuotaTracker,
    default_quota_path,
    estimate_job_reservation,
    load_limits_from_config,
)
from .runner import run_job

logger = logging.getLogger("vimax_mcp")

_PROGRESS_STAGES = [
    ("story.txt", "develop_story"),
    ("characters.json", "extract_characters"),
    ("character_portraits_registry.json", "generate_portraits"),
    ("script.json", "write_script"),
    ("scene_0/storyboard.json", "design_storyboard"),
    ("scene_0/camera_tree.json", "construct_camera_tree"),
    ("scene_0/final_video.mp4", "render_shots"),
    ("final_video.mp4", "concat"),
]


def _infer_progress(working_dir: Path) -> dict:
    current = "pending"
    completed: list[str] = []
    for rel, label in _PROGRESS_STAGES:
        if (working_dir / rel).exists():
            completed.append(label)
            current = label
    return {"current_stage": current, "completed_stages": completed}


def _resolve_config_for_profile(profile: str, kind: str) -> Path:
    vimax_home = Path(
        os.environ.get("VIMAX_HOME", os.path.expanduser("~/projects/ViMax"))
    )
    base = vimax_home / "configs"
    if profile == "default":
        return base / f"{kind}.yaml"
    return base / f"{kind}_{profile}.yaml"


class ServerContext:
    def __init__(self) -> None:
        self.registry = JobRegistry(default_jobs_root())
        # Quota is shared across job kinds (same providers); pick idea2video
        # config as the canonical source of daily limits — they are identical
        # for video/image and chat differences don't matter for the budget.
        limits = load_limits_from_config(_resolve_config_for_profile("default", "idea2video"))
        self.quota = QuotaTracker(default_quota_path(), limits)
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.reservations: dict[str, dict[str, int]] = {}

    def _reserve_for_job(self, kind: str) -> dict[str, int]:
        estimate = estimate_job_reservation(kind)
        granted: dict[str, int] = {}
        try:
            for provider, amount in estimate.items():
                self.quota.reserve(provider, amount)
                granted[provider] = amount
        except QuotaExhausted as exc:
            # Roll back partial reservations on failure.
            for provider, amount in granted.items():
                self.quota.release(provider, amount)
            raise exc
        return granted

    def submit(
        self, kind: str, inputs: dict, profile: str, job_id: Optional[str]
    ):
        granted = self._reserve_for_job(kind)
        job = self.registry.create_or_resume(
            kind=kind, profile=profile, inputs=inputs, job_id=job_id
        )
        self.reservations[job.id] = granted
        existing = self.tasks.get(job.id)
        if existing and not existing.done():
            return job
        cancel_event = asyncio.Event()
        self.cancel_events[job.id] = cancel_event
        task = asyncio.create_task(
            run_job(job, self.registry, cancel_event),
            name=f"vimax-job-{job.id}",
        )
        # When the task finishes, refund unused reservation on hard failure.
        task.add_done_callback(lambda t: self._on_job_done(job.id))
        self.tasks[job.id] = task
        return job

    def _on_job_done(self, job_id: str) -> None:
        """If a job failed before consuming much, give the reservation back.

        We can't know exact consumption without hooking into ViMax internals,
        so use a coarse heuristic: if state==failed at the 'load' stage,
        nothing was consumed — refund everything. For any later failure or
        success, keep the reservation (safer to over-account than to let a
        flaky job let a flood through).
        """
        try:
            job = self.registry.get(job_id)
        except KeyError:
            return
        granted = self.reservations.pop(job_id, None)
        if granted is None:
            return
        if job.state == "failed" and any(e.stage == "load" for e in job.errors):
            for provider, amount in granted.items():
                self.quota.release(provider, amount)


_ctx: ServerContext | None = None


def _ctx_or_die() -> ServerContext:
    global _ctx
    if _ctx is None:
        _ctx = ServerContext()
    return _ctx


mcp = FastMCP("vimax")


@mcp.tool()
async def submit_idea2video(
    idea: str,
    user_requirement: str = "",
    style: str = "",
    profile: str = "default",
    job_id: Optional[str] = None,
) -> dict:
    """Submit an idea-to-video job. Returns immediately with a job_id; poll get_job_status for progress."""
    ctx = _ctx_or_die()
    try:
        job = ctx.submit(
            kind="idea2video",
            inputs={"idea": idea, "user_requirement": user_requirement, "style": style},
            profile=profile,
            job_id=job_id,
        )
    except QuotaExhausted as exc:
        return exc.to_dict()
    return {"job_id": job.id, "working_dir": job.working_dir, "state": job.state}


@mcp.tool()
async def submit_script2video(
    script: str,
    user_requirement: str = "",
    style: str = "",
    profile: str = "default",
    job_id: Optional[str] = None,
) -> dict:
    """Submit a script-to-video job. Returns immediately with a job_id; poll get_job_status for progress."""
    ctx = _ctx_or_die()
    try:
        job = ctx.submit(
            kind="script2video",
            inputs={"script": script, "user_requirement": user_requirement, "style": style},
            profile=profile,
            job_id=job_id,
        )
    except QuotaExhausted as exc:
        return exc.to_dict()
    return {"job_id": job.id, "working_dir": job.working_dir, "state": job.state}


@mcp.tool()
async def get_job_status(job_id: str) -> dict:
    """Get current state, progress, and error list for a submitted job."""
    ctx = _ctx_or_die()
    try:
        job = ctx.registry.get(job_id)
    except KeyError:
        return {"error": f"job {job_id} not found"}
    progress = _infer_progress(Path(job.working_dir))
    return {
        "job_id": job.id,
        "kind": job.kind,
        "state": job.state,
        "submitted_at": job.submitted_at,
        "updated_at": job.updated_at,
        "progress": progress,
        "errors": [
            {"stage": e.stage, "message": e.message, "retriable": e.retriable, "at": e.at}
            for e in job.errors
        ],
        "final_video": job.final_video,
        "working_dir": job.working_dir,
    }


@mcp.tool()
async def list_artifacts(job_id: str, kind: str = "all") -> dict:
    """List files produced by a job. kind ∈ final | frames | intermediate | all."""
    ctx = _ctx_or_die()
    try:
        job = ctx.registry.get(job_id)
    except KeyError:
        return {"error": f"job {job_id} not found"}
    if kind not in ("final", "frames", "intermediate", "all"):
        return {"error": f"invalid kind: {kind}"}
    arts = artifacts_mod.scan(Path(job.working_dir), kind=kind)  # type: ignore[arg-type]
    return {"job_id": job.id, "artifacts": artifacts_mod.to_dicts(arts)}


@mcp.tool()
async def cancel_job(job_id: str) -> dict:
    """Cancel a running or queued job. Working_dir is preserved for inspection."""
    ctx = _ctx_or_die()
    try:
        job = ctx.registry.get(job_id)
    except KeyError:
        return {"error": f"job {job_id} not found"}
    if job.state in ("done", "failed", "cancelled"):
        return {"ok": False, "reason": f"already {job.state}", "job_id": job.id}
    event = ctx.cancel_events.get(job.id)
    if event is not None:
        event.set()
    task = ctx.tasks.get(job.id)
    if task is not None and not task.done():
        task.cancel()
    ctx.registry.update(job, state="cancelled")
    return {"ok": True, "cancelled_at": job.updated_at, "job_id": job.id}


@mcp.tool()
async def get_quota() -> dict:
    """Today's daily quota usage for chat / image / video providers (UTC day)."""
    ctx = _ctx_or_die()
    snap = ctx.quota.snapshot()
    return snap.to_dict()


def main() -> None:
    parser = argparse.ArgumentParser(prog="vimax-mcp")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse"),
        default=os.environ.get("VIMAX_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.environ.get("VIMAX_MCP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("VIMAX_MCP_PORT", "7801")),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("VIMAX_MCP_LOG", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _ctx_or_die()

    if args.transport == "stdio":
        mcp.run()
    else:
        # FastMCP exposes host/port via the settings object before starting SSE.
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        logger.info("starting SSE transport on http://%s:%d/sse", args.host, args.port)
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
