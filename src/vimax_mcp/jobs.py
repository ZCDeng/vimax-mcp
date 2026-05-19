"""Job state model + filesystem-backed registry.

One subdir per job under VIMAX_JOBS_DIR. meta.json is the source of truth
for state; everything else (story.txt, characters.json, shots/, final_video.mp4)
is produced by the ViMax pipeline and used for resume / artifact listing.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

JobKind = Literal["idea2video", "script2video"]
JobState = Literal[
    "queued",
    "running",
    "paused_rate_limit",
    "done",
    "failed",
    "cancelled",
]


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """Lightweight ULID-ish: 10-char timestamp + 16-char random, Crockford base32.

    Crypto-safe randomness, ASCII-only, time-sortable. Avoids the python-ulid
    dependency. ASCII matters for moviepy/ffmpeg working paths (proposal 5.3).
    """
    ms = int(time.time() * 1000)
    t = ""
    for _ in range(10):
        t = _CROCKFORD[ms & 0x1F] + t
        ms >>= 5
    r = "".join(_CROCKFORD[b & 0x1F] for b in secrets.token_bytes(16))
    return t + r


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobError:
    stage: str
    message: str
    retriable: bool
    at: str = field(default_factory=_utc_iso)


@dataclass
class Job:
    id: str
    kind: JobKind
    state: JobState
    profile: str
    submitted_at: str
    updated_at: str
    inputs: dict
    errors: list[JobError] = field(default_factory=list)
    working_dir: str = ""
    final_video: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["errors"] = [asdict(e) for e in self.errors]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        d = dict(d)
        d["errors"] = [JobError(**e) for e in d.get("errors", [])]
        return cls(**d)


class JobRegistry:
    """Filesystem-backed registry. One dir per job, meta.json per dir.

    Concurrency: an in-memory dict caches loaded jobs; a single asyncio task
    runs at a time (Semaphore(1) in the runner), so we don't need locks here.
    Reads from disk on cache miss so a different MCP session can pick up
    jobs created by a previous process.
    """

    def __init__(self, jobs_root: Path):
        self.root = Path(jobs_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Job] = {}

    def _meta_path(self, job_id: str) -> Path:
        return self.root / job_id / "meta.json"

    def create(
        self,
        kind: JobKind,
        profile: str,
        inputs: dict,
        job_id: Optional[str] = None,
    ) -> Job:
        jid = job_id or new_ulid()
        if (self.root / jid / "meta.json").exists():
            raise ValueError(f"job {jid} already exists; use create_or_resume to reuse")
        now = _utc_iso()
        job = Job(
            id=jid,
            kind=kind,
            state="queued",
            profile=profile,
            submitted_at=now,
            updated_at=now,
            inputs=dict(inputs),
            working_dir=str(self.root / jid),
        )
        (self.root / jid).mkdir(parents=True, exist_ok=True)
        self._save(job)
        return job

    def create_or_resume(
        self,
        kind: JobKind,
        profile: str,
        inputs: dict,
        job_id: Optional[str] = None,
    ) -> Job:
        if job_id and self._meta_path(job_id).exists():
            job = self.get(job_id)
            if job.kind != kind:
                raise ValueError(
                    f"job {job_id} is kind={job.kind}, cannot resume as {kind}"
                )
            if any(not e.retriable for e in job.errors):
                raise ValueError(
                    f"job {job_id} has non-retriable failure; pick a new job_id"
                )
            job.errors = []
            job.state = "queued"
            job.inputs = dict(inputs)
            job.updated_at = _utc_iso()
            self._save(job)
            return job
        return self.create(kind, profile, inputs, job_id=job_id)

    def get(self, job_id: str) -> Job:
        if job_id in self._cache:
            return self._cache[job_id]
        path = self._meta_path(job_id)
        if not path.exists():
            raise KeyError(f"job {job_id} not found")
        with path.open() as f:
            job = Job.from_dict(json.load(f))
        self._cache[job_id] = job
        return job

    def update(self, job: Job, **fields) -> Job:
        for k, v in fields.items():
            setattr(job, k, v)
        job.updated_at = _utc_iso()
        self._save(job)
        return job

    def add_error(
        self, job: Job, stage: str, message: str, retriable: bool
    ) -> Job:
        job.errors.append(JobError(stage=stage, message=message, retriable=retriable))
        return self.update(job, state="failed")

    def _save(self, job: Job) -> None:
        path = self._meta_path(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(job.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        self._cache[job.id] = job

    def list_jobs(
        self,
        limit: int = 20,
        kind: Optional[str] = None,
        state: Optional[str] = None,
    ) -> list[Job]:
        """Recent jobs, newest first.

        Scans every meta.json under jobs_root (one disk read per job). For
        single-user workloads this is fine — limit is on the order of dozens.
        Cache is refreshed for every scanned entry so subsequent get() calls
        don't hit disk twice.
        """
        out: list[Job] = []
        if not self.root.is_dir():
            return out
        for child in sorted(self.root.iterdir()):
            meta = child / "meta.json"
            if not meta.is_file():
                continue
            try:
                with meta.open() as f:
                    job = Job.from_dict(json.load(f))
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue
            if kind is not None and job.kind != kind:
                continue
            if state is not None and job.state != state:
                continue
            self._cache[job.id] = job
            out.append(job)
        out.sort(key=lambda j: j.submitted_at, reverse=True)
        if limit > 0:
            out = out[:limit]
        return out


def default_jobs_root() -> Path:
    env = os.environ.get("VIMAX_JOBS_DIR")
    if env:
        return Path(env)
    vimax_home = os.environ.get("VIMAX_HOME", os.path.expanduser("~/projects/ViMax"))
    return Path(vimax_home) / ".working_dir" / "jobs"
