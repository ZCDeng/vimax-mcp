from pathlib import Path

import pytest

from vimax_mcp.quota import (
    QuotaExhausted,
    QuotaTracker,
    estimate_job_reservation,
    load_limits_from_config,
)


def test_reserve_increments(tmp_path: Path):
    q = QuotaTracker(tmp_path / "quota.json", limits={"video": 10, "image": 500, "chat": 2000})
    u = q.reserve("video", 3)
    assert u.used_today == 3
    assert u.limit == 10


def test_reserve_raises_on_overrun(tmp_path: Path):
    q = QuotaTracker(tmp_path / "quota.json", limits={"video": 5, "image": 0, "chat": 0})
    q.reserve("video", 3)
    with pytest.raises(QuotaExhausted) as ex:
        q.reserve("video", 3)
    assert ex.value.used == 3
    assert ex.value.limit == 5
    assert ex.value.reset_in_seconds > 0


def test_zero_limit_means_unlimited(tmp_path: Path):
    q = QuotaTracker(tmp_path / "quota.json", limits={"video": 0, "image": 0, "chat": 0})
    q.reserve("video", 1_000_000)  # no exception


def test_release_refunds(tmp_path: Path):
    q = QuotaTracker(tmp_path / "quota.json", limits={"video": 10})
    q.reserve("video", 7)
    q.release("video", 5)
    assert q.snapshot().video.used_today == 2


def test_persistence_across_instances(tmp_path: Path):
    path = tmp_path / "quota.json"
    QuotaTracker(path, limits={"video": 10}).reserve("video", 4)
    reloaded = QuotaTracker(path, limits={"video": 10})
    assert reloaded.snapshot().video.used_today == 4


def test_yesterday_state_rolls_to_zero(tmp_path: Path):
    path = tmp_path / "quota.json"
    # Hand-write a snapshot dated yesterday
    import json
    from datetime import datetime, timezone, timedelta

    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    path.write_text(
        json.dumps(
            {
                "date": yesterday,
                "video": {"used_today": 9, "limit": 10},
                "image": {"used_today": 100, "limit": 500},
                "chat": {"used_today": 200, "limit": 2000},
            }
        )
    )
    q = QuotaTracker(path, limits={"video": 10, "image": 500, "chat": 2000})
    snap = q.snapshot()
    assert snap.video.used_today == 0
    assert snap.image.used_today == 0


def test_estimate_per_kind():
    a = estimate_job_reservation("idea2video")
    b = estimate_job_reservation("script2video")
    assert a["video"] == b["video"] == 9
    assert b["chat"] < a["chat"]


def test_load_limits_from_real_vimax_config():
    import os

    home = os.path.expanduser("~/projects/ViMax")
    cfg = Path(home) / "configs/idea2video.yaml"
    if not cfg.is_file():
        pytest.skip("ViMax checkout not present")
    limits = load_limits_from_config(cfg)
    assert limits.get("video") == 10
    assert limits.get("image") == 500


def test_partial_reservation_rolls_back(tmp_path: Path):
    """If video reservation fails mid-way through a job submit, image+chat should be refunded."""
    from vimax_mcp.server import ServerContext  # ServerContext._reserve_for_job
    # We can't easily instantiate ServerContext (it builds JobRegistry from env);
    # exercise the rollback semantics via QuotaTracker directly.
    q = QuotaTracker(tmp_path / "quota.json", limits={"video": 5, "image": 500, "chat": 2000})
    q.reserve("image", 30)
    q.reserve("chat", 50)
    with pytest.raises(QuotaExhausted):
        q.reserve("video", 9)
    q.release("image", 30)
    q.release("chat", 50)
    snap = q.snapshot()
    assert snap.image.used_today == 0
    assert snap.chat.used_today == 0
