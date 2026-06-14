"""Transport: how a project's working tree syncs with the cluster.

  LocalTransport  the directory IS the cluster (no git) — sync is a no-op.
  GitTransport    the directory is a single-branch clone of one project's branch
                  (`project/<id>`, an orphan branch) in the cluster repo. git is
                  pure transport + history; merging happens in canonical space
                  via union_jsonl, so the clone is a cache of the branch and
                  git-level conflicts never arise.

Branch-per-project means cloning one project never fetches the others' history,
and pushing one project never conflicts with another (different branches).
"""

from __future__ import annotations

import os

from . import gitutil
from .cluster import README_MAIN, Cluster


class PushRejected(Exception):
    """The branch advanced under us; the caller should re-run and retry."""


def project_branch(project_id: str) -> str:
    return f"project/{project_id}"


def union_jsonl(remote_text: str, ours_text: str) -> str:
    """Merge two canonical JSONL versions of one file without losing records.
    Our local order is authoritative; records present only on the remote are
    appended. Exact-line dedup. Append-mostly union: when the remote is a prefix
    of ours (the common case) the result is exactly ours."""
    ours = ours_text.splitlines(keepends=True)
    seen = set(ours)
    extra = [ln for ln in remote_text.splitlines(keepends=True) if ln not in seen]
    return "".join(ours + extra)


class LocalTransport:
    def __init__(self, cluster_root: str, project_id: str):
        self.cluster = Cluster(cluster_root)
        self.project_id = project_id

    def exists(self) -> bool:
        """This specific project is present in the local cluster dir (a local
        cluster holds one project; its roster's id must match)."""
        return self.cluster.has_roster() and self.cluster.load_roster().project_id == self.project_id

    def ensure(self) -> None:
        self.cluster.ensure()

    def sync_down(self) -> None:
        pass

    def publish(self, message: str) -> None:
        pass


class GitTransport:
    def __init__(self, clone_dir: str, remote: str, branch: str):
        self.cluster = Cluster(clone_dir)
        self.remote = remote
        self.branch = branch

    # -- existence -------------------------------------------------------- #
    def exists(self) -> bool:
        """Does this project's branch already exist on the remote?"""
        return gitutil.remote_has_branch(self.remote, self.branch)

    # -- setup ------------------------------------------------------------ #
    def ensure(self) -> None:
        root = self.cluster.root
        if gitutil.is_repo(root):
            return  # reuse the existing clone (push/pull/sync)
        if self.exists():
            gitutil.clone_single_branch(self.remote, self.branch, root)  # join
        else:
            self._init_new_project(root)                                 # init
        self.cluster.ensure()

    def _init_new_project(self, root: str) -> None:
        gitutil.init_repo(root, self.remote)
        if not gitutil.remote_branches(self.remote):
            # Fresh, empty cluster: seed a human-readable README on `main`.
            gitutil.checkout_new_main_with(root, self._write_readme)
        gitutil.create_orphan(root, self.branch)

    @staticmethod
    def _write_readme(cwd: str) -> None:
        with open(os.path.join(cwd, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(README_MAIN)

    # -- sync ------------------------------------------------------------- #
    def sync_down(self) -> None:
        if self.exists():
            gitutil.fetch(self.cluster.root)
            gitutil.reset_to_remote(self.cluster.root, self.branch)
        # else: brand-new project not yet pushed — nothing to pull.

    def publish(self, message: str) -> None:
        if not gitutil.commit_all(self.cluster.root, message):
            return
        result = gitutil.push(self.cluster.root, self.branch)
        if result.returncode != 0:
            if any(s in result.stderr for s in ("fetch first", "non-fast-forward", "rejected")):
                raise PushRejected(result.stderr.strip())
            from .gitutil import GitError
            raise GitError(result.stderr.strip())


def _assert_safe_cluster_dir(path: str) -> None:
    """Refuse a cluster/clone working dir that overlaps the Claude Code context
    dir. git runs `reset --hard` and `clean -fd` in this dir, so if it sat at or
    inside (or above) ~/.claude/projects, those would destroy live local context.
    The default managed clone (~/.convergence/clones/<id>) is always safe."""
    from . import env
    from .engine import ConvergenceError
    cc = os.path.abspath(env.claude_projects_dir())
    p = os.path.abspath(path)
    try:
        common = os.path.commonpath([p, cc])
    except ValueError:  # different drives (Windows) — cannot overlap
        return
    if common in (p, cc):  # equal, or one contains the other
        raise ConvergenceError(
            f"refusing to use {p} as a cluster working dir: it overlaps your "
            f"Claude Code context dir ({cc}). git operations there could destroy "
            f"local context. Use a path outside ~/.claude/projects — the default "
            f"managed clone under ~/.convergence/clones is safe.")


def open_transport(project_id: str, remote: str | None, clone_or_cluster: str | None):
    """A GitTransport for a remote-backed project (convergence-managed clone),
    or a LocalTransport for a no-git local cluster directory."""
    if remote:
        from . import env
        clone = clone_or_cluster or env.clone_dir(project_id)
        _assert_safe_cluster_dir(clone)
        return GitTransport(clone, remote, project_branch(project_id))
    if not clone_or_cluster:
        from .engine import ConvergenceError
        raise ConvergenceError("a local cluster needs --cluster <dir> (or use --remote)")
    _assert_safe_cluster_dir(clone_or_cluster)
    return LocalTransport(clone_or_cluster, project_id)
