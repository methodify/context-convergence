"""Location resolution and small ambient helpers.

Everything that touches a real filesystem location or the wall clock funnels
through here, and every location honours an environment override. That keeps the
whole tool testable in temp dirs without ever writing to the real
`~/.claude/projects/` (which holds irreplaceable context) and makes runs
reproducible.

Overrides:
  CLAUDE_PROJECTS_DIR   where local context lives (default ~/.claude/projects)
  CONVERGENCE_HOME      per-machine convergence state    (default ~/.convergence)
  CONVERGENCE_MACHINE_ID  pin this machine's id (default: generated + persisted)
  CONVERGENCE_NOW       pin the clock to a fixed ISO-8601 instant (tests)
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone


def claude_projects_dir() -> str:
    return os.environ.get(
        "CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects")
    )


def convergence_home() -> str:
    return os.environ.get(
        "CONVERGENCE_HOME", os.path.expanduser("~/.convergence")
    )


def home_dir() -> str:
    return os.path.expanduser("~")


def now_iso() -> str:
    """Current instant as ISO-8601 UTC (Z), or the CONVERGENCE_NOW override."""
    pinned = os.environ.get("CONVERGENCE_NOW")
    if pinned:
        return pinned
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def machine_id() -> str:
    """This machine's stable id. Honours CONVERGENCE_MACHINE_ID; otherwise reads
    (or creates) `<convergence_home>/machine_id`, so the same machine is
    recognised across runs (design §3.3)."""
    pinned = os.environ.get("CONVERGENCE_MACHINE_ID")
    if pinned:
        return pinned
    path = os.path.join(convergence_home(), "machine_id")
    try:
        with open(path, encoding="utf-8") as fh:
            mid = fh.read().strip()
            if mid:
                return mid
    except FileNotFoundError:
        pass
    mid = uuid.uuid4().hex[:12]
    os.makedirs(convergence_home(), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(mid + "\n")
    return mid


def detected_os() -> str:
    import sys
    return {"darwin": "darwin", "linux": "linux", "win32": "windows"}.get(
        sys.platform, sys.platform
    )
