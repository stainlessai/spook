"""Ingest new conversation, update per-agent records, rebuild the task graph.

Herdr may fire many hook processes at once. Each one enqueues its pane and then
tries to become the single worker; whoever holds the lock drains the queue.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from . import activity, herdr
from .analyze import update_agent_record, update_graph
from .paths import file_lock, load_config, log, read_json, state_dir, write_json
from .transcript import digest_lines, find_transcript, read_new

FINISHED = {"idle", "done", "blocked"}


# ---------------------------------------------------------------- tracking

def tracked() -> dict[str, str]:
    """workspace_id -> label at the time it was tracked."""
    return read_json(state_dir() / "tracked.json", {})


def set_tracked(workspace_id: str, label: str, on: bool) -> None:
    with file_lock(state_dir() / "tracked.lock"):
        t = tracked()
        if on:
            t[workspace_id] = label
        else:
            t.pop(workspace_id, None)
        write_json(state_dir() / "tracked.json", t)


def is_tracked(workspace_id: str, t: dict[str, str]) -> bool:
    return workspace_id in t


def migrate_renumbered(workspaces: list[dict]) -> dict[str, str]:
    """If herdr renumbered a tracked workspace after a restore, follow it by label.

    Only when the old id no longer exists and exactly one live workspace has that label.
    """
    t = tracked()
    live = {w["workspace_id"]: (w.get("label") or w["workspace_id"]) for w in workspaces}
    for old_id, label in list(t.items()):
        if old_id in live:
            continue
        matches = [wid for wid, lab in live.items() if lab == label and wid not in t]
        if len(matches) == 1:
            log(f"workspace '{label}' moved {old_id} -> {matches[0]}")
            set_tracked(old_id, label, False)
            set_tracked(matches[0], label, True)
    return tracked()


# ---------------------------------------------------------------- queue

def enqueue(item: str) -> None:
    with file_lock(state_dir() / "queue.lock"):
        q = read_json(state_dir() / "queue.json", [])
        if item not in q:
            q.append(item)
        write_json(state_dir() / "queue.json", q)


def drain() -> list[str]:
    with file_lock(state_dir() / "queue.lock"):
        q = read_json(state_dir() / "queue.json", [])
        write_json(state_dir() / "queue.json", [])
    return q


def set_status(**kw) -> None:
    p = state_dir() / "status.json"
    s = read_json(p, {})
    s.update(kw)
    write_json(p, s)


def run_worker() -> bool:
    """Drain the queue if no other worker is running. Returns False if one was."""
    debounce = load_config()["debounce_seconds"]
    for _ in range(3):
        with file_lock(state_dir() / "worker.lock", blocking=False) as got:
            if not got:
                return False
            set_status(running=True, started_at=time.time(), phase="starting", error=None)
            try:
                while True:
                    time.sleep(debounce)
                    items = drain()
                    if not items:
                        break
                    process(items)
                set_status(running=False, last_run=time.time(), phase="idle")
            except Exception as e:  # Keep the board usable and say what broke.
                log(f"worker error: {e!r}")
                set_status(running=False, last_run=time.time(), phase="error", error=str(e))
        # An event may have arrived between our last drain and releasing the lock.
        if not read_json(state_dir() / "queue.json", []):
            break
    return True


# ---------------------------------------------------------------- agents

def _agent_path(session_id: str) -> Path:
    return state_dir() / "agents" / f"{session_id}.json"


def _alias(session_id: str) -> str:
    p = state_dir() / "aliases.json"
    a = read_json(p, {})
    if session_id not in a:
        a[session_id] = f"A{len(a) + 1}"
        write_json(p, a)
    return a[session_id]


@lru_cache(maxsize=256)
def _repo(cwd: str) -> tuple[str, str]:
    try:
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        top = ""
    if not top:
        return Path(cwd).name, ""
    rel = str(Path(cwd).relative_to(top)) if cwd.startswith(top) else ""
    return Path(top).name, "" if rel == "." else rel


def _chunks(lines: list[str], budget: int) -> list[str]:
    out, cur, size = [], [], 0
    for line in lines:
        if size + len(line) > budget and cur:
            out.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line[:budget])
        size += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def update_agent(a: dict, ws_labels: dict[str, str]) -> bool:
    """Bring one agent's record up to date. Returns True if its record changed."""
    cfg = load_config()
    sid = a["agent_session"]["value"]
    path = _agent_path(sid)
    rec = read_json(path, {})
    cwd = a.get("cwd") or a.get("foreground_cwd") or ""
    repo, rel = _repo(cwd) if cwd else ("?", "")
    rec.update(
        session_id=sid,
        alias=_alias(sid),
        pane_id=a["pane_id"],
        workspace_id=a["workspace_id"],
        workspace=ws_labels.get(a["workspace_id"], a["workspace_id"]),
        cwd=cwd,
        repo=repo,
        path_in_repo=rel,
        agent_status=a.get("agent_status"),
        terminal_title=a.get("terminal_title_stripped"),
    )
    launched = read_json(state_dir() / "launches.json", {}).get(a["pane_id"])
    if launched:
        rec["launched_for"] = launched["task_id"]
    tpath = find_transcript(sid, cwd)
    if not tpath:
        rec["error"] = "transcript not found"
        write_json(path, rec)
        return False
    rec["transcript"] = str(tpath)

    lines, new_offset = read_new(tpath, rec.get("offset", 0))
    digest, title = digest_lines(lines)
    if title:
        rec["ai_title"] = title
    if not digest:
        rec["offset"] = new_offset
        write_json(path, rec)
        return False

    if "first_ask" not in rec:
        first = next((d for d in digest if d.startswith("\n## USER")), None)
        rec["first_ask"] = first.split("\n", 2)[-1] if first else None

    chunks = _chunks(digest, cfg["digest_budget_chars"])
    if not rec.get("record"):
        skipped = max(0, len(chunks) - cfg["max_backfill_chunks"])
        if skipped:
            log(f"{rec['alias']} backfill: skipping {skipped} older chunks")
        chunks = chunks[skipped:]

    meta = {k: rec.get(k) for k in ("alias", "workspace", "repo", "path_in_repo", "cwd", "ai_title")}
    for i, chunk in enumerate(chunks, 1):
        what = f"{rec['alias']} {rec['workspace']}" + (f" (part {i}/{len(chunks)})" if len(chunks) > 1 else "")
        set_status(phase=f"reading {what}")
        rec["record"] = update_agent_record(rec.get("record"), meta, chunk, rec.get("first_ask"), what)
        rec["updated_at"] = time.time()
        write_json(path, rec)
    rec["offset"] = new_offset
    rec.pop("error", None)
    write_json(path, rec)
    log(f"{rec['alias']} updated from {len(digest)} digest lines")
    return True


