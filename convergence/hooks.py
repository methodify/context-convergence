"""The seamless "when you're done" layer (design §5.1).

A single global Claude Code Stop hook runs `convergence hook-sync` at the end of
every session; hook-sync resolves the project from the session's cwd and syncs
it, failing soft so it can never break a session. One install covers every
convergence project on the machine.

`install`/`uninstall` edit the Claude Code settings.json (path overridable via
CLAUDE_SETTINGS_PATH for tests). Our entry is tagged with a marker string so it
is recognised idempotently and removed cleanly.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import traceback

from . import engine, env

MARKER = "convergence hook-sync"
DEFAULT_EVENT = "Stop"


# --------------------------------------------------------------------------- #
# the hook entry point (invoked by Claude Code at session end)
# --------------------------------------------------------------------------- #
def hook_sync(project_root: str | None = None) -> int:
    """Sync the project for this session. NEVER raises and ALWAYS exits 0 — a
    sync failure must not block the user's session. Resolution order for the
    project: explicit arg, then $CLAUDE_PROJECT_DIR (set by Claude Code for
    hooks), then cwd."""
    root = project_root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        result = engine.sync(project_root=root)
        _log(f"sync {result['project_id']}: pulled {result['pulled']}, pushed {result['pushed']}")
    except engine.ConvergenceError:
        pass  # not a convergence project (the common case) — stay silent
    except Exception:  # noqa: BLE001 — a hook must never break the session
        _log("UNEXPECTED:\n" + traceback.format_exc())
    return 0


def _log(message: str) -> None:
    try:
        os.makedirs(os.path.dirname(env.hook_log_path()), exist_ok=True)
        with open(env.hook_log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"[{env.now_iso()}] {message}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# install / uninstall / status
# --------------------------------------------------------------------------- #
def hook_command() -> str:
    """A shell command that invokes hook-sync without requiring a pip install:
    set PYTHONPATH to the package's parent so `-m convergence` resolves."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return (f"PYTHONPATH={shlex.quote(repo_root)} "
            f"{shlex.quote(sys.executable)} -m convergence hook-sync")


def _load_settings(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def _save_settings(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def _entries(settings: dict, event: str) -> list:
    return settings.setdefault("hooks", {}).setdefault(event, [])


def _has_ours(entries: list) -> bool:
    return any(MARKER in h.get("command", "")
              for e in entries for h in e.get("hooks", []))


def install(event: str = DEFAULT_EVENT, settings_path: str | None = None) -> dict:
    path = settings_path or env.claude_settings_path()
    settings = _load_settings(path)
    entries = _entries(settings, event)
    if _has_ours(entries):
        return {"changed": False, "event": event, "settings": path, "command": hook_command()}
    entries.append({"hooks": [{"type": "command", "command": hook_command()}]})
    _save_settings(path, settings)
    return {"changed": True, "event": event, "settings": path, "command": hook_command()}


def uninstall(event: str = DEFAULT_EVENT, settings_path: str | None = None) -> dict:
    path = settings_path or env.claude_settings_path()
    settings = _load_settings(path)
    hooks = settings.get("hooks", {})
    entries = hooks.get(event, [])
    kept = []
    removed = 0
    for e in entries:
        inner = [h for h in e.get("hooks", []) if MARKER not in h.get("command", "")]
        removed += len(e.get("hooks", [])) - len(inner)
        if inner:
            e["hooks"] = inner
            kept.append(e)
    if removed:
        hooks[event] = kept
        _save_settings(path, settings)
    return {"removed": removed, "event": event, "settings": path}


def status(settings_path: str | None = None) -> dict:
    path = settings_path or env.claude_settings_path()
    settings = _load_settings(path)
    installed = {
        ev: _has_ours(settings.get("hooks", {}).get(ev, []))
        for ev in ("Stop", "SessionEnd")
    }
    return {"settings": path, "installed": installed, "command": hook_command()}
