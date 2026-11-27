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

from . import activity, herdr, missions
from .analyze import update_agent_record, update_graph
from .paths import file_lock, load_config, log, read_json, state_dir, write_json
from .transcript import digest_lines, find_transcript, read_new

FINISHED = {"idle", "done", "blocked"}


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


def mission_records(mission: str | None = None) -> list[dict]:
    """Records of this Mission's agents, including ones whose pane has closed."""
    mem = missions.members(mission)
    out = []
    for sid in mem:
        r = read_json(_agent_path(sid), {})
        if r.get("record"):
            out.append(r)
    return sorted(out, key=lambda r: int(r["alias"][1:]))


# ---------------------------------------------------------------- the human's decisions (per Mission)

def _md(mission: str | None):
    return missions.mission_dir(mission)


def load_trash(mission: str | None = None) -> dict[str, dict]:
    """task id -> {"task": snapshot, "trashed_at": ts}"""
    return read_json(_md(mission) / "trash.json", {})


def save_trash(trash: dict[str, dict], mission: str | None = None) -> None:
    write_json(_md(mission) / "trash.json", trash)


def trash_task(task_id: str, mission: str | None = None) -> bool:
    d = _md(mission)
    with file_lock(d / "graph.lock"):
        g = read_json(d / "graph.json", {})
        task = next((t for t in g.get("tasks", []) if t["id"] == task_id), None)
        if not task:
            return False
        trash = load_trash(mission)
        trash[task_id] = {"task": task, "trashed_at": time.time()}
        save_trash(trash, mission)
        g["tasks"] = [t for t in g["tasks"] if t["id"] != task_id]
        write_json(d / "graph.json", g)
    return True


def restore_task(task_id: str, mission: str | None = None) -> bool:
    d = _md(mission)
    with file_lock(d / "graph.lock"):
        trash = load_trash(mission)
        entry = trash.pop(task_id, None)
        if not entry:
            return False
        g = read_json(d / "graph.json", {})
        g.setdefault("tasks", []).append(entry["task"])
        write_json(d / "graph.json", g)
        save_trash(trash, mission)
    return True


def delete_forever(task_id: str, mission: str | None = None) -> bool:
    d = _md(mission)
    with file_lock(d / "graph.lock"):
        trash = load_trash(mission)
        if trash.pop(task_id, None) is None:
            return False
        save_trash(trash, mission)
    return True


def load_overrides(mission: str | None = None) -> dict[str, dict]:
    """task id -> {"status", "prev", "at"}: statuses the human set by hand."""
    return read_json(_md(mission) / "status_overrides.json", {})


def toggle_done(task_id: str, mission: str | None = None) -> str | None:
    """Mark a task done by hand, or reopen it. Returns the new status."""
    d = _md(mission)
    with file_lock(d / "graph.lock"):
        g = read_json(d / "graph.json", {})
        task = next((t for t in g.get("tasks", []) if t["id"] == task_id), None)
        if not task:
            return None
        ov = load_overrides(mission)
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
        write_json(d / "status_overrides.json", ov)
        write_json(d / "graph.json", g)
        return task["status"]


def board_panes() -> set[str]:
    return set(read_json(state_dir() / "board_panes.json", {}))


# ---------------------------------------------------------------- main step

def live_agents() -> list[dict]:
    """Claude agents herdr reports, minus spook's own board panes."""
    panes = board_panes()
    return [a for a in herdr.agents()
            if (a.get("agent_session") or {}).get("value") and a["pane_id"] not in panes]


def process(items: list[str]) -> None:
    activity.record("sync", items=items)
    activity.trim()
    ws_labels = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in herdr.workspaces()}
    live_all = live_agents()
    missions.follow_sessions(live_all)
    on_a_mission = missions.all_member_sessions()
    live = [a for a in live_all if a["agent_session"]["value"] in on_a_mission]
    everything = "*" in items
    todo = [a for a in live if everything or a["pane_id"] in items]

    for a in todo:
        try:
            update_agent(a, ws_labels)
        except Exception as e:
            log(f"agent {a['pane_id']} failed: {e!r}")
            set_status(error=f"{a['pane_id']}: {e}")

    # Mark agents whose panes are gone. They stay in their Missions' graphs as "closed".
    live_ids = {a["agent_session"]["value"] for a in live_all}
    for p in (state_dir() / "agents").glob("*.json"):
        r = read_json(p, {})
        gone = r.get("session_id") not in live_ids
        if r.get("gone") != gone:
            r["gone"] = gone
            write_json(p, r)

    # Each Mission rebuilds only if its agents or membership changed (fingerprint).
    for mid in missions.all_missions():
        try:
            rebuild_graph(mid)
        except Exception as e:
            log(f"mission {mid} rebuild failed: {e!r}")
            set_status(error=f"{missions.info(mid)['name']}: {e}")


