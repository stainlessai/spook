"""The human's tags on tasks: single words, private like notes (never sent to the models).

Stored per Mission (task ids are per Mission); suggestions come from every Mission so
the same tags get reused.
"""

from __future__ import annotations

import re
from collections import Counter

from . import missions
from .paths import file_lock, read_json, write_json

TAG_RE = re.compile(r"^[\w][\w.-]{0,31}$")


def _path(mission: str | None = None):
    return missions.mission_dir(mission) / "task_tags.json"


def load(mission: str | None = None) -> dict[str, list[str]]:
    """task id -> [tag, ...]"""
    return read_json(_path(mission), {})


def for_task(task_id: str, mission: str | None = None) -> list[str]:
    return load(mission).get(task_id, [])


def all_tags() -> Counter:
    """tag -> how many tasks use it, across every Mission."""
    c: Counter = Counter()
    for mid in missions.all_missions():
        for tags in load(mid).values():
            c.update(tags)
    return c


def canonical(tag: str) -> str | None:
    """The tag as it should be stored: an existing tag's spelling if one matches
    (ignoring case), else the word itself. None if it isn't a single word."""
    tag = tag.strip().lstrip("#")
    if not TAG_RE.match(tag):
        return None
    return next((t for t in all_tags() if t.lower() == tag.lower()), tag)


def add(task_id: str, tag: str) -> str | None:
    tag = canonical(tag)
    if not tag:
        return None
    with file_lock(missions.mission_dir() / "tags.lock"):
        tags = load()
        cur = tags.setdefault(task_id, [])
        if tag.lower() not in {t.lower() for t in cur}:
            cur.append(tag)
        write_json(_path(), tags)
    return tag


def remove(task_id: str, tag: str) -> None:
    with file_lock(missions.mission_dir() / "tags.lock"):
        tags = load()
        tags[task_id] = [t for t in tags.get(task_id, []) if t != tag]
        if not tags[task_id]:
            tags.pop(task_id)
        write_json(_path(), tags)
