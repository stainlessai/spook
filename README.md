# spook

A [herdr](https://herdr.dev) plugin for people running many coding agents at
once on work that cuts across projects.

Spook reads the Claude Code conversations of agents in the herdr workspaces
you choose. It summarizes each agent and builds one task board across all of
them, with concerns, tasks, dependencies and alerts. It docks on the right of
your herdr tab.

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

## How it works

```
agent finishes a turn
  → herdr fires pane.agent_status_changed
  → spook hook: is this workspace tracked? queue the pane
  → worker (one at a time; bursts are batched):
      1. read only the new lines of that agent's transcript
         (~/.claude/projects/<dir>/<session-id>.jsonl)
      2. digest them: user asks, agent replies, files edited, commands run
      3. claude -p  → update that agent's summary
      4. claude -p  → rebuild the task graph from all agents' summaries
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
2. Press `w`, tick the workspaces to watch, then press `s`. The first analysis
   starts right away. Follow it in the status bar or the Activity tab.
3. After that, spook updates whenever a tracked agent finishes a turn.

| Key | Action |
| --- | --- |
| `w` | Choose tracked workspaces (`space` toggles, `s` saves, `esc` cancels) |
| `r` | Re-analyze all tracked agents now |
| `g` | Jump to the agent behind the selected task or row (on an unowned task, offers to start one) |
| `a` | Start an agent for the selected task |
| `x` | Hide the selected completed task (on a hidden one: unhide it) |
| `d` | Show or hide hidden tasks |
| `q` | Close the board |

### Starting an agent for an unowned task

Press `a` on a task, or `g` on a task nobody owns. A popup shows:

- **Directory:** guessed from the task's projects and the repos your tracked
  agents use. You can edit it.
- **Where:** a new tab in a tracked workspace, or a new workspace that spook
  then tracks.
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
dialog. Spook records which task the agent was started for, so the graph
makes it the task's owner after its first turn.

There are also herdr actions: **Spook: track this workspace**, **Spook: stop
tracking this workspace** and **Spook: re-analyze now**.

From a shell:

```sh
bin/spook status                     # tracked workspaces, worker state, graph counts
bin/spook sync                       # same as r
bin/spook track --workspace w3       # track by herdr workspace id
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

- Each finished turn of a tracked agent costs two model calls of roughly
  15–45 seconds each. The Activity tab shows API-rate cost per call. With
  your claude.ai login, the calls count against your plan rather than being
  billed.
- Agent conversations from tracked workspaces are sent to Claude, the same
  service that produced them. Untracked workspaces are never read.

## State

The board holds no state, so closing it loses nothing. Everything lives in
herdr's plugin state directory (`~/.local/state/herdr/plugins/spook/`):

| File | What |
| --- | --- |
| `tracked.json` | Tracked workspace ids |
| `agents/<session>.json` | Per-agent summary and transcript read position |
| `graph.json` | Concerns, tasks, alerts |
| `hidden_tasks.json` | Completed tasks you hid with `x` |
| `activity.jsonl` | Background call log (trimmed to 2000 lines) |
| `queue.json`, `status.json` | Worker queue and state |
| `briefs/<task>.md` | Briefs handed to agents started from the board |
| `launches.json` | Which pane was started for which task |
| `spook.log` | Debug log |

A read position only advances after that agent's summary is saved. If herdr
stops mid-analysis, the next sync redoes that chunk.

To start over, delete the directory. `herdr plugin unlink spook` leaves it in
place.

## Troubleshooting

- **Nothing happens.** Check that the workspace is tracked (`bin/spook
  status`). Then check the Activity tab for failed calls, and `herdr plugin
  log list --plugin spook` for hook errors.
- **"Credit balance is too low" in Activity.** An `ANTHROPIC_API_KEY` without
  credit is being used. Keep `strip_api_key = true`, or top up the key.
- **An agent is missing.** Only Claude Code panes that herdr reports with a
  session id are tracked. See `herdr agent list`.

## Layout

```
herdr-plugin.toml   manifest: startup sync, event hook, actions, board pane
bin/spook           launcher (uv run python -m spook)
spook/
  __main__.py       CLI: hook, sync, track, untrack, open, board, status
  worker.py         queue, locking, per-agent updates, graph rebuild
  transcript.py     find transcripts, digest new lines
  analyze.py        prompts, JSON schemas, claude -p calls
  activity.py       background call log
  launch.py         start an agent for a task: directory guess, brief, prompt
  herdr.py          herdr CLI/socket helpers, docking
  tui.py            the Textual board
```
