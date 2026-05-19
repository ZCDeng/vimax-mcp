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


def test_list_jobs_newest_first(tmp_path: Path):
    import time as _t

    reg = JobRegistry(tmp_path)
    j1 = reg.create(kind="idea2video", profile="default", inputs={"idea": "first"})
    _t.sleep(1.05)  # submitted_at rounds to seconds; needs >1s gap
    j2 = reg.create(kind="idea2video", profile="default", inputs={"idea": "second"})
    listed = reg.list_jobs()
    ids = [j.id for j in listed]
    assert ids == [j2.id, j1.id]


def test_list_jobs_filters_by_kind_and_state(tmp_path: Path):
    reg = JobRegistry(tmp_path)
    j_idea = reg.create(kind="idea2video", profile="default", inputs={"idea": "a"})
    j_script = reg.create(kind="script2video", profile="default", inputs={"script": "b"})
    reg.update(j_script, state="done")
    only_script = reg.list_jobs(kind="script2video")
    assert [j.id for j in only_script] == [j_script.id]
    only_done = reg.list_jobs(state="done")
    assert [j.id for j in only_done] == [j_script.id]
    only_queued_idea = reg.list_jobs(kind="idea2video", state="queued")
    assert [j.id for j in only_queued_idea] == [j_idea.id]


def test_list_jobs_respects_limit(tmp_path: Path):
    reg = JobRegistry(tmp_path)
    for _ in range(5):
        reg.create(kind="idea2video", profile="default", inputs={"idea": "x"})
    assert len(reg.list_jobs(limit=3)) == 3
    assert len(reg.list_jobs(limit=999)) == 5


def test_list_jobs_skips_malformed_meta(tmp_path: Path):
    reg = JobRegistry(tmp_path)
    reg.create(kind="idea2video", profile="default", inputs={"idea": "ok"})
    bad = tmp_path / "GARBAGE01"
    bad.mkdir()
    (bad / "meta.json").write_text("{not json")
    listed = reg.list_jobs()
    assert len(listed) == 1
    assert listed[0].inputs["idea"] == "ok"


def test_list_jobs_empty_root(tmp_path: Path):
    reg = JobRegistry(tmp_path / "nope")
    # Constructor creates the dir; list should still work and return empty.
    assert reg.list_jobs() == []
