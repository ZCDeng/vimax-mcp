"""Walk a job's working_dir and classify ViMax pipeline output.

Categories follow proposal §2.2's `kind` filter:
  - final: the user-facing final_video.mp4 at job root
  - frames: character_portraits + per-shot first/last frames
  - intermediate: json files (story / characters / storyboard / shot descriptions)
                  + per-shot video clips + transitions
  - all: everything
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Literal

ArtifactKind = Literal["final", "frames", "intermediate", "all"]

_FRAME_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
_VIDEO_SUFFIXES = (".mp4", ".mov", ".webm")
_INTERMEDIATE_NAMES = {
    "story.txt",
    "characters.json",
    "character_portraits_registry.json",
    "script.json",
    "storyboard.json",
    "camera_tree.json",
    "shot_description.json",
    "first_frame_selector_output.json",
    "last_frame_selector_output.json",
}


@dataclass
class Artifact:
    path: str
    kind: str
    size: int
    mtime: float


def _classify(rel_parts: tuple[str, ...], name: str) -> str | None:
    suffix = Path(name).suffix.lower()
    if name == "final_video.mp4" and len(rel_parts) == 1:
        return "final"
    if name == "meta.json" and len(rel_parts) == 1:
        return None
    if "character_portraits" in rel_parts and suffix in _FRAME_SUFFIXES:
        return "frames"
    if name in ("first_frame.png", "last_frame.png"):
        return "frames"
    if name.startswith("new_camera") and suffix in _FRAME_SUFFIXES:
        return "frames"
    if suffix in _VIDEO_SUFFIXES:
        return "intermediate"
    if name in _INTERMEDIATE_NAMES or suffix == ".json":
        return "intermediate"
    if name == "pipeline.log":
        return "intermediate"
    return None


def scan(working_dir: Path, kind: ArtifactKind = "all") -> list[Artifact]:
    working_dir = Path(working_dir)
    if not working_dir.is_dir():
        return []
    out: list[Artifact] = []
    for path in working_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(working_dir)
        category = _classify(rel.parts, path.name)
        if category is None:
            continue
        if kind != "all" and kind != category:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append(
            Artifact(
                path=str(path),
                kind=category,
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
        )
    out.sort(key=lambda a: (a.kind != "final", a.path))
    return out


def to_dicts(arts: list[Artifact]) -> list[dict]:
    return [asdict(a) for a in arts]
