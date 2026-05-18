from pathlib import Path

from vimax_mcp.jobs import JobRegistry, new_ulid


def test_ulid_format_and_uniqueness():
    seen = set()
    for _ in range(50):
        u = new_ulid()
        assert len(u) == 26
        assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in u)
        assert u not in seen
        seen.add(u)


def test_ulid_time_ordered():
    a = new_ulid()
    b = new_ulid()
    assert a[:10] <= b[:10]


def test_registry_create_and_get(tmp_path: Path):
    reg = JobRegistry(tmp_path)
    job = reg.create(
        kind="idea2video",
        profile="default",
        inputs={"idea": "test"},
    )
    assert job.state == "queued"
    assert job.id is not None
    assert (tmp_path / job.id / "meta.json").is_file()

    reloaded = JobRegistry(tmp_path).get(job.id)
    assert reloaded.id == job.id
    assert reloaded.kind == "idea2video"
    assert reloaded.inputs["idea"] == "test"


def test_registry_update_state(tmp_path: Path):
    reg = JobRegistry(tmp_path)
    job = reg.create(kind="idea2video", profile="default", inputs={"idea": "x"})
    reg.update(job, state="running")
    assert reg.get(job.id).state == "running"


def test_registry_add_error(tmp_path: Path):
    reg = JobRegistry(tmp_path)
    job = reg.create(kind="idea2video", profile="default", inputs={"idea": "x"})
    reg.add_error(job, stage="pipeline", message="boom", retriable=False)
    after = reg.get(job.id)
    assert after.state == "failed"
    assert len(after.errors) == 1
    assert after.errors[0].retriable is False


def test_resume_clears_retriable_errors(tmp_path: Path):
    reg = JobRegistry(tmp_path)
    job = reg.create(kind="idea2video", profile="default", inputs={"idea": "x"})
    reg.add_error(job, stage="pipeline", message="rate limit", retriable=True)
    resumed = reg.create_or_resume(
        kind="idea2video", profile="default", inputs={"idea": "x"}, job_id=job.id
    )
    assert resumed.state == "queued"
    assert resumed.errors == []


def test_resume_blocks_non_retriable(tmp_path: Path):
    reg = JobRegistry(tmp_path)
    job = reg.create(kind="idea2video", profile="default", inputs={"idea": "x"})
    reg.add_error(job, stage="pipeline", message="content filter", retriable=False)
    import pytest

    with pytest.raises(ValueError, match="non-retriable"):
        reg.create_or_resume(
            kind="idea2video", profile="default", inputs={"idea": "x"}, job_id=job.id
        )
