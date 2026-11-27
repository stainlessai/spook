"""spook <command>

  hook                 herdr event hook: queue the pane if its agent is on a Mission
  sync                 re-check every agent on a Mission and rebuild graphs
  track   [opts]       add agents to the active Mission (or --mission NAME)
  untrack [opts]       remove them
                       opts: --pane ID | --workspace ID (default: current workspace)
  mission list
  mission new NAME [--scope TEXT]
  mission use NAME
  mission rename NAME NEW_NAME
  mission scope NAME TEXT
  open                 dock the board on the right of the current tab (or close it)
  board                run the board UI in this terminal
  status               Missions, their agents and graph counts, worker state
"""

from __future__ import annotations

import json
import os
import sys

from . import herdr, missions
from .paths import log, read_json, state_dir
from .worker import FINISHED, enqueue, live_agents, rebuild_graph, run_worker


def _context() -> dict:
    try:
        return json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except json.JSONDecodeError:
        return {}


def _opt(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None


def _mission_arg(argv: list[str]) -> str:
    name = _opt(argv, "--mission")
    if not name:
        return missions.active()
    mid = missions.find(name)
    if not mid:
        sys.exit(f"no Mission named {name!r}")
    return mid


def cmd_hook() -> None:
    try:
        ev = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    except json.JSONDecodeError:
        return
    data = ev.get("data") if isinstance(ev.get("data"), dict) else ev
    status = str(data.get("agent_status") or "").lower()
    pane = data.get("pane_id")
    if status not in FINISHED or not pane:
        return
    # Cheap check first: panes of known members (a /clear keeps the pane, so this
    # also catches a new session that the sync will then follow).
    if pane not in missions.all_member_panes():
        return
    enqueue(pane)
    run_worker()


def cmd_sync() -> None:
    enqueue("*")
    if not run_worker():
        print("spook: analysis already running; queued a full sync.")


def cmd_track(on: bool, argv: list[str]) -> None:
    mid = _mission_arg(argv)
    pane = _opt(argv, "--pane")
    ws = _opt(argv, "--workspace") or _context().get("workspace_id") or os.environ.get("HERDR_WORKSPACE_ID")
    agents = live_agents()
    chosen = [a for a in agents if a["pane_id"] == pane] if pane else [a for a in agents if a["workspace_id"] == ws]
    if not chosen:
        sys.exit("no Claude agents found for that pane/workspace")
    name = missions.info(mid)["name"]
    for a in chosen:
        if on:
            missions.add_agent(mid, a)
        else:
            missions.remove_agent(mid, a["agent_session"]["value"])
    msg = f"{'Added' if on else 'Removed'} {len(chosen)} agent(s) {'to' if on else 'from'} Mission '{name}'"
    print(msg)
    log(msg)
    if on:
        enqueue("*")
        run_worker()
    else:
        rebuild_graph(mid)


def cmd_mission(argv: list[str]) -> None:
    sub = argv[0] if argv else "list"
    if sub == "list":
        act = missions.active()
        for mid, m in missions.all_missions().items():
            n = len(missions.members(mid))
            print(f"{'*' if mid == act else ' '} {m['name']}  ({mid}, {n} agents)"
                  + (f"\n    scope: {m['scope']}" if m.get("scope") else ""))
    elif sub == "new" and len(argv) > 1:
        mid = missions.create(argv[1], _opt(argv, "--scope") or "")
        print(f"Created and switched to Mission '{argv[1]}' ({mid})")
    elif sub in ("use", "rename", "scope") and len(argv) > 1:
        mid = missions.find(argv[1])
        if not mid:
            sys.exit(f"no Mission named {argv[1]!r}")
        if sub == "use":
            missions.set_active(mid)
        elif sub == "rename" and len(argv) > 2:
            missions.update(mid, name=argv[2])
        elif sub == "scope" and len(argv) > 2:
            missions.update(mid, scope=" ".join(argv[2:]))
        else:
            sys.exit(__doc__)
        print("ok")
    else:
        sys.exit(__doc__)


def cmd_status() -> None:
    print(f"state: {state_dir()}")
    act = missions.active()
    for mid, m in missions.all_missions().items():
        g = read_json(missions.mission_dir(mid) / "graph.json", {})
        open_ = len([t for t in g.get("tasks", []) if t["status"] not in ("done", "dropped")])
        print(f"{'*' if mid == act else ' '} {m['name']}: {len(missions.members(mid))} agents, "
              f"{len(g.get('tasks', []))} tasks ({open_} open), {len(g.get('alerts', []))} alerts")
    print(f"worker: {read_json(state_dir() / 'status.json', {})}")


def main(argv: list[str]) -> None:
    cmd = argv[0] if argv else "board"
    if cmd == "hook":
        cmd_hook()
    elif cmd == "sync":
        cmd_sync()
    elif cmd in ("track", "untrack"):
        cmd_track(cmd == "track", argv[1:])
    elif cmd == "mission":
        cmd_mission(argv[1:])
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
