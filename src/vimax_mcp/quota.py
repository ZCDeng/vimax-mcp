"""Daily quota tracker for ViMax API providers.

Why this layer exists (proposal §4.3, expanded after reading ViMax's
rate_limiter.py):

  * ViMax's RateLimiter is in-memory only — restart wipes the counter.
  * RateLimiter.acquire() on a saturated daily limit sleeps up to 24 hours,
    which stalls Semaphore(1) and blocks every subsequent submit.

So the MCP layer keeps its own daily counter on disk, refuses new submits
when the budget would be blown, and lets the user query `get_quota` for
visibility.

This is an *advisory* tracker. We do not hook into every actual API call
(would require monkey-patching ViMax internals). Instead `submit_*`
reserves the upper-bound it might consume; if the job finishes early the
unused share stays reserved until tomorrow. Conservative on purpose.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


PROVIDERS = ("chat", "image", "video")


@dataclass
class ProviderUsage:
    used_today: int = 0
    limit: int = 0


@dataclass
class QuotaSnapshot:
    date: str
    chat: ProviderUsage = field(default_factory=ProviderUsage)
    image: ProviderUsage = field(default_factory=ProviderUsage)
    video: ProviderUsage = field(default_factory=ProviderUsage)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "chat": asdict(self.chat),
            "image": asdict(self.image),
            "video": asdict(self.video),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QuotaSnapshot":
        return cls(
            date=d["date"],
            chat=ProviderUsage(**d.get("chat", {})),
            image=ProviderUsage(**d.get("image", {})),
            video=ProviderUsage(**d.get("video", {})),
        )


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta

    tomorrow = tomorrow + timedelta(days=1)
    return int((tomorrow - now).total_seconds())


class QuotaExhausted(Exception):
    """Raised by reserve() when the daily budget would be exceeded."""

    def __init__(self, provider: str, requested: int, used: int, limit: int):
        self.provider = provider
        self.requested = requested
        self.used = used
        self.limit = limit
        self.reset_in_seconds = _seconds_until_utc_midnight()
        super().__init__(
            f"{provider} quota exhausted: used={used}, requested={requested}, "
            f"limit={limit}, reset_in={self.reset_in_seconds}s"
        )

    def to_dict(self) -> dict:
        return {
            "error": "quota_exhausted",
            "provider": self.provider,
            "used": self.used,
            "requested": self.requested,
            "limit": self.limit,
            "reset_in_seconds": self.reset_in_seconds,
        }


class QuotaTracker:
    """Filesystem-backed daily quota counter.

    Thread/async safety: protected by a threading.Lock since reads happen
    on the MCP request path and writes during reserve(). Single-process
    server, so no inter-process locking needed.
    """

    def __init__(self, path: Path, limits: dict[str, int]):
        """
        limits: {"chat": int, "image": int, "video": int}  — daily caps.
                A 0 or negative value means "unlimited" (no preflight check).
        """
        self.path = Path(path)
        self.limits = {k: int(v) for k, v in limits.items()}
        self._lock = threading.Lock()
        self._snapshot = self._load_or_init()

    def _load_or_init(self) -> QuotaSnapshot:
        if self.path.is_file():
            try:
                with self.path.open() as f:
                    snap = QuotaSnapshot.from_dict(json.load(f))
                if snap.date == _utc_today():
                    # patch limits in case yaml changed
                    snap.chat.limit = self.limits.get("chat", snap.chat.limit)
                    snap.image.limit = self.limits.get("image", snap.image.limit)
                    snap.video.limit = self.limits.get("video", snap.video.limit)
                    return snap
            except (OSError, json.JSONDecodeError, KeyError):
                pass
        return self._fresh()

    def _fresh(self) -> QuotaSnapshot:
        return QuotaSnapshot(
            date=_utc_today(),
            chat=ProviderUsage(limit=self.limits.get("chat", 0)),
            image=ProviderUsage(limit=self.limits.get("image", 0)),
            video=ProviderUsage(limit=self.limits.get("video", 0)),
        )

    def _maybe_roll_day(self) -> None:
        if self._snapshot.date != _utc_today():
            self._snapshot = self._fresh()
            self._save()

    def _get_usage(self, provider: str) -> ProviderUsage:
        return getattr(self._snapshot, provider)

    def reserve(self, provider: str, amount: int) -> ProviderUsage:
        """Atomically charge `amount` units of `provider`. Raises QuotaExhausted on overrun."""
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider: {provider}")
        with self._lock:
            self._maybe_roll_day()
            usage = self._get_usage(provider)
            if usage.limit > 0 and usage.used_today + amount > usage.limit:
                raise QuotaExhausted(
                    provider=provider,
                    requested=amount,
                    used=usage.used_today,
                    limit=usage.limit,
                )
            usage.used_today += amount
            self._save()
            return ProviderUsage(used_today=usage.used_today, limit=usage.limit)

    def release(self, provider: str, amount: int) -> ProviderUsage:
        """Refund a reservation (e.g. when a job fails before consuming API calls)."""
        with self._lock:
            self._maybe_roll_day()
            usage = self._get_usage(provider)
            usage.used_today = max(0, usage.used_today - amount)
            self._save()
            return ProviderUsage(used_today=usage.used_today, limit=usage.limit)

    def snapshot(self) -> QuotaSnapshot:
        with self._lock:
            self._maybe_roll_day()
            return QuotaSnapshot.from_dict(self._snapshot.to_dict())

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(self._snapshot.to_dict(), f, indent=2)
        os.replace(tmp, self.path)


# Conservative per-job estimates. Real consumption varies; these are upper
# bounds for a "small" job (≤5 shots) so submit can fail fast instead of
# starving partway through.
DEFAULT_RESERVATIONS_PER_JOB = {
    "video": 9,    # 2N-1 for N=5 shots
    "image": 30,   # ~9 portraits + ~3 frames/shot
    "chat": 50,    # ~10 agents x several calls each
}


def estimate_job_reservation(kind: str) -> dict[str, int]:
    """Return per-provider reservation for a job of `kind`. Conservative."""
    # script2video skips story development; chat slightly cheaper. Same
    # video/image cost though — those scale with shot count, not source.
    if kind == "script2video":
        return {**DEFAULT_RESERVATIONS_PER_JOB, "chat": 30}
    return dict(DEFAULT_RESERVATIONS_PER_JOB)


def load_limits_from_config(config_path: Path) -> dict[str, int]:
    """Extract daily limits from a ViMax config yaml.

    Falls back to {} (no limits) if file is missing or malformed.
    """
    import yaml

    if not config_path.is_file():
        return {}
    try:
        with config_path.open() as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}

    limits: dict[str, int] = {}
    chat_rpd = data.get("chat_model", {}).get("max_requests_per_day")
    image_rpd = data.get("image_generator", {}).get("max_requests_per_day")
    video_rpd = data.get("video_generator", {}).get("max_requests_per_day")
    if chat_rpd:
        limits["chat"] = int(chat_rpd)
    if image_rpd:
        limits["image"] = int(image_rpd)
    if video_rpd:
        limits["video"] = int(video_rpd)
    return limits


def default_quota_path() -> Path:
    """Pick a quota.json path next to the jobs dir."""
    env = os.environ.get("VIMAX_QUOTA_FILE")
    if env:
        return Path(env)
    vimax_home = os.environ.get("VIMAX_HOME", os.path.expanduser("~/projects/ViMax"))
    return Path(vimax_home) / ".working_dir" / "quota.json"
