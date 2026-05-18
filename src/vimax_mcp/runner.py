"""Drive ViMax pipelines from MCP-submitted jobs.

Three concerns:
1. Import ViMax lazily — langchain pulls a heavy graph at import time
   (proposal 5.3 #2). Lazy keeps server startup fast.
2. Patch `Script2VideoPipeline.__init__` to install instance-level dicts
   for `character_portrait_events / shot_desc_events / frame_events`.
   Upstream declares them at class scope; without patching, state leaks
   across jobs (proposal 5.3 #1).
3. Build the pipeline ourselves (replicating `init_from_config`) so we can
   inject a per-job `working_dir` without writing a temp yaml.

A module-level `asyncio.Semaphore(1)` serializes jobs. Pipelines fan out
internally; we just ensure two top-level jobs don't share the global Veo
rate limit at the same time.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Optional

import yaml

from .jobs import Job, JobRegistry

logger = logging.getLogger(__name__)

_JOB_SEMAPHORE = asyncio.Semaphore(1)
_VIMAX_PATCHED = False


def _ensure_vimax_on_path() -> Path:
    home = Path(os.environ.get("VIMAX_HOME", os.path.expanduser("~/projects/ViMax")))
    if not (home / "pipelines" / "idea2video_pipeline.py").is_file():
        raise FileNotFoundError(
            f"VIMAX_HOME={home} does not look like a ViMax checkout"
        )
    p = str(home)
    if p not in sys.path:
        sys.path.insert(0, p)
    return home


def _patch_script2video_instance_state() -> None:
    """Make the three event dicts instance-level so multi-job runs don't share them."""
    global _VIMAX_PATCHED
    if _VIMAX_PATCHED:
        return
    from pipelines.script2video_pipeline import Script2VideoPipeline

    orig_init = Script2VideoPipeline.__init__

    def patched_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        self.character_portrait_events = {}
        self.shot_desc_events = {}
        self.frame_events = {}

    Script2VideoPipeline.__init__ = patched_init
    _VIMAX_PATCHED = True
    logger.info("Patched Script2VideoPipeline.__init__ for instance-level event dicts")


def _resolve_config_path(profile: str, kind: str, vimax_home: Path) -> Path:
    """Map (profile, kind) -> configs/<kind>[_<profile>].yaml."""
    base = vimax_home / "configs"
    if profile == "default":
        candidate = base / f"{kind}.yaml"
    else:
        candidate = base / f"{kind}_{profile}.yaml"
    if not candidate.is_file():
        raise FileNotFoundError(f"profile config not found: {candidate}")
    return candidate


def _load_and_build_pipeline(config_path: Path, kind: str, job_working_dir: Path):
    """Replicate `init_from_config` but pin working_dir to the job dir.

    Imports happen here (not at module top) so the heavy chain only loads
    when a job actually starts.
    """
    from utils.env_expand import expand_env
    from utils.provider_presets import resolve_chat_model_config
    from utils.think_strip import wrap_chat_model_strip_think
    from tools.render_backend import RenderBackend
    from langchain.chat_models import init_chat_model

    with config_path.open() as f:
        config = yaml.safe_load(f)
    config = expand_env(config)

    chat_args = resolve_chat_model_config(config["chat_model"]["init_args"])
    chat_model = init_chat_model(**chat_args)
    chat_model = wrap_chat_model_strip_think(chat_model)
    backend = RenderBackend.from_config(config)

    job_working_dir.mkdir(parents=True, exist_ok=True)

    if kind == "idea2video":
        from pipelines.idea2video_pipeline import Idea2VideoPipeline
        return Idea2VideoPipeline(
            chat_model=chat_model,
            image_generator=backend.image_generator,
            video_generator=backend.video_generator,
            working_dir=str(job_working_dir),
        )
    if kind == "script2video":
        from pipelines.script2video_pipeline import Script2VideoPipeline
        return Script2VideoPipeline(
            chat_model=chat_model,
            image_generator=backend.image_generator,
            video_generator=backend.video_generator,
            working_dir=str(job_working_dir),
        )
    raise ValueError(f"unknown kind: {kind}")


def _classify_error(exc: BaseException) -> tuple[str, bool]:
    """Return (message, retriable). Maps proposal §4.4."""
    name = type(exc).__name__
    msg = f"{name}: {exc}"
    text = str(exc).lower()
    if "content" in text and ("filter" in text or "policy" in text or "block" in text):
        return msg, False
    if "safety" in text:
        return msg, False
    return msg, True


async def run_job(
    job: Job,
    registry: JobRegistry,
    cancel_event: Optional[asyncio.Event] = None,
) -> None:
    """Async-task entrypoint; called via asyncio.create_task from server.py."""
    async with _JOB_SEMAPHORE:
        if cancel_event is not None and cancel_event.is_set():
            registry.update(job, state="cancelled")
            return

        vimax_home = _ensure_vimax_on_path()
        _patch_script2video_instance_state()

        try:
            config_path = _resolve_config_path(job.profile, job.kind, vimax_home)
            pipeline = await asyncio.to_thread(
                _load_and_build_pipeline,
                config_path,
                job.kind,
                Path(job.working_dir),
            )
        except Exception as exc:
            msg, retriable = _classify_error(exc)
            logger.exception("job %s: load failed", job.id)
            registry.add_error(job, stage="load", message=msg, retriable=retriable)
            return

        registry.update(job, state="running")

        try:
            if job.kind == "idea2video":
                result = await pipeline(
                    idea=job.inputs["idea"],
                    user_requirement=job.inputs.get("user_requirement", ""),
                    style=job.inputs.get("style", ""),
                )
            else:
                result = await pipeline(
                    script=job.inputs["script"],
                    user_requirement=job.inputs.get("user_requirement", ""),
                    style=job.inputs.get("style", ""),
                    characters=job.inputs.get("characters"),
                    character_portraits_registry=job.inputs.get(
                        "character_portraits_registry"
                    ),
                )
        except asyncio.CancelledError:
            registry.update(job, state="cancelled")
            raise
        except Exception as exc:
            msg, retriable = _classify_error(exc)
            logger.exception("job %s: pipeline failed", job.id)
            registry.add_error(job, stage="pipeline", message=msg, retriable=retriable)
            return

        final = Path(job.working_dir) / "final_video.mp4"
        if final.is_file():
            registry.update(job, state="done", final_video=str(final))
        else:
            registry.add_error(
                job,
                stage="finalize",
                message=f"pipeline returned {result!r} but final_video.mp4 not found",
                retriable=True,
            )
