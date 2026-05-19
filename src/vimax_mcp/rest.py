"""REST API mirroring the 6 MCP tools + a /health endpoint.

Design (plan KD1, KD2):
  * Pure transport layer — every handler delegates to ServerContext, which
    is shared with the MCP SSE app under the same uvicorn process.
  * Starlette only (no FastAPI) — pydantic is already in the deps but a
    full DI/OpenAPI stack would be over-built for 7 endpoints.
  * Errors normalize to {"error": str, ...detail}. QuotaExhausted → 429,
    KeyError on job_id → 404, bad payload → 400.

Build with `build_app(ctx)` and either run it standalone (Mount("/", app))
or mount it under "/api/v1" from a parent Starlette app — see
vimax_mcp.server.main().
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import artifacts as artifacts_mod
from . import __version__
from .progress import infer_progress
from .quota import QuotaExhausted

if TYPE_CHECKING:
    from .server import ServerContext

logger = logging.getLogger("vimax_mcp.rest")

# A process-wide start time the /health endpoint reports so callers can
# tell whether the daemon restarted between two probes.
_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _err(message: str, status: int = 400, **extra) -> JSONResponse:
    payload = {"error": message, **extra}
    return JSONResponse(payload, status_code=status)


async def _read_json(request: Request) -> dict:
    """Read+parse a JSON body, raising the same shape as our error helper.

    Returns {} for an empty body so endpoints with all-optional fields
    (cancel_job) keep working without a request body.
    """
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _BadRequest(f"invalid json: {exc.msg}")
    if not isinstance(data, dict):
        raise _BadRequest("request body must be a JSON object")
    return data


class _BadRequest(Exception):
    """Internal — surfaced by handlers as a 400 JSON response."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def _wrap_endpoint(ctx, handler):
    """Wrap a handler so common error types map to JSON responses."""

    async def endpoint(request: Request) -> JSONResponse:
        try:
            return await handler(ctx, request)
        except _BadRequest as exc:
            return _err(exc.message, status=400)
        except KeyError as exc:
            return _err(str(exc).strip("'\""), status=404)
        except QuotaExhausted as exc:
            return JSONResponse(exc.to_dict(), status_code=429)
        except ValueError as exc:
            return _err(str(exc), status=400)
        except Exception as exc:  # noqa: BLE001 — last-resort guard
            logger.exception("REST handler crashed: %s %s", request.method, request.url.path)
            return _err(f"internal error: {type(exc).__name__}: {exc}", status=500)

    return endpoint


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _h_health(ctx, request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "started_at": _STARTED_AT,
            "pid": os.getpid(),
        }
    )


async def _h_submit_idea2video(ctx, request: Request) -> JSONResponse:
    body = await _read_json(request)
    if "idea" not in body:
        raise _BadRequest("missing required field: idea")
    job = ctx.submit(
        kind="idea2video",
        inputs={
            "idea": body["idea"],
            "user_requirement": body.get("user_requirement", ""),
            "style": body.get("style", ""),
        },
        profile=body.get("profile", "default"),
        job_id=body.get("job_id"),
    )
    return JSONResponse({"job_id": job.id, "working_dir": job.working_dir, "state": job.state})


async def _h_submit_script2video(ctx, request: Request) -> JSONResponse:
    body = await _read_json(request)
    if "script" not in body:
        raise _BadRequest("missing required field: script")
    inputs = {
        "script": body["script"],
        "user_requirement": body.get("user_requirement", ""),
        "style": body.get("style", ""),
    }
    if "characters" in body:
        inputs["characters"] = body["characters"]
    if "character_portraits_registry" in body:
        inputs["character_portraits_registry"] = body["character_portraits_registry"]
    job = ctx.submit(
        kind="script2video",
        inputs=inputs,
        profile=body.get("profile", "default"),
        job_id=body.get("job_id"),
    )
    return JSONResponse({"job_id": job.id, "working_dir": job.working_dir, "state": job.state})


async def _h_get_job_status(ctx, request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    try:
        job = ctx.registry.get(job_id)
    except KeyError:
        raise KeyError(f"job {job_id} not found")
    progress = infer_progress(Path(job.working_dir))
    return JSONResponse(
        {
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
    )


async def _h_list_artifacts(ctx, request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    kind = request.query_params.get("kind", "all")
    if kind not in ("final", "frames", "intermediate", "all"):
        raise _BadRequest(f"invalid kind: {kind}")
    try:
        job = ctx.registry.get(job_id)
    except KeyError:
        raise KeyError(f"job {job_id} not found")
    arts = artifacts_mod.scan(Path(job.working_dir), kind=kind)  # type: ignore[arg-type]
    return JSONResponse({"job_id": job.id, "artifacts": artifacts_mod.to_dicts(arts)})


async def _h_cancel_job(ctx, request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    try:
        job = ctx.registry.get(job_id)
    except KeyError:
        raise KeyError(f"job {job_id} not found")
    if job.state in ("done", "failed", "cancelled"):
        return JSONResponse({"ok": False, "reason": f"already {job.state}", "job_id": job.id})
    event = ctx.cancel_events.get(job.id)
    if event is not None:
        event.set()
    task = ctx.tasks.get(job.id)
    if task is not None and not task.done():
        task.cancel()
    ctx.registry.update(job, state="cancelled")
    return JSONResponse({"ok": True, "cancelled_at": job.updated_at, "job_id": job.id})


async def _h_get_quota(ctx, request: Request) -> JSONResponse:
    snap = ctx.quota.snapshot()
    return JSONResponse(snap.to_dict())


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def build_app(ctx) -> Starlette:
    """Construct the REST sub-app. Mount it under whatever prefix you want.

    Typical: parent_app = Starlette(routes=[Mount("/api/v1", app=build_app(ctx)), ...]).
    """
    routes = [
        Route("/health", _wrap_endpoint(ctx, _h_health), methods=["GET"]),
        Route(
            "/jobs/idea2video",
            _wrap_endpoint(ctx, _h_submit_idea2video),
            methods=["POST"],
        ),
        Route(
            "/jobs/script2video",
            _wrap_endpoint(ctx, _h_submit_script2video),
            methods=["POST"],
        ),
        Route(
            "/jobs/{job_id}",
            _wrap_endpoint(ctx, _h_get_job_status),
            methods=["GET"],
        ),
        Route(
            "/jobs/{job_id}/artifacts",
            _wrap_endpoint(ctx, _h_list_artifacts),
            methods=["GET"],
        ),
        Route(
            "/jobs/{job_id}/cancel",
            _wrap_endpoint(ctx, _h_cancel_job),
            methods=["POST"],
        ),
        Route("/quota", _wrap_endpoint(ctx, _h_get_quota), methods=["GET"]),
    ]
    return Starlette(routes=routes)
