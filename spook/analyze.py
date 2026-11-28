"""LLM calls through headless Claude Code (`claude -p`) with structured output."""

from __future__ import annotations

import json
import os
import subprocess
import time

from . import activity
from .paths import load_config, log, state_dir

STATUS = ["open", "in_progress", "blocked", "done", "dropped"]

AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short name for what this conversation is about"},
        "goal": {"type": "string", "description": "The user's overall objective in one sentence"},
        "summary": {"type": "string", "description": "3-6 sentences: where things stand right now"},
        "phase": {
            "type": "string",
            "enum": ["investigating", "implementing", "testing", "waiting_on_user", "waiting_on_other_work", "done", "idle"],
        },
        "work_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Stable kebab-case slug; reuse existing keys"},
                    "title": {"type": "string"},
                    "status": {"type": "string", "enum": STATUS},
                    "projects": {"type": "array", "items": {"type": "string"}},
                    "detail": {"type": "string"},
                    "needs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Changes or answers required from OTHER projects/repos/services",
                    },
                    "provides": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Changes made here that other projects may depend on or be affected by",
                    },
                },
                "required": ["key", "title", "status", "projects", "detail", "needs", "provides"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "goal", "summary", "phase", "work_items", "open_questions"],
}

GRAPH_SCHEMA = {
    "type": "object",
    "properties": {
        "concerns": {
            "type": "array",
            "description": "Themes that group tasks, especially ones spanning several projects",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "crosscutting": {"type": "boolean"},
                },
                "required": ["id", "name", "description", "crosscutting"],
            },
        },
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Stable short id like T12; reuse previous ids"},
                    "title": {"type": "string"},
                    "status": {"type": "string", "enum": STATUS},
                    "concern_id": {"type": "string"},
                    "projects": {"type": "array", "items": {"type": "string"}},
                    "owners": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Agent ids (A1, A2…) doing this work; empty if nobody is on it yet",
                    },
                    "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Task ids"},
                    "detail": {"type": "string"},
                    "evidence": {"type": "string", "description": "Which agent said what that supports this task/dependency"},
                },
                "required": ["id", "title", "status", "concern_id", "projects", "owners", "depends_on", "detail", "evidence"],
            },
        },
        "alerts": {
            "type": "array",
            "description": "Things the human should act on: unowned needs, conflicts, a change that affects another agent",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["info", "warn", "critical"]},
                    "text": {"type": "string"},
                    "agents": {"type": "array", "items": {"type": "string"}},
                    "task_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["severity", "text", "agents", "task_ids"],
            },
        },
    },
    "required": ["concerns", "tasks", "alerts"],
}

AGENT_SYSTEM = """You maintain a running status record for ONE coding-agent conversation, \
so that a human juggling many agents across many repositories can coordinate them.

You get the previous record (may be empty) and a digest of the conversation's NEW messages. \
Return the updated record. Rules:
- Keep work item keys stable; update status as work progresses; add items for new issues that came up.
- Name projects by repository or service (e.g. "infra/terraform", "billing-api", "web-app"), not by file.
- `needs` = anything this work requires from a DIFFERENT project/repo/service/team. `provides` = changes made here \
that something elsewhere may depend on or be broken by (APIs, schemas, IAM, configs, env vars, contracts). \
These two fields drive cross-project dependency tracking, so be specific and concrete.
- Only record what the conversation supports. Do not invent work.
- Drop items that are fully irrelevant now; keep finished ones as done.
- Acting on the spook task board itself is not a work item: closing or reopening a spook task \
(ids like T3), reporting a task's status, or telling another agent that a task is done or a dependency \
is met. Record it as a status change and a note on the item it concerns."""

GRAPH_SYSTEM = """You are the coordinator for a human running several coding agents at once, \
each in its own directory, on problems with crosscutting concerns.

You get the Mission's scope, each agent's status record and the previous task graph. \
Produce the updated graph:
- Agents can work on several things. Include only work relevant to the Mission scope and \
ignore the rest of an agent's work. If there is no scope, include everything.
- Merge work items from different agents that are the same underlying work into one task.
- Group tasks into concerns. A concern is crosscutting when it spans more than one project.
- Add `depends_on` edges when one task cannot finish until another does, including when one agent's \
`needs` matches another agent's `provides`, or when a change in one project forces a change elsewhere.
- When a `need` has no task or owner anywhere, create an unowned task for it and raise an alert.
- Raise alerts for: unowned needs, two agents making conflicting changes, an agent's change that \
another agent probably doesn't know about, and agents blocked on each other.
- An agent with `launched_for_task` was started by the human specifically for that task id: \
make it an owner of that task (keep the id) unless its record clearly shows it went elsewhere.
- Never create a task whose subject is the board itself: adding a task to it (e.g. `spook task add`), \
closing, reopening or updating another task, \
or telling an agent about a task's status. Apply it to the task it concerns (status, plus a note in \
its detail such as "closed by A5; A1 notified").
- Tasks in "Trash" were removed from the board by the human. Do not output them and do not recreate \
them under another id. Create a new task only for work that is clearly different from them.
- "Statuses set explicitly" are decisions, made by the human or by an agent with `spook task done` \
or `spook task reopen` (see `set_by`): keep each task at that status. If an agent's record clearly \
shows new work on such a task after it was set (e.g. work continuing on a closed task), do not \
change the status; raise a warn alert naming the task and the agent instead.
- "Tasks added explicitly" were put on the board on purpose, by the human or an agent. Output \
every one with its id and title unchanged. You may set its concern, owners, status and further \
dependencies from the records, and merge other agents' matching work into it rather than \
creating a duplicate.
- "Task fields set explicitly" were set by the human or an agent with `spook task update`. Keep \
each listed field at that value (`concern` is a concern name). Other fields of those tasks are yours.
- Reuse previous task and concern ids for the same work, and never reuse a trashed task's id. \
Only claim what the records support."""


