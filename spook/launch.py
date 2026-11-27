"""Start a new Claude agent for a task, after the user confirms in the board.

The agent gets a brief file in spook's state dir and a pre-filled (not sent)
prompt pointing at it. Spook records the launch so the graph assigns the new
agent as the task's owner.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from . import herdr, missions, notes
from .paths import read_json, state_dir, write_json
from .worker import mission_records


# ---------------------------------------------------------------- where to run it

def _toplevel(path: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def candidate_dirs(task: dict) -> list[tuple[str, str]]:
    """(directory, why) pairs, best guess first, from the task's projects and known agents."""
    roots: dict[str, str] = {}  # repo root -> workspace label
    for r in mission_records():
        top = _toplevel(r["cwd"]) if r.get("cwd") else None
        if top:
            roots.setdefault(top, r["workspace"])
    out: list[tuple[str, str]] = []

    def add(path: Path, why: str):
        p = str(path)
        if path.is_dir() and p not in [o[0] for o in out]:
            out.append((p, why))

    for proj in task.get("projects", []):
        parts = [x for x in proj.strip("/").split("/") if x]
        if not parts:
            continue
        for root in roots:
            name = Path(root).name
            if parts[0] == name:  # "repo" or "repo/sub/path"
                add(Path(root, *parts[1:]), f"project {proj}")
                add(Path(root), f"repo of {proj}")
            add(Path(root, *parts), f"{proj} inside {name}")
            add(Path(root).parent / parts[0], f"sibling repo {parts[0]}")
            # Monorepo: a directory named like the project's last part, two levels down.
            for hit in sorted(Path(root).glob(f"*/{parts[-1]}"))[:2]:
                add(hit, f"{parts[-1]} inside {name}")
    for root, ws in roots.items():
        add(Path(root), f"repo used by '{ws}'")
    return out


def workspace_for(directory: str) -> str | None:
    """A workspace of an agent on the active Mission that works in the same repo, if any."""
    top = _toplevel(directory) or directory
    for r in mission_records():
        if r.get("cwd") and (_toplevel(r["cwd"]) or r["cwd"]) == top:
            return r["workspace_id"]
    return None


# ---------------------------------------------------------------- brief + prompt

