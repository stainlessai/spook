# spook

A [herdr](https://herdr.dev) plugin for people running many coding agents at
once on work that cuts across projects.

Spook reads the Claude Code conversations of the agents you choose and
summarizes each one. It groups them into **Missions**, and builds a task board
per Mission with concerns, tasks, dependencies and alerts. The board docks on
the right of your herdr tab.

It watches. It never types into an existing agent's pane, changes your repos
or pops up notifications. The one exception is starting a new agent for an
unowned task, which happens only after you confirm it (see below).

## What you get

- **Tasks:** work grouped into concerns, with crosscutting ones (spanning more
  than one project) marked. Each task shows its status, projects, owning
  agent, what it waits on, what it blocks and the evidence for it.
  Completed tasks stay on the board with a green ✓ until you hide them.
- **Alerts:** what needs your attention:
  - needs that no agent owns
  - two agents changing the same thing
  - a change that another agent probably doesn't know about
  - agents blocked on each other
- **Agents:** each agent's goal, current summary, work items, what it needs
  from other projects, what it provides to them, and open questions.
- **Activity:** every background analysis call, live, with timing, cost,
  errors and the queue.
- **Trash:** tasks you took off the board. Restore them, or delete them for
  good.

## How it works

```
agent finishes a turn
  → herdr fires pane.agent_status_changed
  → spook hook: is this agent on a Mission? queue the pane
  → worker (one at a time; bursts are batched):
      1. read only the new lines of that agent's transcript
         (~/.claude/projects/<dir>/<session-id>.jsonl)
      2. digest them: user asks, agent replies, files edited, commands run
      3. claude -p  → update that agent's summary
      4. claude -p  → rebuild the task graph of each Mission the agent is on
  → the board picks up the change within a second
```

- Herdr reports each agent's Claude session id, and spook uses it to find the
  agent's transcript file.
- Transcripts are mostly tool output. The digest drops that and is typically
  under 3% of the file's size.
- On first contact with a long conversation, spook reads only its 3 most
  recent chunks, plus the conversation's first message for context.
- Analysis runs through headless Claude Code (`claude -p`) with no tools and
  no saved session. Each call can only read the text it's given and return
  JSON.

## Requirements

- herdr ≥ 0.9
- [uv](https://docs.astral.sh/uv/) (installs Python 3.11+ and Textual on
  first run)
- Claude Code (`claude`) logged in
- macOS or Linux

## Install

```sh
git clone <this repo> spook
herdr plugin link ./spook
```

Bind a key to toggle the board in `~/.config/herdr/config.toml`
(`prefix+s` is taken by herdr's settings):

```toml
[[keys.command]]
key = "prefix+shift+s"
type = "plugin_action"
command = "spook.open"
description = "spook board"
```

```sh
herdr server reload-config
```

## Use

1. Press your key (or run `herdr plugin action invoke spook.open`). The board
   docks on the right of the current tab. Press it again to close it.
2. Press `w`, tick the agents to put on the current Mission, then press `s`.
   The first analysis starts right away. Follow it in the status bar or the
   Activity tab.
3. After that, spook updates whenever one of those agents finishes a turn.
4. Press `m` for the Missions tab, where you create Missions and switch between
   them.

| Key | Action |
| --- | --- |
| `?` | Open the Help tab (again to go back) |
| `m` | Go to the Missions tab |
| `enter` | Missions tab: make the selected Mission current and show its tasks |
| `n` | Missions tab: new Mission (on the Tasks tab, `n` is notes) |
| `e` | Missions tab: edit the selected Mission's name and scope |
| `delete` | Missions tab: delete the selected Mission (asks first) |
| `w` | Choose the agents on this Mission (`space` toggles, `s` saves, `esc` cancels) |
| `r` | Re-analyze every agent on a Mission now |
| `g` | Jump to the agent behind the selected task or row, typing (not sending) a prompt that points it at the task (on an unowned task, offers to start one) |
| `a` | Start an agent for the selected task |
| `c` | Mark the selected task done by hand, or reopen a done task (again to undo) |
| `n` | Add or delete private notes on the selected task |
| `s` | Cycle the task sort: by concern; by status (blocked, unowned, in progress, waiting on dependencies, open, done, dropped); as a dependency tree (each task with the tasks it depends on beneath it); or by your tags (a task with several tags shows under each, untagged ones last) |
| `#` | Tag the task: one-word tags of your own, with existing tags suggested as you type |
| `f` | Show only the tasks with one of your tags, or untagged ones (`f` again to change or clear) |
| `x` | Hide the selected completed task (on a hidden one: unhide it) |
| `d` | Show or hide hidden tasks |
| `t` | Move the selected task to the Trash tab |
| `u` | Trash tab: restore the selected task to the board |
| `X` | Trash tab: delete the selected task permanently (asks first) |

All commands, with short descriptions, are in the **Help** tab (`?` opens
it and goes back). The bottom bar points there.

### Missions

A Mission groups the agents working toward one goal, such as "Prod migration"
or "Security review". It holds:

- **Agents:** chosen one by one with `w`. An agent can be on several Missions.
  It's summarized once, and each Mission uses that summary.
- **Scope:** a short description. The Mission's task graph includes only the
  agents' work that fits it, so an agent working on two things shows up on two
  Missions without mixing them. With no scope, everything is included.
- **Its own task graph and your decisions:** notes, trash, manual statuses,
  hidden tasks and briefs. They belong to that Mission only.

Each Mission has an id: M1, M2 and so on, in creation order and never reused. The command line always refers to
Missions by id (`--mission M2`, `mission use M2`), never by name,
since names can change and needn't be unique. `spook mission list` shows them. Missions created before ids
looked like this are renamed once, automatically. Their folder keeps a link
under the old name, so brief paths agents already have still work.

The board shows one Mission at a time. The status line starts with its name.
The **Missions** tab lists them all, with agents, open tasks, alerts and when
each was last updated. Selecting one shows its scope, its agents (and which
other Missions they're on) and its concerns. `w` on that tab edits the
selected Mission's agents.

- **Agents that leave a Mission:** if you remove an agent, its tasks stay on
  the Mission under "Agents removed from this Mission", with your notes and
  statuses still attached. They return to normal if you add the agent back.
  Spook never empties a Mission on its own.
- **New sessions in the same pane:** after `/clear`, or restarting Claude in a
  pane, the new session takes the old one's place on its Missions and keeps
  its alias.
- **New agents aren't added automatically.** Add them with `w`. Agents you
  start from the board with `a` join the current Mission.
- **Deleting a Mission** (Missions tab, `delete`) asks first. Its folder moves to
  `missions/_deleted/` in the state directory, so it can be recovered by hand.

Upgrading from 1.0: the first run moves your existing board into a Mission
called "Default". It adds the agents from the workspaces you were tracking,
and keeps all your data.

### Marking tasks done by hand

Press `c` to mark a task done, for example when the work happened outside the
Mission's agents. On a done task, `c` reopens it. Pressing `c` again undoes your
change. Tasks you set this way show "(you)".

Your status decisions win. The model is told about them and keeps them. If an
agent later works on a task you closed, the model raises an alert and leaves
the status alone. Only you change it back.

Agents can do the same with `spook task done ID` and `spook task reopen ID`
(see below). Those tasks show the agent's id, e.g. "(A4)", instead of "(you)".

### Notes for agents

Press `n` on a task to add private notes, e.g. "use the staging bucket, not
prod". Notes are never sent to spook's analysis models. They reach agents
working on the task:

- **Starting an agent with `a`:** the brief includes a "Notes from the human"
  section.
- **Jumping to an agent with `g`:** spook refreshes the task's brief and types
  a one-line prompt into the agent's input box pointing it at the task, and at
  any notes it hasn't seen on its tasks, without sending it. It only types when
  the agent is idle; if the agent is busy, it says so and types nothing.
  Pressing `g` again before the agent has taken a turn doesn't type it twice.
  On the Agents tab, `g` only types a prompt when there are unseen notes.

Known gap: switching to an agent's tab yourself doesn't deliver notes. Use `g`.

### Tags

Press `#` on a task to tag it with single words of your own, like `urgent` or
`infra`. As you type, existing tags are listed below the input (most used
first, from every Mission); press ↓ and enter to reuse one. Typing a tag that
exists with different capitals reuses it as is. Press ↑ to reach the task's
tags and delete to remove one. Tags show after the task's title and in its
details. Like notes, they're private: never sent to spook's models.

To focus on one tag, press `f` and pick it (or "Untagged"). The board then
shows only those tasks, and alerts about them, in whichever sort you're
using. The status line shows the filter. Press `f` again and pick "All tasks"
to clear it. Switching Missions clears it too. `s` also cycles to a view
grouped by tag.

### Hiding, trash and delete

- **Hide** (`x`) is for completed tasks you don't need to see. It's a display
  setting only. The model is unaffected, and a hidden task reappears if it
  reopens.
- **Trash** (`t`) takes any task off the board without losing it. The model is
  told what's in the trash and asked not to recreate those tasks. If it outputs
  one anyway, spook keeps it in the trash and updates the trash copy. Restore
  it with `u`.
- **Delete forever** (`X`) works only in the Trash tab. It removes the task
  from the trash, so the model is no longer told about it. If agents keep
  working on it, it may come back as a new task.
| `q` | Close the board |

### Adding tasks from an agent

Agents can put a task on the board themselves, so you can just tell one
"add a spook task for that":

```sh
spook task add "Rotate the staging keys" --depends T3 --concern billing-api --detail "after the cutover"
spook task done T7                      # mark it done (spook task reopen T7 to undo)
spook task update T7 --status blocked --add-depends T12 --owners A3
spook task update T7 --release status   # hand a field back to the model
spook task list                         # ids, statuses, owners, dependencies
```

- Without `--mission`, the task goes on every Mission the calling agent's pane
  is on. Spook knows the pane from herdr's `HERDR_PANE_ID`. From a pane whose
  agent is on no Mission, it refuses and asks for `--mission ID`. Outside
  herdr, it uses the active Mission.
- The task shows up on the board right away. The model can give it owners, a
  status, a concern and more dependencies, but it can't drop it, rename it or
  remove the dependencies it was added with. Dependency ids that aren't on
  the board are ignored, with a warning.
- Trash, restore and delete forever work as for any task.
- `spook task done` and `spook task reopen` work on any task on the agent's
  Missions. They count like your own `c`: the model keeps the status and
  raises an alert if later work contradicts it. The board shows who set it,
  and `c` on the board overrides an agent's choice.
- `spook task update ID` sets any of `--title`, `--detail`, `--status`,
  `--concern`, `--owners` and the dependencies (`--depends` replaces them,
  `--add-depends` and `--remove-depends` adjust them). Like a manual status,
  the model keeps what was set, and the task's details on the board say who
  set it. `--release title,depends,...` hands fields back to the model.
  Unknown task or agent ids are ignored, with a warning.

`spook install-cli` writes a small `spook` command to `~/.local/bin` so agents
can run it from any directory. To tell agents about it, add something like
this to a `CLAUDE.md` they read:

```markdown
## Task board
This work is tracked on a spook board. When something needs doing that isn't
part of your current task (often in another project), add it with
`spook task add "TITLE" [--depends T3,T4] [--concern NAME] [--detail TEXT]`
instead of only mentioning it. When you finish a task on the board, run
`spook task done ID`; when a task's status, dependencies or owners change,
update it with `spook task update ID --status blocked --add-depends T5 ...`.
`spook task list` shows the current tasks.
```

### Starting an agent for an unowned task

Press `a` on a task, or `g` on a task nobody owns. A popup shows:

- **Directory:** guessed from the task's projects and the repos this
  Mission's agents use. You can edit it.
- **Where:** a new tab in any workspace, or a new workspace.
- **Prompt:** an editable prompt pointing the new agent at a brief file.

Nothing happens until you press **Start agent**. Spook then:

1. Writes the brief to `briefs/<task>.md` in its state directory. The brief
   holds the task, what it waits on, what waits on it, related alerts, and
   related agents' summaries, needs and provides.
2. Opens the tab or workspace and starts `claude` in it.
3. Types the prompt into Claude's input box **without sending it**. Review it
   and press Enter yourself.

If Claude stops on a startup question, such as whether to trust a folder
it hasn't seen, spook switches you to that tab. It waits up to 3 minutes for
you to answer and only types once Claude is ready. It never types into a
dialog. The new agent joins the current Mission. Spook records which task it
was started for, so the graph makes it the task's owner after its first turn.

There are also herdr actions: **add this workspace's agents to the active
Mission**, **remove this workspace's agents from the active Mission** and
**re-analyze now**.

From a shell:

```sh
bin/spook status                        # Missions, agents, graph counts, worker state
bin/spook sync                          # same as r
bin/spook mission list                  # ids, names, agent counts
bin/spook mission new "Security review" --scope "audit findings and fixes"   # prints its id
bin/spook mission use M2
bin/spook track --workspace w3          # add that workspace's agents to the active Mission
bin/spook track --pane w3:p2 --mission M2
bin/spook task add "Update the client" --depends T4 --mission M2
bin/spook install-cli                   # a `spook` command in ~/.local/bin
```

## Configuration

Optional. Create `config.toml` in the plugin's config directory
(`herdr plugin config-dir spook`):

```toml
agent_model = "sonnet"          # per-agent summaries
graph_model = "sonnet"          # cross-agent task graph
digest_budget_chars = 120000    # max new conversation per call (~30k tokens)
max_backfill_chunks = 3         # chunks read on first contact with a long conversation
debounce_seconds = 3            # batch turn-ends that arrive together
strip_api_key = true            # unset ANTHROPIC_API_KEY so claude uses your claude.ai login
claude_timeout_seconds = 600
```

Set `strip_api_key = false` if you want the analysis calls billed to the API
key in your environment instead.

## Cost and privacy

- Each finished turn of an agent on a Mission costs one summary call, plus one
  graph call per Mission it's on, of roughly 15–45 seconds each. The Activity tab shows API-rate cost per call. With
  your claude.ai login, the calls count against your plan rather than being
  billed.
- Conversations of agents on a Mission are sent to Claude, the same service
  that produced them. Agents on no Mission are never read.

## State

The board holds no state, so closing it loses nothing. Everything lives in
herdr's plugin state directory (`~/.local/state/herdr/plugins/spook/`):

Shared:

| File | What |
| --- | --- |
| `missions.json` | Missions (name, scope) and which one is shown |
| `agents/<session>.json` | Per-agent summary and transcript read position |
| `aliases.json` | Agent ids (A1, A2…), shared by all Missions |
| `activity.jsonl` | Background call log (trimmed to 2000 lines) |
| `queue.json`, `status.json` | Worker queue and state |
| `spook.log` | Debug log |
| `board_prefs.json` | Board display preferences (the task sort) |

Per Mission, in `missions/<id>/`:

| File | What |
| --- | --- |
| `agents.json` | The agents on this Mission |
| `graph.json` | Concerns, tasks, alerts |
| `hidden_tasks.json` | Completed tasks you hid with `x` |
| `added_tasks.json` | Tasks added with `spook task add` (kept by the model) |
| `task_tags.json` | Your tags on tasks (`#`) |
| `task_edits.json` | Task fields set with `spook task update` (kept by the model until released) |
| `trash.json` | Trashed tasks, with a snapshot of each |
| `status_overrides.json` | Statuses set by hand: by you with `c`, or by an agent with `spook task done/reopen` |
| `task_notes.json` | Your private notes on tasks |
| `notes_delivered.json` | Which agent pane has been pointed at which notes |
| `briefs/<task>.md` | Briefs handed to agents |
| `launches.json` | Which pane was started for which task |

A read position only advances after that agent's summary is saved. If herdr
stops mid-analysis, the next sync redoes that chunk.

To start over, delete the directory. `herdr plugin unlink spook` leaves it in
place.

## Troubleshooting

- **Nothing happens.** Check that the agent is on a Mission (`bin/spook
  status`, or `w` on the board). Then check the Activity tab for failed calls, and `herdr plugin
  log list --plugin spook` for hook errors.
- **"Credit balance is too low" in Activity.** An `ANTHROPIC_API_KEY` without
  credit is being used. Keep `strip_api_key = true`, or top up the key.
- **An agent is missing from the picker.** Only Claude Code panes that herdr
  reports with a session id can be added. See `herdr agent list`.

## Layout

```
herdr-plugin.toml   manifest: startup sync, event hook, actions, board pane
bin/spook           launcher (uv run python -m spook)
spook/
  __main__.py       CLI: hook, sync, track, untrack, mission, open, board, status
  missions.py       Missions: registry, membership, session following, migration
  worker.py         queue, locking, per-agent updates, graph rebuild
  transcript.py     find transcripts, digest new lines
  analyze.py        prompts, JSON schemas, claude -p calls
  activity.py       background call log
  launch.py         start an agent for a task; briefs; deliver notes on g
  notes.py          private task notes and their delivery to agents
  herdr.py          herdr CLI/socket helpers, docking
  tui.py            the Textual board
```

## License

[Apache License 2.0](LICENSE)
