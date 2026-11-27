"""Find a Claude Code transcript and turn new JSONL lines into a compact digest.

Transcripts are mostly API request blobs, tool output and thinking. The digest
keeps what a coordinator needs: what the user asked, what the agent said, and
which files and commands it touched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_TAGS = re.compile(r"<(local-command-stdout|local-command-stderr)>.*?</\1>", re.S)

USER_MAX = 2000
ASSISTANT_MAX = 2500
CMD_MAX = 200


def find_transcript(session_id: str, cwd: str | None) -> Path | None:
    if cwd:
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
        p = PROJECTS / slug / f"{session_id}.jsonl"
        if p.exists():
            return p
    # The session may have started in another directory.
    hits = list(PROJECTS.glob(f"*/{session_id}.jsonl"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def _clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 20] + f" …[+{len(s) - n} chars]"


def _tool_line(block: dict) -> str | None:
    name = block.get("name", "?")
    inp = block.get("input") or {}
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"[edit] {inp.get('file_path') or inp.get('notebook_path')}"
    if name == "Bash":
        return f"[bash] {_clip(str(inp.get('command', '')), CMD_MAX)}"
    if name in ("Agent", "Task"):
        return f"[subagent] {_clip(str(inp.get('description') or inp.get('prompt', '')), CMD_MAX)}"
    if name in ("Read", "Grep", "Glob", "LS"):
        return None  # Reads are noise for coordination.
    return f"[tool] {name}"


def digest_lines(lines: list[str]) -> tuple[list[str], str | None]:
    """Return (digest lines, latest ai-title if any)."""
    out: list[str] = []
    title = None
    for raw in lines:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = d.get("type")
        if t == "ai-title":
            title = d.get("aiTitle") or title
            continue
        if t not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
            continue
        content = (d.get("message") or {}).get("content")
        stamp = (d.get("timestamp") or "")[:16].replace("T", " ")
        if t == "user":
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # Lists are tool results, except for pasted text and images.
                text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            else:
                text = ""
            text = _TAGS.sub("", _REMINDER.sub("", text)).strip()
            if text:
                out.append(f"\n## USER {stamp}\n{_clip(text, USER_MAX)}")
        else:
            for b in content or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append(f"AGENT: {_clip(b['text'], ASSISTANT_MAX)}")
                elif b.get("type") == "tool_use":
                    line = _tool_line(b)
                    if line:
                        out.append(line)
    return out, title


def read_new(path: Path, offset: int) -> tuple[list[str], int]:
    """Read complete lines after offset. Returns (lines, new offset)."""
    size = path.stat().st_size
    if size < offset:  # Truncated or replaced: start over.
        offset = 0
    if size == offset:
        return [], offset
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    chunk = data[: end + 1]
    return chunk.decode("utf-8", "replace").splitlines(), offset + len(chunk)


def fit_budget(lines: list[str], budget: int) -> str:
    """Keep the start (the original ask) and as much of the end as fits."""
    text = "\n".join(lines)
    if len(text) <= budget:
        return text
    head = budget // 8
    tail = budget - head
    return text[:head] + "\n\n[… middle of conversation omitted …]\n\n" + text[-tail:]
