"""REST handler tests.

Uses a fake ServerContext that mirrors the shape rest.py reads (registry,
quota, cancel_events, tasks) but skips asyncio.create_task — we want to
verify the transport layer, not run the pipeline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from vimax_mcp.jobs import JobRegistry
from vimax_mcp.quota import QuotaTracker
from vimax_mcp.rest import build_app


# Per-test reservation; small enough that we can exhaust video with 2 submits.
_FAKE_RESERVATION = {"chat": 5, "image": 5, "video": 5}


@dataclass
class FakeCtx:
    registry: JobRegistry
    quota: QuotaTracker
    tasks: dict = field(default_factory=dict)
    cancel_events: dict = field(default_factory=dict)
    reservations: dict = field(default_factory=dict)

    def submit(self, kind: str, inputs: dict, profile: str, job_id: Optional[str]):
        # Mirror real ServerContext.submit semantics: reserve first, roll back
        # partial reservations on QuotaExhausted, then create/resume the job.
        granted: dict[str, int] = {}
        try:
            for provider, amount in _FAKE_RESERVATION.items():
                self.quota.reserve(provider, amount)
                granted[provider] = amount
        except Exception:
            for provider, amount in granted.items():
                self.quota.release(provider, amount)
            raise
        job = self.registry.create_or_resume(
            kind=kind, profile=profile, inputs=inputs, job_id=job_id
        )
        self.reservations[job.id] = granted
        # No asyncio.create_task here — transport tests don't need the runner.
        return job


@pytest.fixture
def ctx(tmp_path: Path) -> FakeCtx:
    registry = JobRegistry(tmp_path / "jobs")
    # Generous limits so most tests pass; specific tests reset to tiny budgets.
    quota = QuotaTracker(tmp_path / "quota.json", limits={"chat": 200, "image": 200, "video": 200})
    return FakeCtx(registry=registry, quota=quota)


@pytest.fixture
def client(ctx: FakeCtx) -> TestClient:
    # Mount under /api/v1 to mirror production layout.
    parent = Starlette(routes=[Mount("/api/v1", app=build_app(ctx))])
    return TestClient(parent)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

def test_health_ok(client: TestClient):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "started_at" in body
    assert isinstance(body["pid"], int)


def test_submit_idea2video_returns_job(client: TestClient, ctx: FakeCtx):
    resp = client.post(
        "/api/v1/jobs/idea2video",
        json={"idea": "a cat on a roof", "style": "Cartoon"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "queued"
    assert body["job_id"]
    assert body["working_dir"].endswith(body["job_id"])
    # Side effect: registry has the job
    job = ctx.registry.get(body["job_id"])
    assert job.kind == "idea2video"
    assert job.inputs["idea"] == "a cat on a roof"


def test_submit_script2video_returns_job(client: TestClient, ctx: FakeCtx):
    resp = client.post(
        "/api/v1/jobs/script2video",
        json={"script": "FADE IN: ...", "user_requirement": "≤2 scenes"},
    )
    assert resp.status_code == 200
    job = ctx.registry.get(resp.json()["job_id"])
    assert job.kind == "script2video"
    assert job.inputs["script"].startswith("FADE IN")


def test_get_job_status_includes_progress(client: TestClient, ctx: FakeCtx):
    sub = client.post("/api/v1/jobs/idea2video", json={"idea": "x"}).json()
    jid = sub["job_id"]
    # Drop a checkpoint file so progress is non-pending.
    (Path(sub["working_dir"]) / "story.txt").write_text("once upon a time")
    resp = client.get(f"/api/v1/jobs/{jid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == jid
    assert body["progress"]["current_stage"] == "develop_story"
    assert "develop_story" in body["progress"]["completed_stages"]


def test_list_artifacts_filter_final(client: TestClient, ctx: FakeCtx):
    sub = client.post("/api/v1/jobs/idea2video", json={"idea": "x"}).json()
    jid = sub["job_id"]
    wd = Path(sub["working_dir"])
    (wd / "final_video.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    (wd / "characters.json").write_text("{}")
    resp = client.get(f"/api/v1/jobs/{jid}/artifacts?kind=final")
    assert resp.status_code == 200
    arts = resp.json()["artifacts"]
    assert len(arts) == 1
    assert arts[0]["kind"] == "final"
    assert arts[0]["path"].endswith("final_video.mp4")


def test_cancel_running_job(client: TestClient, ctx: FakeCtx):
    sub = client.post("/api/v1/jobs/idea2video", json={"idea": "x"}).json()
    jid = sub["job_id"]
    # Fake an in-progress task + cancel event so cancel exercises both paths.
    loop = asyncio.new_event_loop()
    try:
        evt = asyncio.Event()
        ctx.cancel_events[jid] = evt
        resp = client.post(f"/api/v1/jobs/{jid}/cancel")
    finally:
        loop.close()
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["job_id"] == jid
    assert ctx.cancel_events[jid].is_set()
    assert ctx.registry.get(jid).state == "cancelled"


def test_cancel_already_done_job(client: TestClient, ctx: FakeCtx):
    sub = client.post("/api/v1/jobs/idea2video", json={"idea": "x"}).json()
    jid = sub["job_id"]
    job = ctx.registry.get(jid)
    ctx.registry.update(job, state="done")
    resp = client.post(f"/api/v1/jobs/{jid}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "already done" in body["reason"]


def test_list_jobs_empty(client: TestClient):
    resp = client.get("/api/v1/jobs")
    assert resp.status_code == 200
    assert resp.json() == {"jobs": []}


def test_list_jobs_returns_summaries(client: TestClient, ctx: FakeCtx):
    s1 = client.post("/api/v1/jobs/idea2video", json={"idea": "first idea"}).json()
    s2 = client.post(
        "/api/v1/jobs/script2video", json={"script": "FADE IN: something"}
    ).json()
    resp = client.get("/api/v1/jobs")
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    assert len(jobs) == 2
    by_id = {j["job_id"]: j for j in jobs}
    assert by_id[s1["job_id"]]["summary"] == "first idea"
    assert by_id[s2["job_id"]]["summary"] == "FADE IN: something"
    assert by_id[s1["job_id"]]["kind"] == "idea2video"


def test_list_jobs_filters(client: TestClient, ctx: FakeCtx):
    s_idea = client.post("/api/v1/jobs/idea2video", json={"idea": "i"}).json()
    s_scr = client.post("/api/v1/jobs/script2video", json={"script": "s"}).json()
    only_script = client.get("/api/v1/jobs?kind=script2video").json()["jobs"]
    assert [j["job_id"] for j in only_script] == [s_scr["job_id"]]
    # state filter — both are queued, so state=done returns empty
    only_done = client.get("/api/v1/jobs?state=done").json()["jobs"]
    assert only_done == []


def test_list_jobs_bad_limit(client: TestClient):
    assert client.get("/api/v1/jobs?limit=0").status_code == 400
    assert client.get("/api/v1/jobs?limit=abc").status_code == 400
    assert client.get("/api/v1/jobs?limit=9999").status_code == 400


def test_list_jobs_bad_filters(client: TestClient):
    assert client.get("/api/v1/jobs?kind=bogus").status_code == 400
    assert client.get("/api/v1/jobs?state=zzz").status_code == 400


def test_list_jobs_truncates_long_source(client: TestClient, ctx: FakeCtx):
    long_idea = "x" * 200
    client.post("/api/v1/jobs/idea2video", json={"idea": long_idea}).json()
    job = client.get("/api/v1/jobs").json()["jobs"][0]
    assert len(job["summary"]) <= 81  # 80 + ellipsis char
    assert job["summary"].endswith("…")


def test_get_quota_returns_snapshot(client: TestClient):
    resp = client.get("/api/v1/quota")
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    for provider in ("chat", "image", "video"):
        assert provider in body
        assert "used_today" in body[provider]
        assert "limit" in body[provider]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_get_status_unknown_job_returns_404(client: TestClient):
    resp = client.get("/api/v1/jobs/NOPE-DOES-NOT-EXIST")
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"]


def test_cancel_unknown_job_returns_404(client: TestClient):
    resp = client.post("/api/v1/jobs/NOPE/cancel")
    assert resp.status_code == 404


def test_list_artifacts_unknown_job_returns_404(client: TestClient):
    resp = client.get("/api/v1/jobs/NOPE/artifacts")
    assert resp.status_code == 404


def test_list_artifacts_bad_kind_returns_400(client: TestClient, ctx: FakeCtx):
    sub = client.post("/api/v1/jobs/idea2video", json={"idea": "x"}).json()
    resp = client.get(f"/api/v1/jobs/{sub['job_id']}/artifacts?kind=bogus")
    assert resp.status_code == 400
    assert "invalid kind" in resp.json()["error"]


def test_submit_quota_exhausted_returns_429(tmp_path: Path):
    registry = JobRegistry(tmp_path / "jobs")
    # Force a video budget of 5 so the second submit (each reserves 5) overruns.
    quota = QuotaTracker(tmp_path / "quota.json", limits={"chat": 100, "image": 100, "video": 5})
    ctx = FakeCtx(registry=registry, quota=quota)
    parent = Starlette(routes=[Mount("/api/v1", app=build_app(ctx))])
    client = TestClient(parent)
    r1 = client.post("/api/v1/jobs/idea2video", json={"idea": "first"})
    assert r1.status_code == 200
    r2 = client.post("/api/v1/jobs/idea2video", json={"idea": "second"})
    assert r2.status_code == 429
    body = r2.json()
    assert body["error"] == "quota_exhausted"
    assert body["provider"] == "video"
    assert body["limit"] == 5
    # Rollback: chat & image refunded so they don't deny future submits.
    snap = quota.snapshot()
    assert snap.chat.used_today == 5  # only the first submit's chat reservation kept
    assert snap.image.used_today == 5


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_submit_missing_required_field_returns_400(client: TestClient):
    resp = client.post("/api/v1/jobs/idea2video", json={"style": "noir"})
    assert resp.status_code == 400
    assert "idea" in resp.json()["error"]

    resp2 = client.post("/api/v1/jobs/script2video", json={"style": "noir"})
    assert resp2.status_code == 400
    assert "script" in resp2.json()["error"]


def test_submit_malformed_json_returns_400(client: TestClient):
    resp = client.post(
        "/api/v1/jobs/idea2video",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert "invalid json" in resp.json()["error"]


def test_submit_non_object_body_returns_400(client: TestClient):
    resp = client.post(
        "/api/v1/jobs/idea2video",
        content=b'"just a string"',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_unknown_route_returns_404(client: TestClient):
    resp = client.get("/api/v1/jobs/idea2video/nonsense")
    assert resp.status_code == 404


def test_wrong_method_returns_405(client: TestClient):
    # /quota is GET-only; POST should bounce.
    resp = client.post("/api/v1/quota")
    assert resp.status_code == 405
