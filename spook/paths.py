"""Where spook keeps its state and config, and small JSON/locking helpers."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path

_POINTER = Path.home() / ".local" / "state" / "spook" / "state_dir.txt"
_FALLBACK = Path.home() / ".local" / "state" / "spook"


def state_dir() -> Path:
    """HERDR_PLUGIN_STATE_DIR when herdr launched us; otherwise the last one herdr gave us."""
    env = os.environ.get("SPOOK_STATE_DIR") or os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if env:
        p = Path(env)
        if os.environ.get("HERDR_PLUGIN_STATE_DIR"):
            _POINTER.parent.mkdir(parents=True, exist_ok=True)
            if not _POINTER.exists() or _POINTER.read_text().strip() != str(p):
                _POINTER.write_text(str(p))
    elif _POINTER.exists():
        p = Path(_POINTER.read_text().strip())
    else:
        p = _FALLBACK
    p.mkdir(parents=True, exist_ok=True)
    (p / "agents").mkdir(exist_ok=True)
    return p


def config_dir() -> Path:
    env = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    p = Path(env) if env else state_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p


DEFAULT_CONFIG = {
    # Model for per-agent summaries (runs once per finished turn per tracked agent).
    "agent_model": "sonnet",
    # Model for the cross-agent task/dependency graph.
    "graph_model": "sonnet",
    # Max characters of new conversation sent per analysis call.
    "digest_budget_chars": 120_000,
    # On first contact with a long conversation, how many budget-sized chunks
    # (most recent) to roll through. Older history is skipped.
    "max_backfill_chunks": 3,
    # Seconds to wait after an event so bursts of turn-ends are batched.
    "debounce_seconds": 3,
    # Unset ANTHROPIC_API_KEY for claude -p so it uses your claude.ai login.
    "strip_api_key": True,
    "claude_timeout_seconds": 600,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = config_dir() / "config.toml"
    if path.exists():
        with path.open("rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    """Atomic write so the board never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


@contextmanager
def file_lock(path: Path, blocking: bool = True):
    """Yields True if the lock was taken, False if non-blocking and already held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}\n"
    with (state_dir() / "spook.log").open("a") as f:
        f.write(line)
