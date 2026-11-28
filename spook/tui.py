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
    Button, DataTable, Input, Label, Markdown, OptionList, Select, SelectionList, Static,
    TabbedContent, TabPane, TextArea, Tree,
)

from . import activity, herdr, launch, missions, notes
from .paths import read_json, state_dir, write_json
from .worker import (
    delete_forever, live_agents, load_added, load_edits, load_overrides, load_trash, mission_records, restore_task,
    toggle_done, trash_task,
)

# Keyed by status, plus the two derived groups (unowned, waiting): on the board a
# task shows the icon of the status group it falls in (Board._status_group).
ICON = {
    "blocked": ("■", "red"),
    "unowned": ("◌", "magenta"),
    "in_progress": ("◐", "cyan"),
    "waiting": ("⧗", "yellow"),
    "open": ("○", "white"),
    "done": ("✓", "green3"),
    "dropped": ("✗", "grey50"),
}
# Task sort modes, cycled with `s`. "concern" is the grouped tree; "status" groups
# by what needs attention, most urgent first; "dependencies" nests each task's
# dependencies under it.
SORT_MODES = ("concern", "status", "dependencies")
STATUS_GROUPS = [
    ("blocked", "Blocked", "red"),
    ("unowned", "Unowned", "magenta"),
    ("in_progress", "In progress", "cyan"),
    ("waiting", "Waiting on dependencies", "yellow"),
    ("open", "Open", "white"),
    ("done", "Done", "green3"),
    ("dropped", "Dropped", "grey50"),
]
SEV = {"critical": ("‼", "bold red"), "warn": ("!", "yellow"), "info": ("i", "blue")}


def _task_num(tid: str) -> tuple:
    digits = "".join(ch for ch in tid if ch.isdigit())
    return (int(digits) if digits else 1 << 30, tid)


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


