"""Thin wrapper over the herdr CLI (the plugin API)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path


class HerdrError(RuntimeError):
    pass


def _bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def call(*args: str, timeout: float = 30) -> dict:
    proc = subprocess.run([_bin(), *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise HerdrError(f"herdr {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"text": proc.stdout}  # Some commands print plain text on success.
    if "error" in data:
        raise HerdrError(f"herdr {' '.join(args)}: {data['error']}")
    return data.get("result", data)


def agents() -> list[dict]:
    return call("agent", "list").get("agents", [])


def workspaces() -> list[dict]:
    return call("workspace", "list").get("workspaces", [])


def focus_pane(pane_id: str) -> None:
    subprocess.run([_bin(), "agent", "focus", pane_id], capture_output=True, timeout=10)


def sock(method: str, params: dict) -> dict:
    """One raw socket API request (for calls the CLI doesn't expose)."""
    path = os.environ.get("HERDR_SOCKET_PATH") or str(Path.home() / ".config" / "herdr" / "herdr.sock")
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(10)
        s.connect(path)
        s.sendall((json.dumps({"id": "spook", "method": method, "params": params}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    data = json.loads(buf)
    if "error" in data:
        raise HerdrError(f"{method}: {data['error']}")
    return data["result"]


def _is_board(node: dict) -> bool:
    cmd = node.get("command") or []
    return node.get("type") == "pane" and "bin/spook" in cmd and "board" in cmd


def _find(node: dict, pred, path=()):
    """Depth-first search of an exported layout tree. Yields (node, path to it)."""
    if pred(node):
        yield node, list(path)
    if node.get("type") == "split":
        yield from _find(node["first"], pred, path + (False,))
        yield from _find(node["second"], pred, path + (True,))


DOCK_FRACTION = 0.35
DOCK_MIN_COLS = 60


def toggle_board() -> None:
    """Dock the board on the right of the current tab, or close it if it's already there."""
    ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    layout = call("pane", "layout")["layout"]
    tab = ctx.get("tab_id") or os.environ.get("HERDR_TAB_ID") or layout["tab_id"]
    tree = sock("layout.export", {"tab_id": tab})["layout"]["root"]

    existing = next(_find(tree, _is_board), None)
    if existing:
        sock("pane.close", {"pane_id": existing[0]["pane_id"]})
        return

    if tab != layout["tab_id"]:
        any_pane = next(_find(tree, lambda n: n.get("type") == "pane"))[0]["pane_id"]
        layout = call("pane", "layout", "--pane", any_pane)["layout"]
    prev = layout.get("focused_pane_id")
    # Split the tallest pane touching the right edge so the board runs as tall as possible.
    right_edge = max(p["rect"]["x"] + p["rect"]["width"] for p in layout["panes"])
    target = max(
        (p for p in layout["panes"] if p["rect"]["x"] + p["rect"]["width"] == right_edge),
        key=lambda p: p["rect"]["height"],
    )
    opened = call(
        "plugin", "pane", "open", "--plugin", "spook", "--entrypoint", "board",
        "--placement", "split", "--direction", "right",
        "--target-pane", target["pane_id"], "--no-focus",
    )
    board_id = opened["plugin_pane"]["pane"]["pane_id"]

    # Size it: the new split covers the target pane's old width.
    split_cols = target["rect"]["width"]
    want = max(DOCK_MIN_COLS, int(layout["area"]["width"] * DOCK_FRACTION))
    ratio = min(0.8, max(0.2, 1 - want / max(split_cols, 1)))
    tree = sock("layout.export", {"tab_id": tab})["layout"]["root"]
    hit = next(_find(tree, lambda n: n.get("type") == "split" and n["second"].get("pane_id") == board_id), None)
    if hit:
        sock("layout.set_split_ratio", {"tab_id": tab, "path": hit[1], "ratio": ratio})
    if prev:
        sock("pane.focus", {"pane_id": prev})
