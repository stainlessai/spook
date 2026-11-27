"""The human's private notes on tasks.

Notes are never sent to spook's analysis models. They reach agents only through
task briefs: when an agent is started for a task, and when `g` jumps to an
agent that hasn't been given a task's latest notes yet.
"""

from __future__ import annotations

import time
import uuid

from . import missions
from .paths import file_lock, read_json, write_json


def _path(mission: str | None = None):
    return missions.mission_dir(mission) / "task_notes.json"


def load(mission: str | None = None) -> dict[str, list[dict]]:
    """task id -> [{"id", "text", "at"}], oldest first."""
    return read_json(_path(mission), {})


def for_task(task_id: str, mission: str | None = None) -> list[dict]:
    return load(mission).get(task_id, [])


def add(task_id: str, text: str) -> None:
    with file_lock(missions.mission_dir() / "notes.lock"):
        notes = load()
        notes.setdefault(task_id, []).append({"id": uuid.uuid4().hex[:8], "text": text.strip(), "at": time.time()})
        write_json(_path(), notes)


def delete(task_id: str, note_id: str) -> None:
    with file_lock(missions.mission_dir() / "notes.lock"):
        notes = load()
        notes[task_id] = [n for n in notes.get(task_id, []) if n["id"] != note_id]
        if not notes[task_id]:
            notes.pop(task_id)
        write_json(_path(), notes)


# ---------------------------------------------------------------- delivery to agents

def _delivered_path():
    return missions.mission_dir() / "notes_delivered.json"


def undelivered(pane_id: str, task_ids: list[str]) -> dict[str, int]:
    """task id -> number of notes this pane hasn't been pointed at yet."""
    seen = read_json(_delivered_path(), {}).get(pane_id, {})
    out = {}
    for tid in task_ids:
        new = [n for n in for_task(tid) if n["at"] > seen.get(tid, 0)]
        if new:
            out[tid] = len(new)
    return out


def mark_delivered(pane_id: str, task_ids: list[str]) -> None:
    with file_lock(missions.mission_dir() / "notes.lock"):
        d = read_json(_delivered_path(), {})
        for tid in task_ids:
            latest = max((n["at"] for n in for_task(tid)), default=0)
            if latest:
                d.setdefault(pane_id, {})[tid] = latest
        write_json(_delivered_path(), d)
