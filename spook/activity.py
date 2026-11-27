"""Append-only activity log of background work, for the board's Activity tab.

Each line is one event:
  {"ev": "call_start", "id", "ts", "label", "model", "pid", "chars"}
  {"ev": "call_end",   "id", "ts", "ok", "duration_ms", "cost", "error"}
  {"ev": "sync",       "ts", "trigger", "items"}
"""

from __future__ import annotations

import json
import os
import time
import uuid

from .paths import file_lock, state_dir

KEEP_LINES = 2000


def _path():
    return state_dir() / "activity.jsonl"


def record(ev: str, **fields) -> str:
    fields.setdefault("id", uuid.uuid4().hex[:8])
    line = json.dumps({"ev": ev, "ts": time.time(), **fields}, ensure_ascii=False)
    with file_lock(state_dir() / "activity.lock"):
        with _path().open("a") as f:
            f.write(line + "\n")
    return fields["id"]


def trim() -> None:
    p = _path()
    if not p.exists():
        return
    with file_lock(state_dir() / "activity.lock"):
        lines = p.read_text().splitlines()
        if len(lines) > KEEP_LINES:
            p.write_text("\n".join(lines[-KEEP_LINES:]) + "\n")


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def load(limit: int = 300) -> tuple[list[dict], list[dict]]:
    """Return (calls newest first, syncs newest first)."""
    p = _path()
    if not p.exists():
        return [], []
    calls: dict[str, dict] = {}
    syncs: list[dict] = []
    for raw in p.read_text().splitlines()[-KEEP_LINES:]:
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if e["ev"] == "call_start":
            calls[e["id"]] = {**e, "state": "running"}
        elif e["ev"] == "call_end" and e["id"] in calls:
            c = calls[e["id"]]
            c.update(state="done" if e.get("ok") else "error", ended=e["ts"],
                     duration_ms=e.get("duration_ms"), cost=e.get("cost"), error=e.get("error"))
        elif e["ev"] == "sync":
            syncs.append(e)
    for c in calls.values():
        # A worker killed mid-call never writes call_end.
        if c["state"] == "running" and not _alive(c.get("pid")):
            c["state"] = "lost"
    out = sorted(calls.values(), key=lambda c: c["ts"], reverse=True)[:limit]
    return out, syncs[::-1][:limit]