def _fingerprint(records: list[dict], mission: str) -> str:
    info = missions.info(mission)
    key = [(r["session_id"], r.get("updated_at"), r.get("gone")) for r in records]
    key += [sorted(missions.members(mission)), info.get("scope", "")]
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()


def _split_departed(tasks: list[dict], member_aliases: set[str]) -> tuple[list[dict], list[dict]]:
    """(active, departed): departed = owned only by agents no longer on the Mission."""
    active, departed = [], []
    for t in tasks:
        owners = set(t.get("owners", []))
        if owners and not owners & member_aliases:
            departed.append({**t, "departed": True})
        else:
            t = dict(t)
            t.pop("departed", None)
            active.append(t)
    return active, departed


def rebuild_graph(mission: str | None = None, force: bool = False) -> None:
    mid = mission or missions.active()
    d = _md(mid)
    info = missions.info(mid)
    records = mission_records(mid)
    gpath = d / "graph.json"
    prev = read_json(gpath, {})
    fp = _fingerprint(records, mid)
    if not force and prev.get("fingerprint") == fp:
        return
    member_aliases = {r["alias"] for r in records}
    active_prev, departed = _split_departed(prev.get("tasks", []), member_aliases)
    if not records:
        # Never empty a Mission: keep what's there, just flag tasks whose agents left.
        if prev:
            with file_lock(d / "graph.lock"):
                cur = read_json(gpath, {})
                a2, d2 = _split_departed(cur.get("tasks", []), member_aliases)
                cur.update(tasks=a2 + d2, fingerprint=fp)
                write_json(gpath, cur)
        return
    set_status(phase=f"[{info['name']}] rebuilding task graph from {len(records)} agents")
    launches = read_json(d / "launches.json", {})
    agents_in = [
        {
            "id": r["alias"],
            "workspace": r["workspace"],
            "repo": r["repo"],
            "path_in_repo": r["path_in_repo"],
            "herdr_status": "closed" if r.get("gone") else r.get("agent_status"),
            "launched_for_task": (launches.get(r.get("pane_id")) or {}).get("task_id"),
            **r["record"],
        }
        for r in records
    ]
    previous = {"concerns": prev.get("concerns", []), "tasks": active_prev, "alerts": prev.get("alerts", [])} \
        if prev else None
    trashed = [{"id": tid, "title": e["task"]["title"], "status": e["task"]["status"]}
               for tid, e in load_trash(mid).items()]
    overrides = [
        {"id": tid, "status": o["status"], "set_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(o["at"]))}
        for tid, o in load_overrides(mid).items()
    ]
    reserved = sorted({t["id"] for t in departed})
    graph = update_graph(agents_in, previous, trashed, overrides, info.get("scope", ""), reserved,
                         f"[{info['name']}] task graph from {len(records)} agents")
    graph.update(fingerprint=fp, updated_at=time.time())
    with file_lock(d / "graph.lock"):
        # The model call takes a while; apply trash/restore done meanwhile.
        trash = load_trash(mid)
        kept = []
        for t in graph["tasks"]:
            if t["id"] in trash:  # Stays off the board; keep its trash copy current.
                trash[t["id"]]["task"] = t
            elif t["id"] not in reserved:
                kept.append(t)
        out_ids = {t["id"] for t in kept}
        prev_ids = {t["id"] for t in prev.get("tasks", [])}
        for t in read_json(gpath, {}).get("tasks", []):
            if t["id"] not in prev_ids and t["id"] not in out_ids and t["id"] not in trash:
                kept.append(t)  # Restored from trash during the call.
        # Tasks of agents that left the Mission come back unchanged.
        kept += [t for t in departed if t["id"] not in trash]
        # The human's status decisions win; the model flags conflicts as alerts instead.
        ov = load_overrides(mid)
        for t in kept:
            if t["id"] in ov:
                t["status"] = ov[t["id"]]["status"]
        graph["tasks"] = kept
        save_trash(trash, mid)
        write_json(gpath, graph)

    log(f"[{mid}] graph: {len(graph['tasks'])} tasks ({len(departed)} departed), "
        f"{len(graph['concerns'])} concerns, {len(graph['alerts'])} alerts")
