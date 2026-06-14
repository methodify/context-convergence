"""Per-project advisory lock so concurrent runs can't interleave.

The Stop-hook's `sync` can fire while a manual `sync` is running (or two hooks
overlap). Without a lock they'd both mutate the same clone and write the same
local context dir, racing to a mixed state. We serialize per project with an
`flock` on `<convergence_home>/locks/<project_id>.lock`:

- `flock` is released automatically when the process exits (even on crash), so
  there are no stale locks to clean up.
- Acquisition is non-blocking: if another run holds it, we fail loud with
  LockBusy rather than queueing. The hook treats LockBusy as "skip, another run
  has it"; a manual command tells the user to retry.
- Re-entrant within a process: `sync` holds the lock while calling `push`/`pull`
  internally, which would otherwise deadlock on a second acquire.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

from . import env
from .errors import LockBusy

try:
    import fcntl
except ImportError:  # non-POSIX (e.g. Windows) — degrade to no-op
    fcntl = None

_held: set[str] = set()  # project_ids locked by THIS process (for re-entrancy)


def _lock_path(project_id: str) -> str:
    return os.path.join(env.convergence_home(), "locks", f"{project_id}.lock")


@contextmanager
def project_lock(project_id: str):
    if fcntl is None or project_id in _held:
        yield  # no fcntl available, or already held by this process (re-entrant)
        return
    path = _lock_path(project_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = open(path, "w")
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LockBusy(
                f"another convergence operation is already running for "
                f"'{project_id}'. Wait for it to finish, then retry.")
        _held.add(project_id)
        try:
            yield
        finally:
            _held.discard(project_id)
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()
