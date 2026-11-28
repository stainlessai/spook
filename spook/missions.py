"""Missions: named, scoped groupings of agents, each with its own task graph.

An agent (a Claude session) can be on any number of Missions. Agent summaries are
shared; each Mission keeps its own graph and the human's decisions about it in
missions/<id>/.

Mission ids are M1, M2, ... in creation order, never reused (missions.json keeps the counter).

missions.json:  {"active": id, "missions": {id: {"name", "scope", "created_at"}}}
missions/<id>/agents.json:  {session_id: {"pane_id", "workspace", "title", "added_at"}}
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from .paths import file_lock, log, read_json, state_dir, write_json

# Files that belong to one Mission (they lived at the state root before Missions).
PER_MISSION = [
    "graph.json", "trash.json", "status_overrides.json", "hidden_tasks.json",
    "task_notes.json", "notes_delivered.json", "launches.json",
]


def _registry_path() -> Path:
    return state_dir() / "missions.json"


def _lock():
    return file_lock(state_dir() / "missions.lock")


ID_RE = re.compile(r"^M[1-9]\d*$")


def load_registry() -> dict:
    reg = read_json(_registry_path(), None)
    if reg is None or "next_id" not in reg:
        with file_lock(state_dir() / "migrate.lock"):  # Hooks and the board may start together.
            reg = read_json(_registry_path(), None) or migrate_legacy()
            if "next_id" not in reg:
                reg = _renumber(reg)
    return reg


def _take_id(reg: dict) -> str:
    """The next Mission id (M1, M2, ...). Ids are never reused, even after a delete."""
    n = reg.get("next_id", 1)
    reg["next_id"] = n + 1
    return f"M{n}"


def _renumber(reg: dict) -> dict:
    """Give Missions from before sequential ids M1, M2, ... in creation order. Each old
    folder name stays as a link, so brief paths already handed to agents keep working."""
    root = state_dir() / "missions"
    old_missions = reg["missions"]
    order = sorted(old_missions, key=lambda m: old_missions[m].get("created_at", 0))
    new = {"active": reg.get("active"), "missions": {}, "next_id": 1}
    renamed = {}
    for old in order:
        mid = _take_id(new)
        new["missions"][mid] = old_missions[old]
        renamed[old] = mid
        if old == mid:
            continue
        src, dst = root / old, root / mid
        if src.exists() and not src.is_symlink() and not dst.exists():
            shutil.move(str(src), str(dst))
            os.symlink(mid, src)
        log(f"mission id {old} -> {mid}")
    # Links left by an earlier rename point at old folder names: repoint them.
    if root.exists():
        for p in root.iterdir():
            if p.is_symlink() and os.readlink(p) in renamed and p.name not in renamed.values():
                target = renamed[os.readlink(p)]
                p.unlink()
                os.symlink(target, p)
    new["active"] = renamed.get(new["active"], next(iter(new["missions"]), None))
    _save_registry(new)
    return new


def _save_registry(reg: dict) -> None:
    write_json(_registry_path(), reg)


def all_missions() -> dict[str, dict]:
    return load_registry()["missions"]


def active() -> str:
    return load_registry()["active"]


def info(mission: str | None = None) -> dict:
    mid = mission or active()
    return {"id": mid, **all_missions().get(mid, {"name": mid, "scope": ""})}


def mission_dir(mission: str | None = None) -> Path:
    reg = load_registry()  # Also runs the one-time migration on first use.
    d = state_dir() / "missions" / (mission or reg["active"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def find(mission_id: str) -> str | None:
    """The Mission with this id (M1, M2, ...; any case), or None. Names aren't accepted:
    they can be renamed and needn't be unique."""
    mid = mission_id.strip().upper()
    return mid if mid in all_missions() else None


def set_active(mission: str) -> None:
    with _lock():
        reg = load_registry()
        if mission in reg["missions"]:
            reg["active"] = mission
            _save_registry(reg)


def create(name: str, scope: str = "") -> str:
    with _lock():
        reg = load_registry()
        mid = _take_id(reg)
        reg["missions"][mid] = {"name": name.strip(), "scope": scope.strip(), "created_at": time.time()}
        reg["active"] = mid
        _save_registry(reg)
    mission_dir(mid)
    return mid


def update(mission: str, name: str | None = None, scope: str | None = None) -> None:
    with _lock():
        reg = load_registry()
        m = reg["missions"][mission]
        if name is not None and name.strip():
            m["name"] = name.strip()
        if scope is not None:
            m["scope"] = scope.strip()
        _save_registry(reg)


def delete(mission: str) -> bool:
    """Move the Mission's folder aside (recoverable by hand). The last Mission can't go."""
    with _lock():
        reg = load_registry()
        if mission not in reg["missions"] or len(reg["missions"]) == 1:
            return False
        src = state_dir() / "missions" / mission
        if src.exists():
            dst = state_dir() / "missions" / "_deleted" / f"{mission}-{int(time.time())}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        reg["missions"].pop(mission)
        if reg["active"] == mission:
            reg["active"] = next(iter(reg["missions"]))
        _save_registry(reg)
    log(f"mission {mission} deleted (moved to missions/_deleted)")
    return True


