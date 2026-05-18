"""MCP server exposing 5 tools wrapping ViMax pipelines.

Transport: stdio (FastMCP default). HTTP+SSE deferred to step 2.

Tools (proposal §2.2):
  submit_idea2video, submit_script2video, get_job_status,
  list_artifacts, cancel_job
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from . import artifacts as artifacts_mod
from .jobs import JobRegistry, default_jobs_root
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


class ServerContext:
    def __init__(self) -> None:
        self.registry = JobRegistry(default_jobs_root())
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancel_events: dict[str, asyncio.Event] = {}

    def submit(self, kind: str, inputs: dict, profile: str, job_id: Optional[str]):
        job = self.registry.create_or_resume(
            kind=kind, profile=profile, inputs=inputs, job_id=job_id
        )
        existing = self.tasks.get(job.id)
        if existing and not existing.done():
            return job
        cancel_event = asyncio.Event()
        self.cancel_events[job.id] = cancel_event
        task = asyncio.create_task(
            run_job(job, self.registry, cancel_event), name=f"vimax-job-{job.id}"
        )
        self.tasks[job.id] = task
        return job


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
    job = ctx.submit(
        kind="idea2video",
        inputs={"idea": idea, "user_requirement": user_requirement, "style": style},
        profile=profile,
        job_id=job_id,
    )
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
    job = ctx.submit(
        kind="script2video",
        inputs={"script": script, "user_requirement": user_requirement, "style": style},
        profile=profile,
        job_id=job_id,
    )
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


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("VIMAX_MCP_LOG", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _ctx_or_die()
    mcp.run()


if __name__ == "__main__":
    main()