def _env() -> dict:
    env = dict(os.environ)
    if load_config()["strip_api_key"]:
        env.pop("ANTHROPIC_API_KEY", None)
    # Don't let analysis calls look like children of whatever session launched us.
    for k in list(env):
        if k.startswith("CLAUDE_CODE_") and k not in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            env.pop(k)
    return env


def claude_json(system: str, prompt: str, schema: dict, model: str, label: str) -> dict:
    cfg = load_config()
    cwd = state_dir() / "claude-cwd"
    cwd.mkdir(exist_ok=True)
    cmd = [
        "claude", "-p",
        "--model", model,
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format", "json",
        "--system-prompt", system,
        "--json-schema", json.dumps(schema),
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=cwd, env=_env(),
    )
    call_id = activity.record("call_start", label=label, model=model, pid=proc.pid, chars=len(prompt))
    t0 = time.time()

    def fail(msg: str):
        activity.record("call_end", id=call_id, ok=False, duration_ms=int((time.time() - t0) * 1000), error=msg)
        raise RuntimeError(msg)

    try:
        stdout, stderr = proc.communicate(prompt, timeout=cfg["claude_timeout_seconds"])
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        fail(f"timed out after {cfg['claude_timeout_seconds']}s")
    out = stdout.strip()
    try:
        res = json.loads(out[out.find("{"):])
    except json.JSONDecodeError:
        fail(f"claude -p returned non-JSON (exit {proc.returncode}): {stderr[-500:]}")
    if res.get("is_error") or res.get("structured_output") is None:
        fail(f"claude -p failed: {res.get('result') or res.get('terminal_reason')}")
    activity.record(
        "call_end", id=call_id, ok=True,
        duration_ms=res.get("duration_ms") or int((time.time() - t0) * 1000),
        cost=res.get("total_cost_usd"),
    )
    log(f"claude {model} [{label}]: {res.get('duration_ms')}ms ${res.get('total_cost_usd', 0):.4f}")
    return res["structured_output"]


def update_agent_record(previous: dict | None, meta: dict, digest: str, first_ask: str | None, label: str) -> dict:
    parts = [
        "# Agent",
        json.dumps(meta, indent=2),
        "# Previous record",
        json.dumps(previous, indent=2) if previous else "(none — first look at this conversation)",
    ]
    if first_ask:
        parts += ["# The conversation's first user message", first_ask]
    parts += ["# New conversation since the previous record", digest]
    return claude_json(AGENT_SYSTEM, "\n\n".join(parts), AGENT_SCHEMA, load_config()["agent_model"], label)


def update_graph(agent_records: list[dict], previous: dict | None, trashed: list[dict],
                 overrides: list[dict], scope: str, reserved_ids: list[str], label: str,
                 added: list[dict] | None = None, edits: list[dict] | None = None) -> dict:
    prompt = "\n\n".join([
        "# Mission scope",
        scope.strip() or "(none: include all of the agents' work)",
        "# Agents",
        json.dumps(agent_records, indent=2),
        "# Previous graph",
        json.dumps(previous, indent=2) if previous else "(none)",
        "# Trash (removed from the board by the human; do not recreate)",
        json.dumps(trashed, indent=2) if trashed else "(empty)",
        "# Statuses set explicitly (keep these)",
        json.dumps(overrides, indent=2) if overrides else "(none)",
        "# Tasks added explicitly (keep every one)",
        json.dumps(added, indent=2) if added else "(none)",
        "# Task fields set explicitly (keep these values)",
        json.dumps(edits, indent=2) if edits else "(none)",
        "# Reserved task ids (in use elsewhere on this Mission; never output or reuse)",
        ", ".join(reserved_ids) if reserved_ids else "(none)",
    ])
    return claude_json(GRAPH_SYSTEM, prompt, GRAPH_SCHEMA, load_config()["graph_model"], label)
