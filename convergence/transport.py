"""Transport: how a cluster working directory syncs with a shared remote.

Two implementations behind one interface so the engine is transport-agnostic:

  LocalTransport  the cluster dir IS the cluster (Sprint 1/2) — sync is a no-op.
  GitTransport    the cluster dir is a working clone of a private git remote.
                  git is pure transport + history; all merging happens in
                  canonical space via union_jsonl, so the clone is a cache of
                  the remote and git-level conflicts never arise.

The everyday merge is append-mostly union (design §7): two machines that added
different sessions union cleanly; a session extended on two machines keeps both
sides' records rather than dropping either.
"""

from __future__ import annotations

import os

from . import gitutil
from .cluster import Cluster


class PushRejected(Exception):
    """The remote advanced under us; the caller should re-run and retry."""


def union_jsonl(remote_text: str, ours_text: str) -> str:
    """Merge two canonical JSONL versions of one file without losing records.

    Our local order is authoritative (it is the most complete view of the
    sessions we author); any record present only on the remote is preserved by
    appending it. Exact-line dedup. This is the append-mostly union: when the
    remote is a prefix of ours (the common case) the result is exactly ours.
    """
    ours = ours_text.splitlines(keepends=True)
    seen = set(ours)
    extra = [ln for ln in remote_text.splitlines(keepends=True) if ln not in seen]
    return "".join(ours + extra)


class LocalTransport:
    def __init__(self, cluster_root: str):
        self.cluster = Cluster(cluster_root)

    def ensure(self) -> None:
        self.cluster.ensure()

    def sync_down(self) -> None:
        pass

    def publish(self, message: str) -> None:
        pass


class GitTransport:
    def __init__(self, cluster_root: str, remote: str):
        self.cluster = Cluster(cluster_root)
        self.remote = remote

    # -- setup ------------------------------------------------------------ #
    def ensure(self) -> None:
        root = self.cluster.root
        if not gitutil.is_repo(root):
            os.makedirs(os.path.dirname(root) or ".", exist_ok=True)
            if self._remote_has_branch():
                gitutil.clone(self.remote, root)
            else:
                gitutil.init_repo(root, self.remote)  # fresh, empty remote
        self.cluster.ensure()

    def _remote_has_branch(self) -> bool:
        # Works against a URL/path without a local repo.
        from .gitutil import BRANCH, _git
        return bool(_git(["ls-remote", "--heads", self.remote, BRANCH]).stdout.strip())

    # -- sync ------------------------------------------------------------- #
    def sync_down(self) -> None:
        """Bring the working tree to the remote's latest. No-op while the remote
        has no branch yet (first init/push will create it)."""
        if gitutil.remote_has_branch(self.cluster.root):
            gitutil.fetch(self.cluster.root)
            gitutil.reset_to_remote(self.cluster.root)

    def publish(self, message: str) -> None:
        committed = gitutil.commit_all(self.cluster.root, message)
        if not committed:
            return
        result = gitutil.push(self.cluster.root)
        if result.returncode != 0:
            if "fetch first" in result.stderr or "non-fast-forward" in result.stderr \
                    or "rejected" in result.stderr:
                raise PushRejected(result.stderr.strip())
            from .gitutil import GitError
            raise GitError(result.stderr.strip())


def open_transport(cluster_root: str, remote: str | None):
    return GitTransport(cluster_root, remote) if remote else LocalTransport(cluster_root)
