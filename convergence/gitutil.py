"""Thin, explicit git wrappers (subprocess). No third-party deps.

Conventions that keep transport hermetic and predictable:
  - The cluster branch is always `main`.
  - Commits carry a fixed convergence identity and never sign, so a machine with
    no global git identity (CI, a fresh box) can still converge.
  - Every call is checked; a failing git command raises GitError with stderr.
"""

from __future__ import annotations

import os
import subprocess

BRANCH = "main"
_IDENTITY = [
    "-c", "user.name=convergence",
    "-c", "user.email=convergence@localhost",
    "-c", "commit.gpgsign=false",
]


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} (in {cwd}):\n{proc.stderr.strip()}")
    return proc


def init_bare(path: str) -> None:
    _git(["init", "--bare", "-b", BRANCH, path])


def clone(remote: str, dest: str) -> None:
    _git(["clone", "-b", BRANCH, remote, dest])


def is_repo(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    return _git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False).returncode == 0


def init_repo(path: str, remote: str) -> None:
    """Initialise `path` as a working clone of `remote` even when the remote has
    no commits yet (fresh private repo): plain clone fails on an empty remote, so
    init + wire origin instead."""
    _git(["init", "-b", BRANCH, path])
    _git(["remote", "add", "origin", remote], cwd=path)


def remote_has_branch(cwd: str) -> bool:
    out = _git(["ls-remote", "--heads", "origin", BRANCH], cwd=cwd).stdout.strip()
    return bool(out)


def fetch(cwd: str) -> None:
    _git(["fetch", "origin"], cwd=cwd)


def reset_to_remote(cwd: str) -> None:
    """Make the working tree exactly match the remote's branch tip. All merging
    is done by the app in canonical space, so the clone is a pure cache of the
    remote — this keeps git transport conflict-free."""
    _git(["checkout", "-B", BRANCH], cwd=cwd)
    _git(["reset", "--hard", f"origin/{BRANCH}"], cwd=cwd)
    _git(["clean", "-fd"], cwd=cwd)


def commit_all(cwd: str, message: str) -> bool:
    """Stage everything and commit. Returns False if there was nothing to commit."""
    _git(["add", "-A"], cwd=cwd)
    status = _git(["status", "--porcelain"], cwd=cwd).stdout.strip()
    if not status:
        return False
    _git([*_IDENTITY, "commit", "-m", message], cwd=cwd)
    return True


def push(cwd: str) -> subprocess.CompletedProcess:
    """Push the branch; returns the CompletedProcess (caller checks for the
    non-fast-forward case to retry)."""
    return _git(["push", "origin", BRANCH], cwd=cwd, check=False)


def current_commit(cwd: str) -> str | None:
    proc = _git(["rev-parse", "HEAD"], cwd=cwd, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None
