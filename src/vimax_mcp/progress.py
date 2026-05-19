"""Job progress inference from working_dir file existence.

Shared by MCP tool layer (server.py) and REST handlers (rest.py). The
stage list mirrors the pipeline's filesystem checkpoints — see
proposal §4.1 'Pipeline is already checkpoint-friendly'.
"""

from __future__ import annotations

from pathlib import Path

_PROGRESS_STAGES = [
    ("story.txt", "develop_story"),
    ("characters.json", "extract_characters"),
    ("character_portraits_registry.json", "generate_portraits"),
    ("script.json", "write_script"),
    ("scene_0/storyboard.json", "design_storyboard"),
    ("scene_0/camera_tree.json", "construct_camera_tree"),
    ("scene_0/final_video.mp4", "render_shots"),
    ("final_video.mp4", "concat"),
]


def infer_progress(working_dir: Path) -> dict:
    current = "pending"
    completed: list[str] = []
    for rel, label in _PROGRESS_STAGES:
        if (working_dir / rel).exists():
            completed.append(label)
            current = label
    return {"current_stage": current, "completed_stages": completed}
