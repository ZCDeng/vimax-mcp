from pathlib import Path

from vimax_mcp.artifacts import scan


def _build_working_dir(root: Path):
    (root / "story.txt").write_text("story")
    (root / "characters.json").write_text("{}")
    (root / "character_portraits/0_Biscuit").mkdir(parents=True)
    (root / "character_portraits/0_Biscuit/front.png").write_bytes(b"\x89PNG")
    (root / "scene_0/shots/0").mkdir(parents=True)
    (root / "scene_0/shots/0/first_frame.png").write_bytes(b"\x89PNG")
    (root / "scene_0/shots/0/video.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    (root / "scene_0/final_video.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    (root / "final_video.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    (root / "meta.json").write_text("{}")


def test_scan_all(tmp_path: Path):
    _build_working_dir(tmp_path)
    arts = scan(tmp_path, kind="all")
    paths = [a.path for a in arts]
    # meta.json must be excluded; final_video.mp4 must be ranked first.
    assert all("meta.json" not in p for p in paths)
    assert arts[0].kind == "final"
    assert arts[0].path.endswith("/final_video.mp4")


def test_scan_only_final(tmp_path: Path):
    _build_working_dir(tmp_path)
    arts = scan(tmp_path, kind="final")
    assert len(arts) == 1
    assert arts[0].kind == "final"


def test_scan_frames(tmp_path: Path):
    _build_working_dir(tmp_path)
    arts = scan(tmp_path, kind="frames")
    kinds = {a.kind for a in arts}
    assert kinds == {"frames"}
    assert any("character_portraits" in a.path for a in arts)
    assert any("first_frame.png" in a.path for a in arts)


def test_scan_missing_dir(tmp_path: Path):
    assert scan(tmp_path / "nope") == []
