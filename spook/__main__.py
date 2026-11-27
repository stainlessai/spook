"""spook <command>

  hook       herdr event hook: queue the pane if its agent just finished a turn
  sync       re-check every tracked agent and rebuild the graph
  track      track the current (or --workspace ID) workspace
  untrack    stop tracking it
  open       dock the board on the right of the current tab (or close it)
  board      run the board UI in this terminal
  status     print tracked workspaces, worker status and graph counts
"""

from __future__ import annotations

import json
import os
import sys

from . import herdr
from .paths import log, read_json, state_dir
from .worker import FINISHED, enqueue, is_tracked, migrate_renumbered, rebuild_graph, run_worker, set_tracked, tracked


def _context() -> dict:
    try:
        return json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except json.JSONDecodeError:
        return {}


def _current_workspace(argv: list[str]) -> tuple[str, str]:
    if "--workspace" in argv:
        ws_id = argv[argv.index("--workspace") + 1]
    else:
        ws_id = _context().get("workspace_id") or os.environ.get("HERDR_WORKSPACE_ID") or ""
    if not ws_id:
        sys.exit("no workspace in context; pass --workspace ID")
    labels = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in herdr.workspaces()}
    return ws_id, labels.get(ws_id, ws_id)


def cmd_hook() -> None:
    try:
        ev = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    except json.JSONDecodeError:
        return
    data = ev.get("data") if isinstance(ev.get("data"), dict) else ev
    status = str(data.get("agent_status") or "").lower()
    pane, ws = data.get("pane_id"), data.get("workspace_id")
    if status not in FINISHED or not pane or not ws:
        return
    t = tracked()
    if not t:
        return
    if not is_tracked(ws, t):
        t = migrate_renumbered(herdr.workspaces())
        if not is_tracked(ws, t):
            return
    enqueue(pane)
    run_worker()


def cmd_sync() -> None:
    enqueue("*")
    if not run_worker():
        print("spook: analysis already running; queued a full sync.")


def cmd_track(on: bool, argv: list[str]) -> None:
    ws_id, label = _current_workspace(argv)
    set_tracked(ws_id, label, on)
    msg = f"{'Tracking' if on else 'Stopped tracking'} workspace '{label}'"
    print(msg)
    log(msg)
    if on:
        enqueue("*")
        run_worker()
    else:
        rebuild_graph()


def cmd_status() -> None:
    d = state_dir()
    g = read_json(d / "graph.json", {})
    print(f"state: {d}")
    print(f"tracked: {tracked()}")
    print(f"worker: {read_json(d / 'status.json', {})}")
    print(f"graph: {len(g.get('tasks', []))} tasks, {len(g.get('concerns', []))} concerns, {len(g.get('alerts', []))} alerts")


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "board"
    if cmd == "hook":
        cmd_hook()
    elif cmd == "sync":
        cmd_sync()
    elif cmd in ("track", "untrack"):
        cmd_track(cmd == "track", argv[1:])
    elif cmd == "open":
        herdr.toggle_board()
    elif cmd == "board":
        from .tui import run
        run()
    elif cmd == "status":
        cmd_status()
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
