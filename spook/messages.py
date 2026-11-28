"""Messages between agents, relayed by the human.

An agent queues a message with `spook tell A4 "..."`. Spook never sends it on its own:
the board shows it, and `g` on the recipient types it into its input box (not sent).
Messages are never given to spook's analysis models.

missions/<id>/messages.json: [{"id", "from", "to", "task", "text", "at", "delivered_at"}]
"""

from __future__ import annotations

import time
import uuid

from . import missions
from .paths import file_lock, read_json, write_json

MAX_CHARS = 1500


def _path(mission: str | None = None):
    return missions.mission_dir(mission) / "messages.json"


def load(mission: str | None = None) -> list[dict]:
    return read_json(_path(mission), [])


def pending(mission: str | None = None, to: str | None = None) -> list[dict]:
    return [m for m in load(mission) if not m.get("delivered_at") and (to is None or m["to"] == to)]


def add(mission: str, sender: str, to: str, text: str, task: str | None = None) -> dict:
    # One line: typed into an input box, a newline could submit it.
    text = " ".join(text.split())[:MAX_CHARS]
    msg = {"id": uuid.uuid4().hex[:8], "from": sender, "to": to, "task": task, "text": text,
           "at": time.time(), "delivered_at": None}
    with file_lock(missions.mission_dir(mission) / "messages.lock"):
        msgs = load(mission)
        msgs.append(msg)
        write_json(_path(mission), msgs)
    return msg


def mark_delivered(ids: list[str], mission: str | None = None) -> None:
    if not ids:
        return
    with file_lock(missions.mission_dir(mission) / "messages.lock"):
        msgs = load(mission)
        for m in msgs:
            if m["id"] in ids and not m.get("delivered_at"):
                m["delivered_at"] = time.time()
        write_json(_path(mission), msgs)


def discard(msg_id: str, mission: str | None = None) -> None:
    """Drop a message without delivering it."""
    with file_lock(missions.mission_dir(mission) / "messages.lock"):
        write_json(_path(mission), [m for m in load(mission) if m["id"] != msg_id])


def as_prompt(msgs: list[dict]) -> str:
    """The messages as one line to append to a typed prompt."""
    if not msgs:
        return ""
    parts = [f"{m['from']}{' about ' + m['task'] if m.get('task') else ''}: \"{m['text']}\"" for m in msgs]
    return (" Messages from other agents, relayed through spook: " + "; ".join(parts) + "."
            " Reply with `spook tell <agent> \"...\"` if they need an answer.")
