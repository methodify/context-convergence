"""Machine-level convergence config — currently just the default remote.

A user keeps one cluster repo, so they should name it once, not on every
`init`/`join`/`projects`. (`push`/`pull`/`sync`/`status` already remember the
remote per project via LocalState — this is only for the commands that run
before a project has local state.) `--remote` always overrides, reserved for the
rare second cluster.

Stored at `<convergence_home>/config.json`, so it is per-machine and isolated in
tests via CONVERGENCE_HOME. CONVERGENCE_REMOTE overrides the stored default
(handy for tests and one-off shells).
"""

from __future__ import annotations

import json
import os

from . import env


def _path() -> str:
    return os.path.join(env.convergence_home(), "config.json")


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def get_default_remote() -> str | None:
    return load().get("default_remote")


def set_default_remote(url: str) -> None:
    data = load()
    data["default_remote"] = url
    _save(data)


def clear_default_remote() -> None:
    data = load()
    data.pop("default_remote", None)
    _save(data)


def resolve_remote(explicit: str | None) -> str | None:
    """Effective remote: explicit --remote, else CONVERGENCE_REMOTE, else the
    stored default. None if nothing is set."""
    return explicit or os.environ.get("CONVERGENCE_REMOTE") or get_default_remote()