# ---------------------------------------------------------------- membership

def members(mission: str | None = None) -> dict[str, dict]:
    return read_json(mission_dir(mission) / "agents.json", {})


def _save_members(mission: str, m: dict) -> None:
    write_json(mission_dir(mission) / "agents.json", m)


def add_agent(mission: str, agent: dict) -> None:
    """agent: a herdr agent-list entry (needs agent_session.value)."""
    sid = (agent.get("agent_session") or {}).get("value")
    if not sid:
        return
    with _lock():
        m = members(mission)
        m.setdefault(sid, {"added_at": time.time()})
        m[sid].update(pane_id=agent.get("pane_id"), workspace=agent.get("workspace_id"),
                      title=agent.get("terminal_title_stripped"))
        _save_members(mission, m)


def add_pending_pane(mission: str, pane_id: str) -> None:
    """For an agent we just started whose session id herdr hasn't reported yet."""
    with _lock():
        m = members(mission)
        m[f"pane:{pane_id}"] = {"pane_id": pane_id, "pending": True, "added_at": time.time()}
        _save_members(mission, m)


def remove_agent(mission: str, session_id: str) -> None:
    with _lock():
        m = members(mission)
        m.pop(session_id, None)
        _save_members(mission, m)


def missions_for_session(session_id: str) -> list[str]:
    return [mid for mid in all_missions() if session_id in members(mid)]


def all_member_sessions() -> set[str]:
    return {sid for mid in all_missions() for sid in members(mid) if not sid.startswith("pane:")}


def all_member_panes() -> set[str]:
    return {m.get("pane_id") for mid in all_missions() for m in members(mid).values() if m.get("pane_id")}


def follow_sessions(live: list[dict]) -> None:
    """Keep members attached to their agents across /clear, restarts and pending launches.

    A member whose session is no longer live, in a pane that now runs a different
    session, moves to that session and keeps its alias.
    """
    from . import activity  # local: avoid an import cycle

    by_pane = {a["pane_id"]: a for a in live if (a.get("agent_session") or {}).get("value")}
    live_sids = {a["agent_session"]["value"] for a in by_pane.values()}
    aliases_path = state_dir() / "aliases.json"
    for mid in all_missions():
        with _lock():
            m = members(mid)
            changed = False
            for sid, entry in list(m.items()):
                a = by_pane.get(entry.get("pane_id"))
                if sid in live_sids:
                    # Refresh where it lives now.
                    cur = next(x for x in by_pane.values() if x["agent_session"]["value"] == sid)
                    if entry.get("pane_id") != cur["pane_id"]:
                        entry["pane_id"] = cur["pane_id"]
                        changed = True
                    continue
                if not a:
                    continue
                new_sid = a["agent_session"]["value"]
                if new_sid in m:
                    continue
                m.pop(sid)
                m[new_sid] = {**entry, "pending": False, "pane_id": a["pane_id"],
                              "workspace": a.get("workspace_id"), "title": a.get("terminal_title_stripped")}
                m[new_sid].pop("pending", None)
                if not sid.startswith("pane:"):
                    aliases = read_json(aliases_path, {})
                    if sid in aliases and new_sid not in aliases:
                        aliases[new_sid] = aliases[sid]
                        write_json(aliases_path, aliases)
                    activity.record("follow", mission=mid, old=sid, new=new_sid, pane=a["pane_id"])
                    log(f"mission {mid}: pane {a['pane_id']} session {sid[:8]} -> {new_sid[:8]}")
                changed = True
            if changed:
                _save_members(mid, m)


# ---------------------------------------------------------------- migration

def migrate_legacy() -> dict:
    """First run with Missions: turn the old single board into Mission "Default"."""
    root = state_dir()
    reg = {"missions": {}, "next_id": 1}
    mid = _take_id(reg)
    reg["active"] = mid
    reg["missions"][mid] = {"name": "Default", "scope": "", "created_at": time.time()}
    dest = root / "missions" / mid
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in PER_MISSION:
        src = root / name
        if src.exists() and not (dest / name).exists():
            shutil.move(str(src), str(dest / name))
            moved.append(name)
    if (root / "briefs").exists() and not (dest / "briefs").exists():
        shutil.move(str(root / "briefs"), str(dest / "briefs"))
        moved.append("briefs/")
    # Seed members with the agents of the workspaces that used to be tracked.
    tracked = read_json(root / "tracked.json", {})
    seeded = {}
    for p in sorted((root / "agents").glob("*.json")):
        r = read_json(p, {})
        if r.get("session_id") and r.get("workspace_id") in tracked:
            seeded[r["session_id"]] = {"pane_id": r.get("pane_id"), "workspace": r.get("workspace_id"),
                                       "title": r.get("terminal_title"), "added_at": time.time()}
    if not (dest / "agents.json").exists():
        write_json(dest / "agents.json", seeded)
    if (root / "tracked.json").exists():
        shutil.move(str(root / "tracked.json"), str(dest / "tracked.legacy.json"))
    write_json(root / "missions.json", reg)
    if moved or seeded:
        log(f"migrated to Missions: moved {moved}, seeded {len(seeded)} agents into Mission 'Default' ({mid})")
    return reg