class AgentPicker(ModalScreen[None]):
    def __init__(self, mission: str | None = None) -> None:
        super().__init__()
        self.mission = mission or missions.active()

    """Choose which agents are on the active Mission. Agents can be on several Missions."""

    BINDINGS = [Binding("s", "save", "Save"), Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    AgentPicker { align: center middle; }
    #box { width: 96%; max-width: 110; height: auto; max-height: 90%; border: round $accent;
           padding: 1 2; background: $panel; }
    SelectionList { height: auto; max-height: 30; }
    """

    def compose(self) -> ComposeResult:
        mid = self.mission
        mine = missions.members(mid)
        aliases = read_json(state_dir() / "aliases.json", {})
        ws_labels = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in herdr.workspaces()}
        names = {m: info["name"] for m, info in missions.all_missions().items()}
        self.agents = live_agents()
        items = []
        for a in self.agents:
            sid = a["agent_session"]["value"]
            others = [names[m] for m in missions.missions_for_session(sid) if m != mid]
            label = f"{aliases.get(sid, '··')}  {ws_labels.get(a['workspace_id'], a['workspace_id'])} · " \
                    f"{(a.get('terminal_title_stripped') or '')[:40]}"
            if others:
                label += f"  (also on: {', '.join(others)})"
            items.append((label, sid, sid in mine))
        with Vertical(id="box"):
            yield Label(f"Agents on Mission '{missions.info(mid)['name']}'   space: toggle · s: save · esc: cancel")
            yield SelectionList(*items)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        mid = self.mission
        chosen = set(self.query_one(SelectionList).selected)
        mine = set(missions.members(mid))
        changed = False
        for a in self.agents:
            sid = a["agent_session"]["value"]
            if sid in chosen and sid not in mine:
                missions.add_agent(mid, a)
                changed = True
            elif sid not in chosen and sid in mine:
                missions.remove_agent(mid, sid)
                changed = True
        if changed:
            _trigger_sync()
        self.dismiss(None)


class MissionForm(ModalScreen[dict | None]):
    """Name and scope for a new or existing Mission."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    MissionForm { align: center middle; }
    #mform { width: 96%; max-width: 90; height: auto; border: round $accent; padding: 1 2; background: $panel; }
    #mform Label { margin-top: 1; color: $text-muted; }
    #mscope { height: 5; }
    #mbuttons { height: auto; margin-top: 1; align-horizontal: right; }
    #mbuttons Button { margin-left: 2; }
    """

    def __init__(self, title: str, name: str = "", scope: str = "") -> None:
        super().__init__()
        self.title_, self.name_, self.scope_ = title, name, scope

    def compose(self) -> ComposeResult:
        with Vertical(id="mform"):
            yield Static(self.title_, classes="bold")
            yield Label("Name")
            yield Input(value=self.name_, id="mname")
            yield Label("Scope: what belongs on this Mission. The task graph only includes agents' work that "
                        "fits it. Leave empty to include everything.")
            yield TextArea(self.scope_, id="mscope")
            with Horizontal(id="mbuttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save" and self.query_one("#mname", Input).value.strip():
            self.dismiss({"name": self.query_one("#mname", Input).value.strip(),
                          "scope": self.query_one("#mscope", TextArea).text.strip()})
        elif event.button.id == "cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
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
        self.ws_opts = [(f"New tab in '{w.get('label') or w['workspace_id']}'", w["workspace_id"])
                        for w in herdr.workspaces()]
        self.ws_opts.append(("New workspace", self.NEW_WS))

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
            yield Select(self.ws_opts, value=(launch.workspace_for(first) if launch.workspace_for(first) in
                                              [w for _, w in self.ws_opts] else self.NEW_WS),
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


class NotesEditor(ModalScreen[None]):
    """Private notes on one task. Agents see them via briefs; the models never do."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("delete,backspace", "delete_note", "Delete note")]
    DEFAULT_CSS = """
    NotesEditor { align: center middle; }
    #notes_box { width: 96%; max-width: 100; height: auto; max-height: 96%; border: round $accent;
                 padding: 1 2; background: $panel; }
    #notes_box Label { margin-top: 1; color: $text-muted; }
    #notes_title { text-style: bold; }
    #note_list { height: auto; max-height: 12; }
    #note_text { height: 5; }
    #notes_buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #notes_buttons Button { margin-left: 2; }
    """

    def __init__(self, task: dict) -> None:
        super().__init__()
        self.task_ = task

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="notes_box"):
            yield Static(f"Notes on {self.task_['id']}: {self.task_['title']}", id="notes_title")
            yield Static("Private: never sent to spook's models. Agents get them through the task brief "
                         "(when started with a, or when g finds notes they haven't seen).", classes="muted")
            yield Label("Notes (select one and press delete to remove it)")
            yield OptionList(id="note_list")
            yield Label("New note")
            yield TextArea(id="note_text")
            with Horizontal(id="notes_buttons"):
                yield Button("Close", id="close")
                yield Button("Add note", id="add", variant="primary")

    def on_mount(self) -> None:
        self._reload()
        self.query_one("#note_text", TextArea).focus()

    def _reload(self) -> None:
        ol = self.query_one("#note_list", OptionList)
        ol.clear_options()
        self.items = notes.for_task(self.task_["id"])
        if not self.items:
            ol.add_option("(none yet)")
            ol.disabled = True
            return
        ol.disabled = False
        for n in self.items:
            ol.add_option(f"{time.strftime('%m-%d %H:%M', time.localtime(n['at']))}  {n['text']}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)
            return
        box = self.query_one("#note_text", TextArea)
        if box.text.strip():
            notes.add(self.task_["id"], box.text)
            box.clear()
            self._reload()
            self.app.notify(f"Note added to {self.task_['id']}")

    def action_delete_note(self) -> None:
        ol = self.query_one("#note_list", OptionList)
        if self.focused is not ol or ol.highlighted is None or not self.items:
            return
        notes.delete(self.task_["id"], self.items[ol.highlighted]["id"])
        self._reload()

    def action_close(self) -> None:
        self.dismiss(None)


class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "Cancel")]
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    #confirm { width: 60; max-width: 96%; height: auto; border: round $error; padding: 1 2; background: $panel; }
    #confirm_buttons { height: auto; margin-top: 1; align-horizontal: right; }
    #confirm_buttons Button { margin-left: 2; }
    """

    def __init__(self, message: str, yes: str) -> None:
        super().__init__()
        self.message, self.yes = message, yes

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(self.message)
            with Horizontal(id="confirm_buttons"):
                yield Button("Cancel", id="no")
                yield Button(self.yes, id="yes", variant="error")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()  # Safe default.

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_no(self) -> None:
        self.dismiss(False)


class Board(App):
    TITLE = "spook"
    AUTO_FOCUS = "#tree"
    CSS = """
    #status { height: auto; padding: 0 1; background: $boost; }
    #keys { dock: bottom; height: 1; padding: 0 1; background: $boost; color: $text-muted; }
    #help_box { padding: 0 1; }
    /* Pin the Help tab to the right end of the tab bar. */
    #--content-tab-tab_help { dock: right; }
    #tree, #agents { width: 50%; }
    #act_head { height: auto; padding: 0 1; color: $text-muted; border-bottom: solid $primary-darken-2; }
    #calls, #trash, #missions { width: 50%; }
    #mission_box { width: 50%; border-left: solid $primary-darken-2; padding: 0 1; }
    .narrow #missions { width: 100%; height: 40%; }
    .narrow #mission_box { width: 100%; height: 60%; border-left: none; border-top: solid $primary-darken-2; }
    #trash_box { width: 50%; border-left: solid $primary-darken-2; padding: 0 1; }
    .narrow #trash { width: 100%; height: 55%; }
    .narrow #trash_box { width: 100%; height: 45%; border-left: none; border-top: solid $primary-darken-2; }
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
        Binding("question_mark", "help", "Help"),
        Binding("r", "sync", "Re-analyze"),
        Binding("w", "agents", "Agents on Mission"),
        Binding("m", "missions", "Missions"),
        Binding("e", "mission_edit", "Edit Mission"),
        Binding("delete", "mission_delete", "Delete Mission"),
        Binding("g", "goto", "Jump to agent"),
        Binding("a", "assign", "Start agent"),
        Binding("c", "close_task", "Done"),
        Binding("n", "notes", "Notes"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("x", "hide_task", "Hide"),
        Binding("d", "toggle_hidden", "Show hidden"),
        Binding("t", "trash_task", "Trash"),
        Binding("u", "restore_task", "Restore"),
        Binding("X", "delete_forever", "Delete forever"),
    ]
    HELP = [
        ("Everywhere", [
            ("?", "Help", "Open this tab; press again to go back."),
            ("q / esc", "Close", "Close the board. Analysis keeps running in the background."),
            ("r", "Re-analyze", "Re-read every agent on a Mission and rebuild the task graphs now."),
            ("m", "Missions", "Go to the Missions tab."),
            ("w", "Agents on Mission", "Choose which agents are on this Mission. An agent can be on "
             "several Missions."),
        ]),
        ("Missions", [
            ("enter", "Open", "Make the selected Mission current and show its tasks."),
            ("n", "New", "Create a Mission: name and scope."),
            ("e", "Edit", "Change the selected Mission's name or scope."),
            ("w", "Agents", "Choose the selected Mission's agents."),
            ("delete", "Delete", "Delete the selected Mission (asks first; its folder is kept under "
             "missions/_deleted for recovery). Agents and their summaries are unaffected."),
        ]),
        ("Tasks", [
            ("↑ ↓ / enter", "Move / fold", "Move through concerns, tasks and alerts; enter folds a group."),
            ("s", "Sort", "Cycle how tasks are grouped: by concern; by status (blocked, unowned, "
             "in progress, waiting on dependencies, open, done, dropped); or as a dependency tree, "
             "each task with the tasks it depends on beneath it."),
            ("g", "Jump to agent", "Focus the agent that owns the task and type a prompt pointing it at the "
             "task's brief, including notes it hasn't seen (typed, not sent; only when the agent is idle, "
             "and not twice before it takes a turn). On an unowned task, or one whose agent's pane closed, "
             "offers to start one."),
            ("a", "Start agent", "Start a new Claude agent for the task. Confirm first; the prompt is typed, not sent."),
            ("c", "Done / reopen", "Mark the task done by hand, or reopen a done task. Again to undo. Your call wins over the model."),
            ("n", "Notes", "Add or delete private notes on the task, for agents working on it. "
             "Never sent to spook's models."),
            ("x", "Hide", "Hide a completed task from the board (display only)."),
            ("d", "Show hidden", "Show or hide the tasks you hid."),
            ("t", "Trash", "Move any task to the Trash tab. The model is told not to recreate it."),
        ]),
        ("Agents", [
            ("g", "Jump to agent", "Focus the selected agent's pane, pointing it at notes it hasn't seen "
             "on tasks it owns (typed, not sent)."),
        ]),
        ("Activity", [
            ("↑ ↓", "Inspect", "Select a background analysis call to see its timing, size, cost and errors."),
        ]),
        ("Trash", [
            ("u", "Restore", "Put the task back on the board."),
            ("X", "Delete forever", "Remove it from the trash (asks first). The model is no longer told about it."),
        ]),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.show_hidden = False
        self.graph: dict = {}
        self.records: dict[str, dict] = {}  # alias -> agent record
        self._sig = None
        self._agents_sig = None
        self._selected_key = None
        self._rebuilding = False
        self._concern_names: dict[str, str] = {}
        self.sort_mode = read_json(state_dir() / "board_prefs.json", {}).get("sort", "concern")
        if self.sort_mode not in SORT_MODES:
            self.sort_mode = "concern"

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with TabbedContent(initial="tab_tasks"):
            with TabPane("Missions", id="tab_missions"):
                with Horizontal(classes="split"):
                    yield DataTable(id="missions", cursor_type="row")
                    with VerticalScroll(id="mission_box"):
                        yield Markdown(id="mission_detail")
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
            with TabPane("Trash", id="tab_trash"):
                with Horizontal(classes="split"):
                    yield DataTable(id="trash", cursor_type="row")
                    with VerticalScroll(id="trash_box"):
                        yield Markdown(id="trash_detail")
            with TabPane("Help", id="tab_help"):
                with VerticalScroll(id="help_box"):
                    yield Markdown(self._help_md(), id="help")
        yield Static(Text.assemble("Commands are in the ", ("Help", "bold"), " tab · press ", ("?", "bold orange1")), id="keys")

    def on_mount(self) -> None:
        tree = self.query_one(Tree)
        tree.show_root = False
        t = self.query_one("#agents", DataTable)
        t.add_columns("id", "workspace", "status", "phase", "about")
        self.query_one("#calls", DataTable).add_columns("", "started", "what", "model", "time", "cost")
        self.query_one("#trash", DataTable).add_columns("id", "status", "title", "trashed")
        self.query_one("#missions", DataTable).add_columns("", "Mission", "agents", "open", "alerts", "updated")
        self._missions_sig = None
        self.trash: dict = {}
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
        # Boards closed with the pane (not q) never unregister; drop ones whose process is gone.
        panes = {k: v for k, v in panes.items() if activity._alive(v.get("pid"))}
        if on:
            panes[pane] = {"size": [self.size.width, self.size.height], "pid": os.getpid()}
        else:
            panes.pop(pane, None)
        write_json(p, panes)

    def on_resize(self, event) -> None:
        self.screen.set_class(event.size.width < self.NARROW_COLS, "narrow")

    def _help_md(self) -> str:
        out = []
        for section, rows in self.HELP:
            out.append(f"## {section}")
            out += [f"- **`{key}`**: {name} — {desc}" for key, name, desc in rows]
            out.append("")
        return "\n".join(out)

    FOCUS_IN_TAB = {"tab_missions": "#missions", "tab_tasks": "#tree", "tab_agents": "#agents", "tab_activity": "#calls",
                    "tab_trash": "#trash", "tab_help": "#help_box"}

    def _switch_tab(self, tab: str) -> None:
        # Move focus into the new tab first: if focus stays on a widget in the old tab,
        # Textual follows it and switches straight back.
        self.set_focus(None)
        self.query_one(TabbedContent).active = tab
        self.query_one(self.FOCUS_IN_TAB[tab]).focus()

    def action_help(self) -> None:
        tc = self.query_one(TabbedContent)
        if tc.active == "tab_help":
            self._switch_tab(getattr(self, "_before_help", "tab_tasks"))
        else:
            self._before_help = tc.active
            self._switch_tab("tab_help")

    # ---------------------------------------------------------------- data

    @staticmethod
    def _mtimes(files):
        return tuple((str(f), f.stat().st_mtime if f.exists() else 0) for f in files)

    def poll(self) -> None:
        d = state_dir()
        mid = missions.active()
        if mid != getattr(self, "_mission", None):  # Switched Missions: start fresh.
            self._mission = mid
            self._sig = self._agents_sig = None
            self._selected_key = None
            self.show_hidden = False
        md = missions.mission_dir(mid)
        # Agent files change constantly during analysis; only redraw the tree when the graph does.
        agents_sig = self._mtimes([md / "agents.json", *sorted((d / "agents").glob("*.json"))])
        if agents_sig != self._agents_sig:
            self._agents_sig = agents_sig
            self.records = {r["alias"]: r for r in mission_records(mid)}
            self._render_agents()
        sig = self._mtimes([d / "missions.json", md / "graph.json", md / "agents.json", md / "trash.json",
                            md / "status_overrides.json", md / "task_notes.json", md / "hidden_tasks.json",
                            md / "added_tasks.json", md / "task_edits.json"])
        if sig != self._sig:
            self._sig = sig
            self.graph = read_json(md / "graph.json", {})
            self.overrides = load_overrides()
            self.added = load_added()
            self.edits = load_edits()
            self.task_notes = notes.load()
            self.trash = load_trash()
            self._render_tree()
            self._render_trash()
        all_m = list(missions.all_missions())
        msig = self._mtimes([d / "missions.json"] + [state_dir() / "missions" / m / f
                                                     for m in all_m for f in ("graph.json", "agents.json")])
        if msig != self._missions_sig:
            self._missions_sig = msig
            self._render_missions()
        status = read_json(state_dir() / "status.json", {})
        self.calls, self.syncs = activity.load()
        self._render_status(status)
        self._render_activity(status)

    def _render_status(self, s: dict) -> None:
        g = self.graph
        n_t = len([x for x in g.get("tasks", []) if x["status"] not in ("done", "dropped")])
        n_done = len([x for x in g.get("tasks", []) if x["status"] == "done"])
        n_a = len(g.get("alerts", []))
        name = missions.info()["name"]
        left = Text.assemble((name, "bold"), f" · {len(self.records)} agents · {n_t} open · {n_done} done · {n_a} alerts")
        if self.sort_mode != "concern":
            left.append(f" · by {self.sort_mode}")
        if not missions.members():
            left = Text.assemble((name, "bold"), " · no agents yet: press w to add some")
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
        icon, color = self._icon(t, by_id)
        finished = t["status"] in ("done", "dropped")
        title_style = "grey62" if finished else ""
        if t["id"] in self.hidden:
            title_style = "grey42 italic"
        txt = Text.assemble((f"{icon} ", f"bold {color}"), (f"{t['id']} ", "bold"), (t["title"], title_style))
        if t.get("projects"):
            txt.append(f"  · {', '.join(t['projects'])}", style="grey62")
        if self.sort_mode != "concern":
            c = self._concern_names.get(t.get("concern_id"))
            if c:
                txt.append(f"  ◆ {c}", style="grey50")
        waiting = [d for d in t.get("depends_on", []) if by_id.get(d, {}).get("status") not in ("done", "dropped")]
        if waiting:
            txt.append(f"  ← {', '.join(waiting)}", style="yellow")
        if not t.get("owners") and not finished:
            txt.append("  unowned", style="magenta")
        if t["id"] in self.hidden:
            txt.append("  hidden", style="grey42")
        ov = getattr(self, "overrides", {}).get(t["id"])
        if ov:
            txt.append(f"  ({ov.get('by') or 'you'})", style="grey50 italic")
        n = len(getattr(self, "task_notes", {}).get(t["id"], []))
        if n:
            txt.append(f"  ✎{n}", style="khaki1")
        return txt

    def _render_tree(self) -> None:
        tree = self.query_one(Tree)
        keep = self._current_key()
        self._rebuilding = True  # Ignore the highlight events clear() and rebuild fire.
        tree.clear()
        g = self.graph
        # Hiding only applies while a task is finished; if it reopens, it shows again.
        tasks = [
            t for t in g.get("tasks", [])
            if self.show_hidden or t["id"] not in self.hidden or t["status"] not in ("done", "dropped")
        ]
        by_id = {t["id"]: t for t in g.get("tasks", [])}

        alerts = g.get("alerts", [])
        if alerts:
            an = tree.root.add(Text(f"Alerts ({len(alerts)})", style="bold yellow"), data=("alerts", None), expand=True)
            order = {"critical": 0, "warn": 1, "info": 2}
            for i, a in sorted(enumerate(alerts), key=lambda x: order.get(x[1]["severity"], 3)):
                icon, style = SEV.get(a["severity"], ("?", ""))
                an.add_leaf(Text.assemble((f"{icon} ", style), a["text"]), data=("alert", i))

        concerns = {c["id"]: c for c in g.get("concerns", [])}
        departed = [t for t in tasks if t.get("departed")]
        tasks = [t for t in tasks if not t.get("departed")]
        self._concern_names = {k: c["name"] for k, c in concerns.items()}
        if self.sort_mode == "status":
            self._render_by_status(tree, tasks, by_id)
        elif self.sort_mode == "dependencies":
            self._render_by_dependencies(tree, tasks, by_id)
        else:
            self._render_by_concern(tree, tasks, by_id, concerns)

        if departed:
            node = tree.root.add(Text("Agents removed from this Mission", style="grey50 italic"),
                                 data=("concern", "_departed"), expand=True)
            for t in departed:
                lbl = self._task_label(t, by_id)
                lbl.stylize("grey50")
                node.add_leaf(lbl, data=("task", t["id"]))
        if not tasks and not alerts and not departed:
            tree.root.add_leaf(Text("Nothing yet. Add agents with w; analysis runs when they finish a turn.", style="italic"))
        self.call_after_refresh(self._finish_rebuild, keep)

    def _render_by_concern(self, tree, tasks, by_id, concerns) -> None:
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
            for t in sorted(items, key=lambda t: (list(ICON).index(self._status_group(t, by_id)), _task_num(t["id"]))):
                node.add_leaf(self._task_label(t, by_id), data=("task", t["id"]))

    def _icon(self, t: dict, by_id: dict) -> tuple[str, str]:
        return ICON.get(self._status_group(t, by_id), ("?", "white"))

    @staticmethod
    def _status_group(t: dict, by_id: dict) -> str:
        st = t["status"]
        if st in ("blocked", "done", "dropped"):
            return st
        if not t.get("owners"):
            return "unowned"
        if st == "in_progress":
            return "in_progress"
        if any(by_id.get(d, {}).get("status") not in ("done", "dropped") for d in t.get("depends_on", [])):
            return "waiting"
        return "open"

    def _render_by_status(self, tree, tasks, by_id) -> None:
        groups: dict[str, list] = {}
        for t in tasks:
            groups.setdefault(self._status_group(t, by_id), []).append(t)
        for gid, name, color in STATUS_GROUPS:
            items = groups.get(gid, [])
            if not items:
                continue
            label = Text.assemble((f"{name} ", f"bold {color}"), (f"({len(items)})", "grey62"))
            node = tree.root.add(label, data=("status", gid), expand=True)
            for t in sorted(items, key=lambda t: _task_num(t["id"])):
                node.add_leaf(self._task_label(t, by_id), data=("task", t["id"]))

    def _render_by_dependencies(self, tree, tasks, by_id) -> None:
        """Each task with the tasks it depends on nested beneath it (like a package
        dependency tree). Roots are tasks nothing on the board depends on. A task
        needed by several others is expanded once; later mentions point back to it."""
        visible = {t["id"]: t for t in tasks}
        deps = {tid: [d for d in t.get("depends_on", []) if d in visible and d != tid] for tid, t in visible.items()}
        needed = {d for ds in deps.values() for d in ds}
        order = lambda tid: (list(ICON).index(self._status_group(visible[tid], by_id)), _task_num(tid))
        shown: set[str] = set()

        def add(parent, tid, path):
            t = visible[tid]
            label = self._task_label(t, by_id)
            if tid in path:
                label.append("  ↺ cycle", style="red")
                parent.add_leaf(label, data=("task", tid))
                return
            if tid in shown and deps[tid]:
                label.append("  ↑ expanded above", style="grey50 italic")
                parent.add_leaf(label, data=("task", tid))
                return
            shown.add(tid)
            if not deps[tid]:
                parent.add_leaf(label, data=("task", tid))
                return
            node = parent.add(label, data=("task", tid), expand=True)
            for d in sorted(deps[tid], key=order):
                add(node, d, path | {tid})

        roots = [tid for tid in visible if deps[tid] and tid not in needed]
        for tid in sorted(roots, key=order):
            add(tree.root, tid, set())
        # Tasks only reachable through a cycle have no root; start from them directly.
        for tid in sorted(visible, key=order):
            if deps[tid] and tid not in shown:
                add(tree.root, tid, set())
        alone = [tid for tid in visible if not deps[tid] and tid not in needed]
        if alone:
            node = tree.root.add(Text.assemble(("No dependencies ", "bold grey62"), (f"({len(alone)})", "grey62")),
                                 data=("deps", "alone"), expand=True)
            for tid in sorted(alone, key=order):
                node.add_leaf(self._task_label(visible[tid], by_id), data=("task", tid))

    def action_cycle_sort(self) -> None:
        if self.query_one(TabbedContent).active != "tab_tasks":
            return
        i = SORT_MODES.index(self.sort_mode)
        self.sort_mode = SORT_MODES[(i + 1) % len(SORT_MODES)]
        p = state_dir() / "board_prefs.json"
        write_json(p, {**read_json(p, {}), "sort": self.sort_mode})
        self.notify(f"Tasks sorted by {self.sort_mode}")
        self._render_tree()

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
        elif kind == "deps":
            md.update("## No dependencies\n\nTasks that neither depend on another task on the board "
                      "nor are depended on.")
        elif kind == "status":
            name = next((n for gid, n, _ in STATUS_GROUPS if gid == key), key)
            ts = [t for t in g.get("tasks", []) if self._status_group(t, by_id) == key]
            md.update(f"## {name}\n\n" + "\n".join(
                f"- {self._icon(t, by_id)[0]} **{t['id']}**: {t['title']}" for t in sorted(ts, key=lambda t: _task_num(t["id"]))))
        elif kind == "concern":
            c = next((c for c in g.get("concerns", []) if c["id"] == key), None)
            if c:
                ts = [t for t in g.get("tasks", []) if t.get("concern_id") == key]
                projects = sorted({p for t in ts for p in t.get("projects", [])})
                md.update(
                    f"## {c['name']}\n\n{c['description']}\n\n**Projects**: {', '.join(projects) or '—'}\n\n"
                    + "\n".join(f"- {self._icon(t, by_id)[0]} **{t['id']}**: {t['title']}" for t in ts)
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
        finished = ("done", "dropped")
        waiting = [d for d in t.get("depends_on", []) if by_id.get(d, {}).get("status") not in finished]
        met = [d for d in t.get("depends_on", []) if by_id.get(d, {}).get("status") in finished]
        if waiting:
            out += ["", "### Waiting on"]
            out += [f"- **{d}**: {by_id[d]['title']} ({by_id[d]['status']})" if d in by_id else f"- {d}" for d in waiting]
        if met:
            out += ["", "### Done dependencies"]
            out += [f"- {ICON[by_id[d]['status']][0]} **{d}**: {by_id[d]['title']} ({by_id[d]['status']})" for d in met]
        if blocks:
            out += ["", "### Blocks"] + [f"- **{b['id']}**: {b['title']} ({b['status']})" for b in blocks]
        mine = getattr(self, "task_notes", {}).get(t["id"], [])
        if mine:
            out += ["", "### Your notes (private)"]
            out += [f"- {time.strftime('%m-%d %H:%M', time.localtime(n['at']))}: {n['text']}" for n in mine]
        ov = getattr(self, "overrides", {}).get(t["id"])
        if ov:
            verb = "Marked done" if ov["status"] == "done" else "Reopened"
            if ov.get("by"):
                out += ["", f"_{verb} by {self._owner_label(ov['by'])} {_ago(ov['at'])} with `spook task "
                            f"{'done' if ov['status'] == 'done' else 'reopen'}` (c to change)_"]
            else:
                out += ["", f"_{verb} by you {_ago(ov['at'])} (c to undo)_"]
        ed = getattr(self, "edits", {}).get(t["id"])
        if ed:
            who = sorted({self._owner_label(e["by"]) if e.get("by") else "you" for e in ed.values()})
            names = ", ".join(f.replace("depends_on", "dependencies") for f in ed)
            out += ["", f"_{names.capitalize()} set by {', '.join(who)} with `spook task update`; the model "
                        f"keeps {'it' if len(ed) == 1 else 'them'} (`--release` hands back)._"]
        added = getattr(self, "added", {}).get(t["id"])
        if added:
            out += ["", f"_Added by {self._owner_label(added['added_by']) if added['added_by'] != 'the human' else 'you'} "
                        f"{_ago(added['at'])} with `spook task add`; the model keeps it._"]
        if t.get("evidence"):
            out += ["", "### Evidence", t["evidence"]]
        if t.get("owners"):
            out += ["", "_g: jump to owner agent_"]
        return "\n".join(out)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "missions":
            self._show_mission(event.row_key.value if event.row_key else None)
            return
        if event.data_table.id == "trash":
            tid = event.row_key.value if event.row_key else None
            e = self.trash.get(tid)
            md = self.query_one("#trash_detail", Markdown)
            if e:
                t = e["task"]
                md.update("\n".join([
                    f"## {tid} · {t['title']}",
                    f"**Status**: {t['status']}  ·  **Projects**: {', '.join(t.get('projects', [])) or '—'}",
                    "",
                    f"**Trashed**: {_ago(e['trashed_at'])}",
                    "",
                    t.get("detail", ""),
                    "",
                    "_u: restore to the board · X: delete forever_",
                ]))
            return
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

    # ---------------------------------------------------------------- Missions tab

    def _render_missions(self) -> None:
        table = self.query_one("#missions", DataTable)
        keep = table.cursor_row
        table.clear()
        act = missions.active()
        for mid, m in missions.all_missions().items():
            g = read_json(missions.mission_dir(mid) / "graph.json", {})
            tasks = g.get("tasks", [])
            open_ = len([t for t in tasks if t["status"] not in ("done", "dropped")])
            table.add_row(
                Text("●", style="bold green") if mid == act else "",
                Text(m["name"], style="bold" if mid == act else ""),
                str(len(missions.members(mid))),
                str(open_),
                str(len(g.get("alerts", []))),
                _ago(g.get("updated_at")) if g.get("updated_at") else "—",
                key=mid,
            )
        if table.row_count:
            table.move_cursor(row=min(keep, table.row_count - 1))

    def _mission_cursor(self) -> str | None:
        table = self.query_one("#missions", DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def _show_mission(self, mid: str | None) -> None:
        md = self.query_one("#mission_detail", Markdown)
        if not mid or mid not in missions.all_missions():
            md.update("")
            return
        m = missions.all_missions()[mid]
        names = {k: v["name"] for k, v in missions.all_missions().items()}
        g = read_json(missions.mission_dir(mid) / "graph.json", {})
        recs = mission_records(mid)
        out = [f"## {m['name']}" + ("  (current)" if mid == missions.active() else ""), "",
               "**Scope**: " + (m.get("scope") or "_none: all of the agents' work is included_"), "",
               "### Agents"]
        if not recs and not missions.members(mid):
            out.append("_None yet. Press w to add some._")
        for r in recs:
            others = [names[o] for o in missions.missions_for_session(r["session_id"]) if o != mid]
            state = "closed" if r.get("gone") else (r.get("agent_status") or "?")
            line = f"- **{r['alias']}**: {r['record'].get('title', '')} · {r['workspace']} · {state}"
            if others:
                line += f" · also on: {', '.join(others)}"
            out.append(line)
        waiting = len(missions.members(mid)) - len(recs)
        if waiting > 0:
            out.append(f"- _{waiting} more not analyzed yet_")
        concerns = g.get("concerns", [])
        if concerns:
            out += ["", "### Concerns"]
            for c in concerns:
                n = len([t for t in g.get("tasks", []) if t.get("concern_id") == c["id"]
                         and t["status"] not in ("done", "dropped")])
                out.append(f"- {c['name']}: {n} open" + (" · crosscutting" if c.get("crosscutting") else ""))
        out += ["", "_enter: open · n: new · e: edit · w: agents · delete: delete_"]
        md.update("\n".join(out))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "missions" or not event.row_key:
            return
        missions.set_active(event.row_key.value)
        self.poll()
        self._switch_tab("tab_tasks")
        self.notify(f"Mission: {missions.info()['name']}")

    def _on_missions_tab(self) -> bool:
        return self.query_one(TabbedContent).active == "tab_missions"

    def action_missions(self) -> None:
        self._switch_tab("tab_missions")

    def action_agents(self) -> None:
        mid = self._mission_cursor() if self._on_missions_tab() else None
        self.push_screen(AgentPicker(mid), lambda _: self.poll())

    def _mission_new(self) -> None:
        def made(res):
            if res:
                missions.create(res["name"], res["scope"])
                self.poll()
                self.notify(f"Mission '{res['name']}' created. Press w to add agents.")
        self.push_screen(MissionForm("New Mission"), made)

    def action_mission_edit(self) -> None:
        if not self._on_missions_tab():
            return
        mid = self._mission_cursor()
        if not mid:
            return
        m = missions.all_missions()[mid]

        def saved(res):
            if res:
                missions.update(mid, name=res["name"], scope=res["scope"])
                self._missions_sig = None
                self.poll()
                self._show_mission(mid)
        self.push_screen(MissionForm(f"Edit Mission '{m['name']}'", m["name"], m.get("scope", "")), saved)

    def action_mission_delete(self) -> None:
        if not self._on_missions_tab():
            return
        mid = self._mission_cursor()
        if not mid:
            return
        if len(missions.all_missions()) == 1:
            self.notify("Can't delete the only Mission", severity="warning")
            return
        name = missions.all_missions()[mid]["name"]

        def done(yes: bool) -> None:
            if yes and missions.delete(mid):
                self.notify(f"Mission '{name}' deleted")
                self._missions_sig = None
                self.poll()
        self.push_screen(Confirm(
            f"Delete Mission '{name}'?\n\nIts task graph, notes, trash and decisions go with it. "
            "Agents and their summaries are not affected. The folder is kept in the state "
            "directory under missions/_deleted, so it can be recovered by hand.",
            "Delete Mission",
        ), done)

    def action_close_task(self) -> None:
        if self.query_one(TabbedContent).active != "tab_tasks":
            return
        key = self._current_key()
        if not key or key[0] != "task":
            self.notify("Select a task first", severity="warning")
            return
        new = toggle_done(key[1])
        if new:
            self.notify(f"{key[1]} marked done" if new == "done" else f"{key[1]} reopened ({new})")
            self.poll()

    def _render_trash(self) -> None:
        table = self.query_one("#trash", DataTable)
        keep = table.cursor_row
        table.clear()
        for tid, e in sorted(self.trash.items(), key=lambda kv: -kv[1]["trashed_at"]):
            t = e["task"]
            icon, color = ICON.get(t["status"], ("?", "white"))
            table.add_row(tid, Text(icon, style=color), t["title"], _ago(e["trashed_at"]), key=tid)
        if table.row_count:
            table.move_cursor(row=min(keep, table.row_count - 1))
        else:
            self.query_one("#trash_detail", Markdown).update(
                "_Trash is empty. Press t on a task to move it here._"
            )

    def _trash_cursor(self) -> str | None:
        table = self.query_one("#trash", DataTable)
        if self.query_one(TabbedContent).active != "tab_trash" or not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def action_trash_task(self) -> None:
        if self.query_one(TabbedContent).active != "tab_tasks":
            return
        key = self._current_key()
        if not key or key[0] != "task":
            self.notify("Select a task first", severity="warning")
            return
        self.query_one(Tree).action_cursor_down()  # Land on a neighbour after it's gone.
        if trash_task(key[1]):
            self.notify(f"{key[1]} moved to Trash (u restores it there)")
        self.poll()

    def action_restore_task(self) -> None:
        tid = self._trash_cursor()
        if tid and restore_task(tid):
            self.notify(f"{tid} restored to the board")
            self.poll()

    def action_delete_forever(self) -> None:
        tid = self._trash_cursor()
        if not tid:
            if self.query_one(TabbedContent).active != "tab_trash":
                self.notify("Delete forever only works in the Trash tab", severity="warning")
            return
        title = self.trash.get(tid, {}).get("task", {}).get("title", "")

        def done(yes: bool) -> None:
            if yes and delete_forever(tid):
                self.notify(f"{tid} deleted")
                self.poll()

        self.push_screen(Confirm(
            f"Permanently delete {tid}: {title}?\n\n"
            "It leaves the trash, and the model is no longer told about it, "
            "so it may come back if an agent works on it again.",
            "Delete forever",
        ), done)

    @property
    def hidden(self) -> set[str]:
        return set(read_json(missions.mission_dir() / "hidden_tasks.json", []))

    def action_hide_task(self) -> None:
        key = self._current_key()
        task = next((t for t in self.graph.get("tasks", []) if key and key[0] == "task" and t["id"] == key[1]), None)
        if not task:
            self.notify("Select a task first", severity="warning")
            return
        hidden = self.hidden
        if task["id"] in hidden:
            hidden.discard(task["id"])
            self.notify(f"{task['id']} unhidden")
        elif task["status"] in ("done", "dropped"):
            hidden.add(task["id"])
            self.notify(f"{task['id']} hidden (d shows hidden tasks)")
        else:
            self.notify("Only completed tasks can be hidden", severity="warning")
            return
        # Move off the row first so the cursor lands on a neighbour, not the top.
        if not self.show_hidden and task["id"] in hidden:
            self.query_one(Tree).action_cursor_down()
        write_json(missions.mission_dir() / "hidden_tasks.json", sorted(hidden))
        self._render_tree()

    def action_toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self._render_tree()

    def action_goto(self) -> None:
        alias = None
        on_agents = self.query_one(TabbedContent).active == "tab_agents"
        if on_agents:
            table = self.query_one("#agents", DataTable)
            if table.row_count:
                alias = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        else:
            t = self._selected_task()
            owners = t.get("owners", []) if t else []
            # Prefer an owner whose pane is still open.
            alias = next((o for o in owners if not self.records.get(o, {}).get("gone")), owners[0] if owners else None)
        r = self.records.get(alias) if alias else None
        task = self._selected_task()
        if task and task.get("departed"):
            self.notify(f"{task['id']}'s agent isn't on this Mission anymore. Add it back with w, "
                        "or press a to start a new agent.", severity="warning")
            return
        if not r:
            if task:
                self.action_assign()  # Unowned task: offer to start one.
            else:
                self.notify("Select a task or an alert to jump to its agent", severity="warning")
            return
        if r.get("gone"):
            # The owner's pane has closed: nothing to deliver to or focus.
            if task and not on_agents:
                self.notify(f"{alias}'s pane is closed. Start a new agent for {task['id']}? "
                            "Its brief will include your notes.", severity="warning")
                self.action_assign()
            else:
                self.notify(f"{alias}'s pane is closed.", severity="warning")
            return
        owned = [t for t in self.graph.get("tasks", []) if alias in t.get("owners", [])]
        try:
            if task and not on_agents:
                # A second g on the same task right after a "pending" warning types anyway.
                again = getattr(self, "_g_warned", None) == (r["pane_id"], task["id"])
                result = launch.point_at_task(r, task, owned, self.graph, force=again)
                self._g_warned = (r["pane_id"], task["id"]) if result.startswith("pending:") else None
            else:
                result = launch.deliver_notes(r["pane_id"], owned, self.graph)
        except Exception as e:
            result = f"error:{e}"
        if result == "typed":
            what = f"at {task['id']}" if task and not on_agents else "at your new notes"
            self.notify(f"Typed a prompt into {alias} pointing it {what}. Review it and press Enter.")
        elif result.startswith("pending:"):
            self.notify(f"{alias} already has a prompt for {result[8:]} typed and not sent. Send or clear it, "
                        f"or press g again to type the one for {task['id']} anyway.", severity="warning")
        elif result == "already":
            self.notify(f"The prompt for {task['id']} is already typed in {alias}.")
        elif result.startswith("busy:"):
            self.notify(f"{alias} is {result[5:]}; nothing typed. Press g again when it's idle.", severity="warning")
        elif result.startswith("error:"):
            self.notify(f"Couldn't type the prompt: {result[6:]}", severity="error")
        herdr.focus_pane(r["pane_id"])
        if not os.environ.get("HERDR_PANE_ID"):  # Popup: get out of the way. Docked: stay.
            self.exit()

    def action_notes(self) -> None:
        if self._on_missions_tab():
            self._mission_new()  # n means "new" on the Missions tab.
            return
        if self.query_one(TabbedContent).active != "tab_tasks":
            return
        task = self._selected_task()
        if not task:
            self.notify("Select a task first", severity="warning")
            return
        self.push_screen(NotesEditor(task), lambda _: self.poll())

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