def write_brief(task: dict, graph: dict) -> Path:
    by_id = {t["id"]: t for t in graph.get("tasks", [])}
    concern = next((c for c in graph.get("concerns", []) if c["id"] == task.get("concern_id")), None)
    records = {r["alias"]: r for r in mission_records()}
    blocks = [t for t in by_id.values() if task["id"] in t.get("depends_on", [])]
    alerts = [a for a in graph.get("alerts", []) if task["id"] in a.get("task_ids", [])]
    projects = set(task.get("projects", []))

    lines = [
        f"# {task['id']}: {task['title']}",
        "",
        "_Snapshot written by spook, the cross-agent task board. Other agents are working on related",
        "tasks in other directories; this file is how you learn about them._",
        "",
        f"- **Status:** {task['status']}",
        f"- **Projects:** {', '.join(task.get('projects', [])) or '—'}",
        f"- **Concern:** {concern['name'] + ' — ' + concern['description'] if concern else '—'}",
        f"- **Snapshot taken:** {time.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    mine = notes.for_task(task["id"])
    if mine:
        lines += ["## Notes from the human (follow these)"]
        lines += [f"- ({time.strftime('%Y-%m-%d %H:%M', time.localtime(n['at']))}) {n['text']}" for n in mine]
        lines += [""]
    lines += [
        "## What needs doing",
        task.get("detail", ""),
        "",
        "## Why spook thinks so",
        task.get("evidence", ""),
    ]
    if task.get("depends_on"):
        lines += ["", "## Waits on"]
        for d in task["depends_on"]:
            t = by_id.get(d)
            if t:
                own = ", ".join(f"{o} ({records[o]['workspace']})" for o in t.get("owners", []) if o in records) or "nobody"
                lines.append(f"- **{d}** {t['title']} ({t['status']}; owner: {own}). {t.get('detail', '')}")
    if blocks:
        lines += ["", "## Other work waiting on this"]
        for t in blocks:
            own = ", ".join(f"{o} ({records[o]['workspace']})" for o in t.get("owners", []) if o in records) or "nobody"
            lines.append(f"- **{t['id']}** {t['title']} ({t['status']}; owner: {own})")
    if alerts:
        lines += ["", "## Alerts"] + [f"- [{a['severity']}] {a['text']}" for a in alerts]

    related = []
    for alias, r in records.items():
        rec = r["record"]
        items = [w for w in rec.get("work_items", []) if projects & set(w.get("projects", []))]
        if items or alias in {o for t in [task, *blocks] for o in t.get("owners", [])}:
            related.append((alias, r, items))
    if related:
        lines += ["", "## Related agents (do not edit their files without coordinating)"]
        for alias, r, items in related:
            rec = r["record"]
            lines += ["", f"### {alias} — {r['workspace']} (`{r['cwd']}`)", rec.get("summary", "")]
            for w in items:
                lines.append(f"- {w['title']} ({w['status']})")
                lines += [f"  - needs: {n}" for n in w.get("needs", [])]
                lines += [f"  - provides: {p}" for p in w.get("provides", [])]

    path = missions.mission_dir() / "briefs" / f"{task['id']}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def default_prompt(task: dict, brief: Path) -> str:
    return (
        f"You're picking up spook task {task['id']}: {task['title']}. "
        f"Read the brief at {brief} first: it has the task, what it waits on, what's waiting on it, "
        f"and what other agents are doing in related projects. "
        f"Then tell me your plan before changing anything."
    )


# ---------------------------------------------------------------- launch

def _first_pane(tab_id: str) -> str:
    tree = herdr.sock("layout.export", {"tab_id": tab_id})["layout"]["root"]
    return next(herdr._find(tree, lambda n: n.get("type") == "pane"))[0]["pane_id"]


def launch(task: dict, directory: str, workspace_id: str | None, prompt: str, progress) -> dict:
    """Create a tab (or workspace), start claude, type the prompt without sending it."""
    label = f"{task['id']} {task['title']}"[:40]
    if workspace_id:
        progress(f"Opening a tab in workspace {workspace_id}…")
        res = herdr.call("tab", "create", "--workspace", workspace_id, "--cwd", directory,
                         "--label", label, "--no-focus")
        tab_id = res["tab"]["tab_id"]
    else:
        progress("Creating a workspace…")
        res = herdr.call("workspace", "create", "--cwd", directory,
                         "--label", re.sub(r"\s+", " ", label), "--no-focus")
        ws = res["workspace"]
        workspace_id = ws["workspace_id"]
        tab_id = ws.get("active_tab_id") or f"{workspace_id}:t1"
    pane = _first_pane(tab_id)

    progress("Starting Claude (waiting until it's ready)…")
    name = re.sub(r"[^a-z0-9_-]", "-", f"spook-{task['id'].lower()}")[:32]
    try:
        herdr.call("agent", "start", name, "--kind", "claude", "--pane", pane, "--timeout", "60000", timeout=75)
    except herdr.HerdrError as e:
        if "agent_not_ready" not in str(e):
            raise
        # Claude is showing a startup question (folder trust, login…). Never type into it:
        # hand the user the pane and wait until Claude is at its input box.
        progress(f"Claude is asking a startup question in the new tab ({pane}). "
                 "Answer it there; spook types the prompt once Claude is ready (up to 3 min)…")
        herdr.focus_pane(pane)
        try:
            herdr.call("agent", "wait", pane, "--until", "idle", "--timeout", "180000", timeout=200)
        except herdr.HerdrError:
            pass  # Checked just below.
    status = herdr.call("agent", "get", pane).get("agent", {}).get("agent_status")
    if status != "idle":
        raise herdr.HerdrError(f"Claude isn't ready in {pane} (status: {status}); nothing was typed. "
                               f"The brief is at {missions.mission_dir() / 'briefs' / (task['id'] + '.md')}")

    # Record before typing so the graph links the agent to the task even if typing fails.
    mdir = missions.mission_dir()
    launches = read_json(mdir / "launches.json", {})
    launches[pane] = {"task_id": task["id"], "ts": time.time(), "dir": directory}
    write_json(mdir / "launches.json", launches)
    # Put the new agent on the active Mission (by session if herdr knows it yet).
    agent = herdr.call("agent", "get", pane).get("agent", {})
    if (agent.get("agent_session") or {}).get("value"):
        missions.add_agent(missions.active(), agent)
    else:
        missions.add_pending_pane(missions.active(), pane)

    progress("Typing the prompt (not sending it)…")
    one_line = re.sub(r"\s*\n\s*", " ", prompt).strip()
    herdr.call("pane", "send-text", pane, one_line)
    notes.mark_delivered(pane, [task["id"]])  # The brief it was pointed at has them.
    return {"pane_id": pane, "workspace_id": workspace_id, "tab_id": tab_id}


def deliver_notes(pane_id: str, tasks: list[dict], graph: dict) -> str:
    """Point an existing agent at refreshed briefs that hold notes it hasn't seen.

    Types a prompt into its input box without sending it, and only when the agent
    is idle, so nothing lands in a running turn or a dialog. Returns what happened.
    """
    pending = notes.undelivered(pane_id, [t["id"] for t in tasks])
    if not pending:
        return "none"
    status = herdr.call("agent", "get", pane_id).get("agent", {}).get("agent_status")
    if status not in ("idle", "done"):
        return f"busy:{status}"
    briefs = [str(write_brief(t, graph)) for t in tasks if t["id"] in pending]
    what = ", ".join(f"{tid} ({n} new note{'s' * (n > 1)})" for tid, n in pending.items())
    prompt = (
        f"I've added notes on {what} in spook. Read the 'Notes from the human' section of "
        f"{' and '.join(briefs)} and take them into account."
    )
    herdr.call("pane", "send-text", pane_id, prompt)
    notes.mark_delivered(pane_id, list(pending))
    return "typed"