def tracked_records() -> list[dict]:
    t = tracked()
    out = []
    for p in sorted((state_dir() / "agents").glob("*.json")):
        r = read_json(p, {})
        if r.get("record") and is_tracked(r.get("workspace_id", ""), t) and not r.get("gone"):
            out.append(r)
    return out


# ---------------------------------------------------------------- main step

def load_trash() -> dict[str, dict]:
    """task id -> {"task": snapshot, "trashed_at": ts}"""
    return read_json(state_dir() / "trash.json", {})


def save_trash(trash: dict[str, dict]) -> None:
    write_json(state_dir() / "trash.json", trash)


def trash_task(task_id: str) -> bool:
    with file_lock(state_dir() / "graph.lock"):
        g = read_json(state_dir() / "graph.json", {})
        task = next((t for t in g.get("tasks", []) if t["id"] == task_id), None)
        if not task:
            return False
        trash = load_trash()
        trash[task_id] = {"task": task, "trashed_at": time.time()}
        save_trash(trash)
        g["tasks"] = [t for t in g["tasks"] if t["id"] != task_id]
        write_json(state_dir() / "graph.json", g)
    return True


def restore_task(task_id: str) -> bool:
    with file_lock(state_dir() / "graph.lock"):
        trash = load_trash()
        entry = trash.pop(task_id, None)
        if not entry:
            return False
        g = read_json(state_dir() / "graph.json", {})
        g.setdefault("tasks", []).append(entry["task"])
        write_json(state_dir() / "graph.json", g)
        save_trash(trash)
    return True


def delete_forever(task_id: str) -> bool:
    with file_lock(state_dir() / "graph.lock"):
        trash = load_trash()
        if trash.pop(task_id, None) is None:
            return False
        save_trash(trash)
    return True


def load_overrides() -> dict[str, dict]:
    """task id -> {"status", "prev", "at"}: statuses the human set by hand."""
    return read_json(state_dir() / "status_overrides.json", {})


