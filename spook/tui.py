"""The spook board: concerns → tasks with dependencies, alerts, and per-agent summaries."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual import work
from textual.widgets import (
    Button, DataTable, Footer, Input, Label, Markdown, Select, SelectionList, Static, TabbedContent,
    TabPane, TextArea, Tree,
)

from . import activity, herdr, launch
from .paths import read_json, state_dir, write_json
from .worker import set_tracked, tracked, tracked_records

ICON = {
    "open": ("○", "white"),
    "in_progress": ("◐", "cyan"),
    "blocked": ("■", "red"),
    "done": ("✓", "green"),
    "dropped": ("✗", "grey50"),
}
SEV = {"critical": ("‼", "bold red"), "warn": ("!", "yellow"), "info": ("i", "blue")}


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 90 else f"{s // 60}m ago" if s < 5400 else f"{s // 3600}h ago"


SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _dur(seconds: float) -> str:
    s = int(seconds)
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


def _trigger_sync() -> None:
    # Run through herdr so the analysis outlives this popup.
    subprocess.Popen(
        [herdr._bin(), "plugin", "action", "invoke", "spook.sync"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class Workspaces(ModalScreen[None]):
    BINDINGS = [Binding("s", "save", "Save"), Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    Workspaces { align: center middle; }
    #box { width: 70; height: auto; max-height: 80%; border: round $accent; padding: 1 2; background: $panel; }
    SelectionList { height: auto; max-height: 30; }
    """

    def compose(self) -> ComposeResult:
        t = tracked()
        self.ws = herdr.workspaces()
        items = [
            (f"{w.get('label') or w['workspace_id']}  ({w['workspace_id']}, {w.get('pane_count', '?')} panes)",
             w["workspace_id"], w["workspace_id"] in t)
            for w in self.ws
        ]
        with Vertical(id="box"):
            yield Label("Track which workspaces?  space: toggle · s: save · esc: cancel")
            yield SelectionList(*items)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        chosen = set(self.query_one(SelectionList).selected)
        t = tracked()
        changed = False
        for w in self.ws:
            wid, label = w["workspace_id"], w.get("label") or w["workspace_id"]
            was = wid in t
            if (wid in chosen) != was:
                set_tracked(wid, label, wid in chosen)
                changed = True
        if changed:
            _trigger_sync()
        self.dismiss(None)

class LaunchAgent(ModalScreen[dict | None]):
    """Confirm, then start a Claude agent for a task with a pre-filled prompt."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    LaunchAgent { align: center middle; }
    #launch { width: 96%; max-width: 110; height: auto; max-height: 96%; border: round $accent;
              padding: 1 2; background: $panel; }
    #launch Label { margin-top: 1; color: $text-muted; }
    #launch #title { margin-top: 0; color: $text; text-style: bold; }
    #prompt { height: 7; }
    #buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #buttons Button { margin-left: 2; }
    #progress { margin-top: 1; color: $warning; height: auto; }
    """

    NEW_WS = "__new__"

    def __init__(self, task: dict, graph: dict) -> None:
        super().__init__()
        self.task_ = task
        self.brief = launch.write_brief(task, graph)
        self.dirs = launch.candidate_dirs(task)
        t = tracked()
        self.ws_opts = [(f"New tab in '{label}'", wid) for wid, label in t.items()]
        self.ws_opts.append(("New workspace (spook tracks it)", self.NEW_WS))

    def compose(self) -> ComposeResult:
        first = self.dirs[0][0] if self.dirs else ""
        with VerticalScroll(id="launch"):
            yield Static(f"Start an agent for {self.task_['id']}: {self.task_['title']}", id="title")
            yield Label("Directory")
            if self.dirs:
                yield Select([(f"{d}  ({why})", d) for d, why in self.dirs], value=first,
                             allow_blank=False, id="dir_pick")
            yield Input(value=first, placeholder="/path/to/project", id="dir")
            yield Label("Where")
            yield Select(self.ws_opts, value=launch.workspace_for(first) or self.NEW_WS,
                         allow_blank=False, id="where")
            yield Label("Prompt: typed into the new agent, NOT sent. You review it and press Enter yourself.")
            yield TextArea(launch.default_prompt(self.task_, self.brief), id="prompt")
            yield Static(f"Brief: {self.brief}", classes="muted")
            yield Static("", id="progress")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Start agent", id="go", variant="primary")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "dir_pick" and event.value:
            self.query_one("#dir", Input).value = str(event.value)
            self.query_one("#where", Select).value = launch.workspace_for(str(event.value)) or self.NEW_WS

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        directory = self.query_one("#dir", Input).value.strip()
        if not Path(directory).expanduser().is_dir():
            self.query_one("#progress", Static).update(f"Not a directory: {directory}")
            return
        where = self.query_one("#where", Select).value
        prompt = self.query_one("#prompt", TextArea).text
        for b in self.query(Button):
            b.disabled = True
        self._run(str(Path(directory).expanduser()), None if where == self.NEW_WS else where, prompt)

    @work(thread=True, exclusive=True)
    def _run(self, directory: str, workspace_id: str | None, prompt: str) -> None:
        say = lambda msg: self.app.call_from_thread(self.query_one("#progress", Static).update, msg)
        try:
            result = launch.launch(self.task_, directory, workspace_id, prompt, say)
        except Exception as e:  # Show it; let the user retry or cancel.
            say(f"Failed: {e}")
            self.app.call_from_thread(lambda: [setattr(b, "disabled", False) for b in self.query(Button)])
            return
        self.app.call_from_thread(self.dismiss, result)


