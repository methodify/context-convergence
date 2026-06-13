"""The verbs: init / push / pull / status (design §4).

Sprint 1 scope — single machine, local cluster directory, roster of one, no git
and no merge yet (those are Sprint 3). The point is to prove the repo layout and
canonical form survive a full local roundtrip.

Safety posture (design §6): push runs a per-file round-trip guard and refuses to
ship anything it cannot losslessly reverse; pull backs up the local context dir
before overwriting.
"""

from __future__ import annotations

import glob
import os
import shutil

from . import env
from .cluster import Cluster
from .localstate import LocalState
from .roster import Participant, Roster


class ConvergenceError(Exception):
    """A user-facing failure (fail loud, never guess)."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _abs_root(project_root: str | None) -> str:
    return os.path.abspath(os.path.expanduser(project_root or os.getcwd()))


def _local_context_dir(encoded_dir: str) -> str:
    return os.path.join(env.claude_projects_dir(), encoded_dir)


def _local_context_files(encoded_dir: str) -> list[str]:
    return sorted(glob.glob(os.path.join(_local_context_dir(encoded_dir), "*.jsonl")))


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _resolve_state(project_root: str | None, project_id: str | None) -> LocalState:
    """Find the LocalState for an already-initialised project.

    By explicit --project-id, else by matching a known project's recorded root
    to the given/working dir. Fails loud if ambiguous or absent."""
    if project_id:
        st = LocalState.load(project_id)
        if not st:
            raise ConvergenceError(f"no local state for project_id '{project_id}' — run init/join first")
        return st
    root = _abs_root(project_root)
    matches = []
    for f in glob.glob(os.path.join(env.convergence_home(), "projects", "*.json")):
        st = LocalState.load(os.path.splitext(os.path.basename(f))[0])
        if st and st.project_root == root:
            matches.append(st)
    if not matches:
        raise ConvergenceError(
            f"no convergence project at {root} — run `init` here first, or pass --project-id")
    if len(matches) > 1:
        ids = ", ".join(s.project_id for s in matches)
        raise ConvergenceError(f"multiple projects at {root}: {ids} — pass --project-id")
    return matches[0]


def _guarded_canonicalize(participant: Participant, text: str, sentinel: str) -> str:
    """Canonicalize, then verify it reverses losslessly. Refuse on failure."""
    canon, _ = participant.canonicalize(text, sentinel)
    back, _ = participant.localize(canon, sentinel)
    if back != text:
        raise ConvergenceError(
            "refusing to push: a transcript did not round-trip losslessly "
            "(run `doctor` to inspect). No data was written.")
    return canon


def _backup_local_context(encoded_dir: str) -> str | None:
    """Copy the local context dir to a timestamped backup before overwriting."""
    src = _local_context_dir(encoded_dir)
    files = _local_context_files(encoded_dir)
    if not files:
        return None
    dst = os.path.join(env.convergence_home(), "backups", encoded_dir, env.now_iso().replace(":", ""))
    os.makedirs(dst, exist_ok=True)
    for f in files:
        shutil.copy2(f, os.path.join(dst, os.path.basename(f)))
    return dst


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
def init(project_root: str | None, cluster_root: str, project_id: str | None = None) -> dict:
    root = _abs_root(project_root)
    from .pathmap import encode_project_dir
    encoded = encode_project_dir(root)
    if not _local_context_files(encoded):
        raise ConvergenceError(
            f"no Claude Code context found for {root}\n"
            f"  expected: {_local_context_dir(encoded)}/*.jsonl")
    pid = project_id or os.path.basename(root)

    cluster = Cluster(cluster_root)
    cluster.ensure()
    if cluster.has_project(pid):
        raise ConvergenceError(
            f"project '{pid}' already exists in the cluster — use `push`, "
            f"or `join` on a new machine")

    mid = env.machine_id()
    now = env.now_iso()
    participant = Participant(
        machine_id=mid, os=env.detected_os(), home=env.home_dir(),
        project_root=root, last_converged=now,
    )
    roster = Roster(project_id=pid, participants=[participant])

    n_files = n_subs = 0
    for f in _local_context_files(encoded):
        canon = _guarded_canonicalize(participant, _read(f), roster.canonical_sentinel)
        cluster.write_context(pid, os.path.basename(f), canon)
        n_files += 1
        n_subs += participant.canonicalize(_read(f), roster.canonical_sentinel)[1]
    cluster.save_roster(roster)

    LocalState(
        project_id=pid, machine_id=mid, cluster_root=cluster.root,
        project_root=root, encoded_dir=encoded, last_converged=now,
    ).save()

    return {"project_id": pid, "machine_id": mid, "files": n_files,
            "substitutions": n_subs, "cluster": cluster.root}


# --------------------------------------------------------------------------- #
# join  (the second machine — design §4.2)
# --------------------------------------------------------------------------- #
def join(project_root: str | None, cluster_root: str, project_id: str | None = None) -> dict:
    """Bring an existing cluster project onto THIS machine, localized.

    The mirror of init: init creates the project from local context; join
    creates local context from the project. Appends this machine to the roster
    and materializes the canonical context into this machine's encoded dir.
    """
    root = _abs_root(project_root)
    from .pathmap import encode_project_dir
    encoded = encode_project_dir(root)

    cluster = Cluster(cluster_root)
    pid = project_id or os.path.basename(root)
    if not cluster.has_project(pid):
        raise ConvergenceError(
            f"no project '{pid}' in cluster {cluster.root} — run `init` on the "
            f"first machine, or pass --project-id")

    roster = cluster.load_roster(pid)
    mid = env.machine_id()
    now = env.now_iso()
    participant = Participant(
        machine_id=mid, os=env.detected_os(), home=env.home_dir(),
        project_root=root, last_converged=now,
    )
    roster.upsert(participant)  # re-join on the same machine replaces its entry

    # Materialize: localize cluster context into this machine's local dir,
    # backing up anything already there first (design §6).
    backup = _backup_local_context(encoded)
    local_dir = _local_context_dir(encoded)
    os.makedirs(local_dir, exist_ok=True)
    n_files = n_subs = 0
    for cf in cluster.context_files(pid):
        text, n = participant.localize(_read(cf), roster.canonical_sentinel)
        with open(os.path.join(local_dir, os.path.basename(cf)), "w", encoding="utf-8") as fh:
            fh.write(text)
        n_files += 1
        n_subs += n

    cluster.save_roster(roster)
    LocalState(
        project_id=pid, machine_id=mid, cluster_root=cluster.root,
        project_root=root, encoded_dir=encoded, last_converged=now,
    ).save()

    return {"project_id": pid, "machine_id": mid, "files": n_files,
            "substitutions": n_subs, "participants": len(roster.participants),
            "backup": backup, "local_dir": local_dir}


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #
def push(project_root: str | None = None, project_id: str | None = None) -> dict:
    st = _resolve_state(project_root, project_id)
    cluster = Cluster(st.cluster_root)
    roster = cluster.load_roster(st.project_id)
    participant = roster.get(st.machine_id)
    if not participant:
        raise ConvergenceError(
            f"this machine ({st.machine_id}) is not in the roster for '{st.project_id}'")

    n_files = n_subs = 0
    for f in _local_context_files(st.encoded_dir):
        text = _read(f)
        canon = _guarded_canonicalize(participant, text, roster.canonical_sentinel)
        cluster.write_context(st.project_id, os.path.basename(f), canon)
        n_files += 1
        n_subs += participant.canonicalize(text, roster.canonical_sentinel)[1]

    now = env.now_iso()
    participant.last_converged = now
    cluster.save_roster(roster)
    st.last_converged = now
    st.save()
    return {"project_id": st.project_id, "files": n_files, "substitutions": n_subs,
            "cluster": cluster.root}


# --------------------------------------------------------------------------- #
# pull
# --------------------------------------------------------------------------- #
def pull(project_root: str | None = None, project_id: str | None = None) -> dict:
    st = _resolve_state(project_root, project_id)
    cluster = Cluster(st.cluster_root)
    roster = cluster.load_roster(st.project_id)
    participant = roster.get(st.machine_id)
    if not participant:
        raise ConvergenceError(
            f"this machine ({st.machine_id}) is not in the roster for '{st.project_id}'")

    backup = _backup_local_context(st.encoded_dir)
    local_dir = _local_context_dir(st.encoded_dir)
    os.makedirs(local_dir, exist_ok=True)

    n_files = n_subs = 0
    for cf in cluster.context_files(st.project_id):
        local_text, n = participant.localize(_read(cf), roster.canonical_sentinel)
        with open(os.path.join(local_dir, os.path.basename(cf)), "w", encoding="utf-8") as fh:
            fh.write(local_text)
        n_files += 1
        n_subs += n

    now = env.now_iso()
    participant.last_converged = now
    cluster.save_roster(roster)
    st.last_converged = now
    st.save()
    return {"project_id": st.project_id, "files": n_files, "substitutions": n_subs,
            "backup": backup, "local_dir": local_dir}


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def status(project_root: str | None = None, project_id: str | None = None) -> dict:
    st = _resolve_state(project_root, project_id)
    cluster = Cluster(st.cluster_root)
    roster = cluster.load_roster(st.project_id)
    participant = roster.get(st.machine_id)
    sentinel = roster.canonical_sentinel

    local_files = {os.path.basename(f): f for f in _local_context_files(st.encoded_dir)}
    cluster_files = {os.path.basename(f): f for f in cluster.context_files(st.project_id)}

    dirty, behind = [], []
    for name, lf in local_files.items():
        if name not in cluster_files:
            dirty.append(name)  # new locally, not yet pushed
        else:
            canon = participant.canonicalize(_read(lf), sentinel)[0] if participant else _read(lf)
            if canon != _read(cluster_files[name]):
                dirty.append(name)
    behind = [n for n in cluster_files if n not in local_files]

    return {
        "project_id": st.project_id,
        "machine_id": st.machine_id,
        "cluster": st.cluster_root,
        "project_root": st.project_root,
        "last_converged": st.last_converged,
        "participants": [(p.machine_id, p.os, p.project_root, p.last_converged)
                         for p in roster.participants],
        "local_count": len(local_files),
        "cluster_count": len(cluster_files),
        "dirty": sorted(dirty),
        "behind": sorted(behind),
    }