def toggle_done(task_id: str) -> str | None:
    """Mark a task done by hand, or reopen it. Returns the new status."""
    with file_lock(state_dir() / "graph.lock"):
        g = read_json(state_dir() / "graph.json", {})
        task = next((t for t in g.get("tasks", []) if t["id"] == task_id), None)
        if not task:
            return None
        ov = load_overrides()
        if task["status"] in ("done", "dropped"):
            mine = ov.get(task_id)
            if mine and mine["status"] == "done":
                ov.pop(task_id)  # Undo my own close: back to what it was.
                task["status"] = mine.get("prev") or "open"
            else:
                ov[task_id] = {"status": "open", "prev": task["status"], "at": time.time()}
                task["status"] = "open"
        else:
            ov[task_id] = {"status": "done", "prev": task["status"], "at": time.time()}
            task["status"] = "done"
        write_json(state_dir() / "status_overrides.json", ov)
        write_json(state_dir() / "graph.json", g)
        return task["status"]


def board_panes() -> set[str]:
    return set(read_json(state_dir() / "board_panes.json", {}))


def process(items: list[str]) -> None:
    activity.record("sync", items=items)
    activity.trim()
    workspaces = herdr.workspaces()
    t = migrate_renumbered(workspaces)
    ws_labels = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in workspaces}
    live = [
        a for a in herdr.agents()
        if (a.get("agent_session") or {}).get("value") and is_tracked(a["workspace_id"], t)
        and a["pane_id"] not in board_panes()
    ]
    everything = "*" in items
    todo = [a for a in live if everything or a["pane_id"] in items]

    for a in todo:
        try:
            update_agent(a, ws_labels)
        except Exception as e:
            log(f"agent {a['pane_id']} failed: {e!r}")
            set_status(error=f"{a['pane_id']}: {e}")

    # Mark agents whose panes are gone so they drop out of the graph.
    live_ids = {a["agent_session"]["value"] for a in herdr.agents() if a.get("agent_session")}
    for p in (state_dir() / "agents").glob("*.json"):
        r = read_json(p, {})
        gone = r.get("session_id") not in live_ids
        if r.get("gone") != gone:
            r["gone"] = gone
            write_json(p, r)

    rebuild_graph()


def _fingerprint(records: list[dict]) -> str:
    key = [(r["session_id"], r.get("updated_at")) for r in records]
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()


def rebuild_graph(force: bool = False) -> None:
    records = tracked_records()
    gpath = state_dir() / "graph.json"
    prev = read_json(gpath, {})
    fp = _fingerprint(records)
    if not force and prev.get("fingerprint") == fp:
        return
    if not records:
        write_json(gpath, {"concerns": [], "tasks": [], "alerts": [], "fingerprint": fp, "updated_at": time.time()})
        return
    set_status(phase=f"rebuilding task graph from {len(records)} agents")
    agents_in = [
        {
            "id": r["alias"],
            "workspace": r["workspace"],
            "repo": r["repo"],
            "path_in_repo": r["path_in_repo"],
            "herdr_status": r.get("agent_status"),
            "launched_for_task": r.get("launched_for"),
            **r["record"],
        }
        for r in records
    ]
    previous = {k: prev[k] for k in ("concerns", "tasks", "alerts") if k in prev} or None
    trashed = [{"id": tid, "title": e["task"]["title"], "status": e["task"]["status"]}
               for tid, e in load_trash().items()]
    overrides = [
        {"id": tid, "status": o["status"], "set_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(o["at"]))}
        for tid, o in load_overrides().items()
    ]
    graph = update_graph(agents_in, previous, trashed, overrides)
    graph.update(fingerprint=fp, updated_at=time.time())
    with file_lock(state_dir() / "graph.lock"):
        # The model call takes a while; apply trash/restore done meanwhile.
        trash = load_trash()
        kept = []
        for t in graph["tasks"]:
            if t["id"] in trash:  # Stays off the board; keep its trash copy current.
                trash[t["id"]]["task"] = t
            else:
                kept.append(t)
        out_ids = {t["id"] for t in kept}
        prev_ids = {t["id"] for t in prev.get("tasks", [])}
        for t in read_json(gpath, {}).get("tasks", []):
            if t["id"] not in prev_ids and t["id"] not in out_ids and t["id"] not in trash:
                kept.append(t)  # Restored from trash during the call.
        # The human's status decisions win; the model flags conflicts as alerts instead.
        ov = load_overrides()
        for t in kept:
            if t["id"] in ov:
                t["status"] = ov[t["id"]]["status"]
        graph["tasks"] = kept
        save_trash(trash)
        write_json(gpath, graph)

    log(f"graph: {len(graph['tasks'])} tasks, {len(graph['concerns'])} concerns, {len(graph['alerts'])} alerts")