class Board(App):
    TITLE = "spook"
    CSS = """
    #status { height: auto; padding: 0 1; background: $boost; }
    #tree, #agents { width: 50%; }
    #act_head { height: auto; padding: 0 1; color: $text-muted; border-bottom: solid $primary-darken-2; }
    #calls { width: 50%; }
    #call_box { width: 50%; border-left: solid $primary-darken-2; padding: 0 1; }
    .narrow #calls { width: 100%; height: 60%; }
    .narrow #call_box { width: 100%; height: 40%; border-left: none; border-top: solid $primary-darken-2; }
    #detail_box, #agent_box { width: 50%; border-left: solid $primary-darken-2; padding: 0 1; }
    /* Docked in a narrow column: stack the list above the details. */
    .narrow .split { layout: vertical; }
    .narrow #tree, .narrow #agents { width: 100%; height: 55%; }
    .narrow #detail_box, .narrow #agent_box { width: 100%; height: 45%; border-left: none; border-top: solid $primary-darken-2; }
    """
    NARROW_COLS = 110
    BINDINGS = [
        Binding("q,escape", "quit", "Close"),
        Binding("r", "sync", "Re-analyze"),
        Binding("w", "workspaces", "Workspaces"),
        Binding("g", "goto", "Go to agent"),
        Binding("a", "assign", "Start agent"),
        Binding("d", "toggle_done", "Show/hide done"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.show_done = False
        self.graph: dict = {}
        self.records: dict[str, dict] = {}  # alias -> agent record
        self._sig = None
        self._agents_sig = None
        self._selected_key = None
        self._rebuilding = False

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with TabbedContent():
            with TabPane("Tasks", id="tab_tasks"):
                with Horizontal(classes="split"):
                    yield Tree("tasks", id="tree")
                    with VerticalScroll(id="detail_box"):
                        yield Markdown(id="detail")
            with TabPane("Agents", id="tab_agents"):
                with Horizontal(classes="split"):
                    yield DataTable(id="agents", cursor_type="row")
                    with VerticalScroll(id="agent_box"):
                        yield Markdown(id="agent_detail")
            with TabPane("Activity", id="tab_activity"):
                with Vertical():
                    yield Static(id="act_head")
                    with Horizontal(classes="split"):
                        yield DataTable(id="calls", cursor_type="row")
                        with VerticalScroll(id="call_box"):
                            yield Markdown(id="call_detail")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one(Tree)
        tree.show_root = False
        t = self.query_one("#agents", DataTable)
        t.add_columns("id", "workspace", "status", "phase", "about")
        self.query_one("#calls", DataTable).add_columns("", "started", "what", "model", "time", "cost")
        self.calls: list[dict] = []
        self.screen.set_class(self.size.width < self.NARROW_COLS, "narrow")
        self._register_pane(True)
        self.poll()
        self.set_interval(1, self.poll)
        tree.focus()

    def on_unmount(self) -> None:
        self._register_pane(False)

    def _register_pane(self, on: bool) -> None:
        """Tell the worker which pane is the board so it never analyzes it as an agent."""
        pane = os.environ.get("HERDR_PANE_ID")
        if not pane:
            return
        p = state_dir() / "board_panes.json"
        panes = read_json(p, {})
        if on:
            panes[pane] = {"size": [self.size.width, self.size.height], "pid": os.getpid()}
        else:
            panes.pop(pane, None)
        write_json(p, panes)

    def on_resize(self, event) -> None:
        self.screen.set_class(event.size.width < self.NARROW_COLS, "narrow")

    # ---------------------------------------------------------------- data

    @staticmethod
    def _mtimes(files):
        return tuple((str(f), f.stat().st_mtime if f.exists() else 0) for f in files)

    def poll(self) -> None:
        d = state_dir()
        # Agent files change constantly during analysis; only redraw the tree when the graph does.
        agents_sig = self._mtimes([d / "tracked.json", *sorted((d / "agents").glob("*.json"))])
        if agents_sig != self._agents_sig:
            self._agents_sig = agents_sig
            self.records = {r["alias"]: r for r in tracked_records()}
            self._render_agents()
        sig = self._mtimes([d / "graph.json", d / "tracked.json"])
        if sig != self._sig:
            self._sig = sig
            self.graph = read_json(d / "graph.json", {})
            self._render_tree()
        status = read_json(state_dir() / "status.json", {})
        self.calls, self.syncs = activity.load()
        self._render_status(status)
        self._render_activity(status)

    def _render_status(self, s: dict) -> None:
        g = self.graph
        n_t = len([x for x in g.get("tasks", []) if x["status"] not in ("done", "dropped")])
        n_a = len(g.get("alerts", []))
        left = f"{len(tracked())} workspaces · {len(self.records)} agents · {n_t} open tasks · {n_a} alerts"
        if not tracked():
            left = "No workspaces tracked yet — press w to pick some"
        running = [c for c in self.calls if c["state"] == "running"]
        queued = read_json(state_dir() / "queue.json", [])
        if running:
            c = running[0]
            spin = SPIN[int(time.time() * 4) % len(SPIN)]
            act = Text.assemble((f"{spin} {c['label']}", "bold cyan"), f" · {_dur(time.time() - c['ts'])}")
            if queued:
                act.append(f" · {len(queued)} queued", style="yellow")
        elif s.get("running"):
            act = Text(f"{SPIN[int(time.time() * 4) % len(SPIN)]} {s.get('phase', 'working')}", style="cyan")
        elif s.get("phase") == "error":
            act = Text(f"✗ last run failed: {s.get('error', '')[:100]}  (see Activity)", style="red")
        else:
            act = Text(f"idle · graph updated {_ago(g.get('updated_at'))}", style="grey62")
        self.query_one("#status", Static).update(Text.assemble(left, "\n", act))

    def _render_activity(self, s: dict) -> None:
        queued = read_json(state_dir() / "queue.json", [])
        q = ", ".join("all agents" if x == "*" else f"turn ended in {x}" for x in queued) or "empty"
        worker = (f"running since {_ago(s.get('started_at'))}" if s.get("running")
                  else f"idle · last run {_ago(s.get('last_run'))}")
        spent = sum(c.get("cost") or 0 for c in self.calls if c["ts"] > time.time() - 86400)
        n_day = len([c for c in self.calls if c["ts"] > time.time() - 86400])
        self.query_one("#act_head", Static).update(
            f"Worker: {worker}\nQueue: {q}\nLast 24h: {n_day} calls · ${spent:.2f} at API rates"
        )
        table = self.query_one("#calls", DataTable)
        keep = table.cursor_row
        table.clear()
        icons = {"running": ("⟳", "cyan"), "done": ("✓", "green"), "error": ("✗", "red"), "lost": ("?", "yellow")}
        for c in self.calls[:200]:
            icon, color = icons.get(c["state"], ("?", "white"))
            elapsed = (time.time() - c["ts"]) if c["state"] == "running" else (c.get("duration_ms") or 0) / 1000
            table.add_row(
                Text(icon, style=color),
                time.strftime("%H:%M:%S", time.localtime(c["ts"])),
                c.get("label", ""),
                c.get("model", ""),
                _dur(elapsed),
                f"${c['cost']:.3f}" if c.get("cost") else "",
                key=c["id"],
            )
        if table.row_count:
            table.move_cursor(row=min(keep, table.row_count - 1))

    def on_data_table_row_highlighted_calls(self, row_key) -> None:
        c = next((c for c in self.calls if c["id"] == row_key), None)
        md = self.query_one("#call_detail", Markdown)
        if not c:
            md.update("")
            return
        lines = [
            f"## {c.get('label')}",
            f"**State**: {c['state']}  ·  **Model**: {c.get('model')}  ·  **pid**: {c.get('pid')}",
            "",
            f"**Started**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(c['ts']))}",
            "",
            f"**Sent**: {c.get('chars', 0):,} characters (about {c.get('chars', 0) // 4:,} tokens)",
        ]
        if c.get("duration_ms"):
            lines += ["", f"**Took**: {_dur(c['duration_ms'] / 1000)}"]
        if c.get("cost"):
            lines += ["", f"**Cost at API rates**: ${c['cost']:.4f}"]
        if c.get("error"):
            lines += ["", "### Error", "```", c["error"], "```"]
        if c["state"] == "lost":
            lines += ["", "_The worker stopped before this call finished (process gone)._"]
        md.update("\n".join(lines))

    def _owner_label(self, alias: str) -> str:
        r = self.records.get(alias)
        return f"{alias} ({r['workspace']})" if r else alias

    def _task_label(self, t: dict, by_id: dict) -> Text:
        icon, color = ICON.get(t["status"], ("?", "white"))
        txt = Text.assemble((f"{icon} ", color), (f"{t['id']} ", "bold"), t["title"])
        if t.get("projects"):
            txt.append(f"  · {', '.join(t['projects'])}", style="grey62")
        waiting = [d for d in t.get("depends_on", []) if by_id.get(d, {}).get("status") not in ("done", "dropped")]
        if waiting:
            txt.append(f"  ← {', '.join(waiting)}", style="yellow")
        if not t.get("owners"):
            txt.append("  unowned", style="magenta")
        return txt

    def _render_tree(self) -> None:
        tree = self.query_one(Tree)
        keep = self._current_key()
        self._rebuilding = True  # Ignore the highlight events clear() and rebuild fire.
        tree.clear()
        g = self.graph
        tasks = [t for t in g.get("tasks", []) if self.show_done or t["status"] not in ("done", "dropped")]
        by_id = {t["id"]: t for t in g.get("tasks", [])}

        alerts = g.get("alerts", [])
        if alerts:
            an = tree.root.add(Text(f"Alerts ({len(alerts)})", style="bold yellow"), data=("alerts", None), expand=True)
            order = {"critical": 0, "warn": 1, "info": 2}
            for i, a in sorted(enumerate(alerts), key=lambda x: order.get(x[1]["severity"], 3)):
                icon, style = SEV.get(a["severity"], ("?", ""))
                an.add_leaf(Text.assemble((f"{icon} ", style), a["text"]), data=("alert", i))

        concerns = {c["id"]: c for c in g.get("concerns", [])}
        groups: dict[str, list] = {}
        for t in tasks:
            groups.setdefault(t.get("concern_id") if t.get("concern_id") in concerns else "_", []).append(t)
        ordered = sorted(concerns.values(), key=lambda c: (not c.get("crosscutting"), c["name"]))
        for c in ordered + ([{"id": "_", "name": "Other", "crosscutting": False}] if "_" in groups else []):
            items = groups.get(c["id"], [])
            if not items:
                continue
            label = Text.assemble(("◆ ", "cyan" if c.get("crosscutting") else "grey62"), (c["name"], "bold"))
            if c.get("crosscutting"):
                label.append("  crosscutting", style="cyan italic")
            node = tree.root.add(label, data=("concern", c["id"]), expand=True)
            for t in sorted(items, key=lambda t: list(ICON).index(t["status"]) if t["status"] in ICON else 9):
                node.add_leaf(self._task_label(t, by_id), data=("task", t["id"]))

        if not tasks and not alerts:
            tree.root.add_leaf(Text("Nothing yet. Track workspaces with w; analysis runs when agents finish a turn.", style="italic"))
        self.call_after_refresh(self._finish_rebuild, keep)

    def _finish_rebuild(self, key) -> None:
        tree = self.query_one(Tree)

        def walk(node):
            for child in node.children:
                if child.data == key:
                    return child
                found = walk(child)
                if found:
                    return found
            return None

        node = walk(tree.root) if key else None
        if node is not None:
            tree.move_cursor(node)
        self._rebuilding = False
        self._selected_key = tree.cursor_node.data if tree.cursor_node else None
        self._show_detail(self._selected_key)

    def _current_key(self):
        """What the tree cursor is on right now (the widget is the source of truth)."""
        tree = self.query_one(Tree)
        if tree.cursor_node is not None and tree.cursor_node.data:
            return tree.cursor_node.data
        return self._selected_key

    def _render_agents(self) -> None:
        table = self.query_one("#agents", DataTable)
        keep = table.cursor_row
        table.clear()
        for alias, r in sorted(self.records.items(), key=lambda kv: int(kv[0][1:])):
            rec = r["record"]
            table.add_row(alias, r["workspace"], r.get("agent_status") or "?", rec.get("phase", ""), rec.get("title", ""), key=alias)
        if table.row_count:
            table.move_cursor(row=min(keep, table.row_count - 1))

    # ---------------------------------------------------------------- details

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if self._rebuilding:
            return
        self._selected_key = event.node.data
        self._show_detail(event.node.data)

    def _show_detail(self, data) -> None:
        md = self.query_one("#detail", Markdown)
        if not data:
            md.update("")
            return
        kind, key = data
        g = self.graph
        by_id = {t["id"]: t for t in g.get("tasks", [])}
        if kind == "task":
            md.update(self._task_md(by_id[key], by_id))
        elif kind == "alert":
            a = g["alerts"][key]
            lines = [f"## {a['severity'].upper()}", a["text"], ""]
            if a.get("agents"):
                lines.append("**Agents**: " + ", ".join(self._owner_label(x) for x in a["agents"]))
            for tid in a.get("task_ids", []):
                if tid in by_id:
                    lines.append(f"- **{tid}**: {by_id[tid]['title']} ({by_id[tid]['status']})")
            md.update("\n".join(lines))
        elif kind == "concern":
            c = next((c for c in g.get("concerns", []) if c["id"] == key), None)
            if c:
                ts = [t for t in g.get("tasks", []) if t.get("concern_id") == key]
                projects = sorted({p for t in ts for p in t.get("projects", [])})
                md.update(
                    f"## {c['name']}\n\n{c['description']}\n\n**Projects**: {', '.join(projects) or '—'}\n\n"
                    + "\n".join(f"- {ICON.get(t['status'], ('?',))[0]} **{t['id']}**: {t['title']}" for t in ts)
                )
        else:
            md.update("")

    def _task_md(self, t: dict, by_id: dict) -> str:
        blocks = [x for x in by_id.values() if t["id"] in x.get("depends_on", [])]
        out = [
            f"## {t['id']} · {t['title']}",
            f"**Status**: {t['status']}  ·  **Projects**: {', '.join(t.get('projects', [])) or '—'}",
            "",
            f"**Owners**: {', '.join(self._owner_label(o) for o in t.get('owners', [])) or '_unowned_'}",
            "",
            t.get("detail", ""),
        ]
        if t.get("depends_on"):
            out += ["", "### Waiting on"]
            out += [f"- **{d}**: {by_id[d]['title']} ({by_id[d]['status']})" if d in by_id else f"- {d}" for d in t["depends_on"]]
        if blocks:
            out += ["", "### Blocks"] + [f"- **{b['id']}**: {b['title']} ({b['status']})" for b in blocks]
        if t.get("evidence"):
            out += ["", "### Evidence", t["evidence"]]
        if t.get("owners"):
            out += ["", "_g: jump to owner agent_"]
        return "\n".join(out)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "calls":
            self.on_data_table_row_highlighted_calls(event.row_key.value if event.row_key else None)
            return
        alias = event.row_key.value if event.row_key else None
        r = self.records.get(alias)
        md = self.query_one("#agent_detail", Markdown)
        if not r:
            md.update("")
            return
        rec = r["record"]
        out = [
            f"## {alias} · {rec.get('title', '')}",
            f"`{r['cwd']}`  ·  pane {r['pane_id']}  ·  {r.get('agent_status')}  ·  updated {_ago(r.get('updated_at'))}",
            "",
            f"**Goal**: {rec.get('goal', '')}",
            "",
            rec.get("summary", ""),
            "",
            "### Work items",
        ]
        for w in rec.get("work_items", []):
            out.append(f"- {ICON.get(w['status'], ('?',))[0]} **{w['title']}**: {', '.join(w.get('projects', []))}")
            out += [f"    - needs: {n}" for n in w.get("needs", [])]
            out += [f"    - provides: {p}" for p in w.get("provides", [])]
        if rec.get("open_questions"):
            out += ["", "### Open questions"] + [f"- {q}" for q in rec["open_questions"]]
        out += ["", "_g: jump to this agent_"]
        md.update("\n".join(out))

    # ---------------------------------------------------------------- actions

    def action_sync(self) -> None:
        _trigger_sync()
        self.notify("Re-analysis started")

    def action_workspaces(self) -> None:
        self.push_screen(Workspaces(), lambda _: self.poll())

    def action_toggle_done(self) -> None:
        self.show_done = not self.show_done
        self._render_tree()

    def action_goto(self) -> None:
        alias = None
        if self.query_one(TabbedContent).active == "tab_agents":
            table = self.query_one("#agents", DataTable)
            if table.row_count:
                alias = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        else:
            t = self._selected_task()
            alias = t["owners"][0] if t and t.get("owners") else None
        r = self.records.get(alias) if alias else None
        if not r:
            if self._selected_task():
                self.action_assign()  # Unowned task: offer to start one.
            else:
                self.notify("Select a task or an alert to jump to its agent", severity="warning")
            return
        herdr.focus_pane(r["pane_id"])
        if not os.environ.get("HERDR_PANE_ID"):  # Popup: get out of the way. Docked: stay.
            self.exit()

    def _selected_task(self) -> dict | None:
        """The task under the cursor, or the first task of a selected alert."""
        key_ = self._current_key()
        if self.query_one(TabbedContent).active != "tab_tasks" or not key_:
            return None
        kind, key = key_
        tasks = {t["id"]: t for t in self.graph.get("tasks", [])}
        if kind == "task":
            return tasks.get(key)
        if kind == "alert":
            ids = self.graph["alerts"][key].get("task_ids", [])
            unowned = [tasks[i] for i in ids if i in tasks and not tasks[i].get("owners")]
            return (unowned or [tasks[i] for i in ids if i in tasks] or [None])[0]
        return None

    def action_assign(self) -> None:
        task = self._selected_task()
        if not task:
            self.notify("Select a task (or an alert about one) first", severity="warning")
            return

        def done(result: dict | None) -> None:
            if result:
                self.notify(f"Agent started for {task['id']} in pane {result['pane_id']}. Prompt typed, not sent.")
                herdr.focus_pane(result["pane_id"])

        self.push_screen(LaunchAgent(task, self.graph), done)


def run() -> None:
    Board().run()
