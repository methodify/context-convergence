"""Thin, explicit git wrappers (subprocess). No third-party deps.

In the branch-per-project model each project is an orphan branch `project/<id>`
in one cluster repo, cloned single-branch so a machine only ever fetches the one
project it wants. These helpers are branch-parameterized; the only fixed branch
is `main`, which holds a human-readable README for browsing the cluster on the
host.

Commits carry a fixed convergence identity and never sign, so a machine with no
global git identity (CI, a fresh box) can still converge. Every call is checked;
a failing git command raises GitError with stderr.
"""

from __future__ import annotations

import os
import subprocess

MAIN_BRANCH = "main"
_IDENTITY = [
    "-c", "user.name=convergence",
    "-c", "user.email=convergence@localhost",
    "-c", "commit.gpgsign=false",
]


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} (in {cwd}):\n{proc.stderr.strip()}")
    return proc


# -- repo creation / inspection ------------------------------------------- #
def init_bare(path: str) -> None:
    _git(["init", "--bare", "-b", MAIN_BRANCH, path])


def is_repo(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    return _git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False).returncode == 0


def init_repo(path: str, remote: str) -> None:
    """Initialise `path` as a working clone of `remote` with no checkout yet —
    used when creating a brand-new project branch."""
    os.makedirs(path, exist_ok=True)
    _git(["init", "-b", MAIN_BRANCH, path])
    _git(["remote", "add", "origin", remote], cwd=path)


def remote_branches(remote: str) -> list[str]:
    """All branch names on `remote` (works against a URL, no local repo)."""
    out = _git(["ls-remote", "--heads", remote]).stdout.strip()
    return [line.split("refs/heads/", 1)[1] for line in out.splitlines() if "refs/heads/" in line]


def remote_has_branch(remote: str, branch: str) -> bool:
    return bool(_git(["ls-remote", "--heads", remote, branch]).stdout.strip())


# -- per-branch working operations ---------------------------------------- #
def clone_single_branch(remote: str, branch: str, dest: str) -> None:
    """Clone ONLY `branch` — fetches just that project's history."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    _git(["clone", "--single-branch", "--branch", branch, remote, dest])


def create_orphan(cwd: str, branch: str) -> None:
    """Start a new project as an orphan branch (no shared history with others).
    Clear the working tree AND index so files from `main` (the README) do not
    leak into the project branch."""
    _git(["checkout", "--orphan", branch], cwd=cwd)
    _git(["rm", "-rf", "--ignore-unmatch", "."], cwd=cwd, check=False)


def checkout_new_main_with(cwd: str, write_readme) -> None:
    """Create the README-bearing `main` branch in a fresh clone and push it.
    `write_readme` is called with the working dir to drop the README file."""
    _git(["checkout", "-b", MAIN_BRANCH], cwd=cwd)
    write_readme(cwd)
    _git(["add", "-A"], cwd=cwd)
    _git([*_IDENTITY, "commit", "-m", "initialize convergence cluster"], cwd=cwd)
    _git(["push", "-u", "origin", MAIN_BRANCH], cwd=cwd)


def fetch(cwd: str) -> None:
    _git(["fetch", "origin"], cwd=cwd)


def reset_to_remote(cwd: str, branch: str) -> None:
    """Make the working tree exactly match origin/<branch>. All merging happens
    in canonical space, so the clone is a pure cache of the remote branch."""
    _git(["checkout", "-B", branch], cwd=cwd)
    _git(["reset", "--hard", f"origin/{branch}"], cwd=cwd)
    _git(["clean", "-fd"], cwd=cwd)


def commit_all(cwd: str, message: str) -> bool:
    _git(["add", "-A"], cwd=cwd)
    if not _git(["status", "--porcelain"], cwd=cwd).stdout.strip():
        return False
    _git([*_IDENTITY, "commit", "-m", message], cwd=cwd)
    return True


def push(cwd: str, branch: str) -> subprocess.CompletedProcess:
    """Push `branch`; returns the CompletedProcess (caller checks for non-ff)."""
    return _git(["push", "-u", "origin", branch], cwd=cwd, check=False)


def current_commit(cwd: str) -> str | None:
    proc = _git(["rev-parse", "HEAD"], cwd=cwd, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None
