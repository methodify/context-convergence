"""The verbs: init / join / push / pull / sync / status (design §4).

Branch-per-project model: each project is an orphan branch `project/<id>` in one
cluster repo; convergence keeps a single-branch working clone per project under
`~/.convergence/clones/<id>/`, so a machine only ever fetches the project it
wants. A user hands the tool a --remote; the clone is managed for them. (A local
no-git cluster directory still works via --cluster, mainly for tests.)

Publishing operations (init/join/push) run through a retry loop — if the branch
advanced under us, sync_down rebases our world on the new tip and we re-derive
and re-publish. Safety (design §6): push runs a per-file round-trip guard and
refuses to ship anything it cannot losslessly reverse; pull backs up the local
context dir before overwriting; merges union records rather than dropping either.
"""

from __future__ import annotations

import glob
import os
import shutil

from . import env, gitutil, secrets
from .localstate import LocalState
from .pathmap import encode_project_dir, normalize_jsonl
from .roster import Participant, Roster
from .transport import PushRejected, open_transport, project_branch, union_jsonl

_PUBLISH_ATTEMPTS = 5


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
            f"no convergence project at {root} — run `init`/`join` here first, or pass --project-id")
    if len(matches) > 1:
        raise ConvergenceError(
            f"multiple projects at {root}: {', '.join(s.project_id for s in matches)} — pass --project-id")
    return matches[0]


def _guarded_canonicalize(participant: Participant, text: str, rewrite_home: bool) -> str:
    """Canonicalize, verifying the PATH rewriting reverses without data loss.
    Compared against the normalized (compact-reserialized) form, not raw bytes,
    so incidental JSON formatting is not mistaken for a round-trip failure."""
    canon, _ = participant.canonicalize(text, rewrite_home)
    if participant.localize(canon, rewrite_home)[0] != normalize_jsonl(text):
        raise ConvergenceError(
            "refusing to push: a transcript did not round-trip losslessly "
            "(run `doctor` to inspect). No data was written.")
    return canon


def _backup_local_context(encoded_dir: str) -> str | None:
    files = _local_context_files(encoded_dir)
    if not files:
        return None
    dst = os.path.join(env.convergence_home(), "backups", encoded_dir, env.now_iso().replace(":", ""))
    os.makedirs(dst, exist_ok=True)
    for f in files:
        shutil.copy2(f, os.path.join(dst, os.path.basename(f)))
    return dst


def _publishing(op):
    """Run a publishing op, retrying if the branch advanced under us. Each
    attempt re-syncs and re-derives from local truth, so retries are clean."""
    last = None
    for _ in range(_PUBLISH_ATTEMPTS):
        try:
            return op()
        except PushRejected as e:
            last = e
    raise ConvergenceError(f"could not publish after {_PUBLISH_ATTEMPTS} attempts "
                           f"(branch kept advancing): {last}")


def _write_canonical(transport, participant, encoded, rewrite_home, *, union):
    """Canonicalize this machine's local context into the cluster working tree.
    On push, union with whatever the branch already holds so no records drop."""
    n_files = n_subs = 0
    for f in _local_context_files(encoded):
        text = _read(f)
        canon = _guarded_canonicalize(participant, text, rewrite_home)
        name = os.path.basename(f)
        if union:
            existing = transport.cluster.read_context(name)
            if existing is not None:
                canon = union_jsonl(existing, canon)
        transport.cluster.write_context(name, canon)
        n_files += 1
        n_subs += participant.canonicalize(text, rewrite_home)[1]
    return n_files, n_subs


def _save_state(pid, mid, transport, root, encoded, remote, now):
    LocalState(
        project_id=pid, machine_id=mid, cluster_root=transport.cluster.root,
        project_root=root, encoded_dir=encoded, remote=remote,
        branch=project_branch(pid) if remote else None,
        last_converged=now, last_converged_commit=gitutil.current_commit(transport.cluster.root),
    ).save()


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
def init(project_root, remote=None, cluster=None, project_id=None, rewrite_home=True) -> dict:
    root = _abs_root(project_root)
    encoded = encode_project_dir(root)
    if not _local_context_files(encoded):
        raise ConvergenceError(
            f"no Claude Code context found for {root}\n"
            f"  expected: {_local_context_dir(encoded)}/*.jsonl")
    pid = project_id or os.path.basename(root)
    transport = open_transport(pid, remote, cluster)
    if transport.exists():
        raise ConvergenceError(
            f"project '{pid}' already exists in the cluster — use `push`, or `join`")
    mid, now = env.machine_id(), env.now_iso()

    def op():
        transport.ensure()  # creates the orphan branch (+ main README on a fresh cluster)
        participant = Participant(machine_id=mid, os=env.detected_os(),
                                  home=env.home_dir(), project_root=root, last_converged=now)
        roster = Roster(project_id=pid, rewrite_home=rewrite_home, participants=[participant])
        n = _write_canonical(transport, participant, encoded, roster.rewrite_home, union=False)
        transport.cluster.save_roster(roster)
        transport.publish(f"init {pid} from {mid}")
        return n

    n_files, n_subs = _publishing(op)
    _save_state(pid, mid, transport, root, encoded, remote, now)
    return {"project_id": pid, "machine_id": mid, "files": n_files, "substitutions": n_subs,
            "cluster": transport.cluster.root, "remote": remote, "branch": project_branch(pid)}


# --------------------------------------------------------------------------- #
# join
# --------------------------------------------------------------------------- #
def join(project_root, remote=None, cluster=None, project_id=None) -> dict:
    root = _abs_root(project_root)
    encoded = encode_project_dir(root)
    pid = project_id or os.path.basename(root)
    transport = open_transport(pid, remote, cluster)
    if not transport.exists():
        raise ConvergenceError(
            f"no project '{pid}' in cluster — run `init` on the first machine, or pass --project-id")
    mid, now = env.machine_id(), env.now_iso()
    result = {}

    def op():
        transport.ensure()      # single-branch clone of just this project
        transport.sync_down()
        roster = transport.cluster.load_roster()
        participant = Participant(machine_id=mid, os=env.detected_os(),
                                  home=env.home_dir(), project_root=root, last_converged=now)
        roster.upsert(participant)
        transport.cluster.save_roster(roster)
        transport.publish(f"join {pid} from {mid}")
        result.update(participant=participant, rewrite_home=roster.rewrite_home,
                      participants=len(roster.participants))

    _publishing(op)

    backup = _backup_local_context(encoded)
    local_dir = _local_context_dir(encoded)
    os.makedirs(local_dir, exist_ok=True)
    n_files = n_subs = 0
    for cf in transport.cluster.context_files():
        text, n = result["participant"].localize(_read(cf), result["rewrite_home"])
        with open(os.path.join(local_dir, os.path.basename(cf)), "w", encoding="utf-8") as fh:
            fh.write(text)
        n_files += 1
        n_subs += n

    _save_state(pid, mid, transport, root, encoded, remote, now)
    return {"project_id": pid, "machine_id": mid, "files": n_files, "substitutions": n_subs,
            "participants": result["participants"], "backup": backup, "local_dir": local_dir}


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #
def push(project_root=None, project_id=None, scan_secrets=False, strict_secrets=False) -> dict:
    st = _resolve_state(project_root, project_id)
    warnings = _scan_files(st.encoded_dir) if scan_secrets else {}
    if warnings and strict_secrets:
        n = sum(len(v) for v in warnings.values())
        raise ConvergenceError(
            f"refusing to push: {n} apparent secret(s) found across {len(warnings)} file(s) — "
            f"run `convergence scan`, or push without --strict. Nothing was written.")
    transport = open_transport(st.project_id, st.remote, st.cluster_root)
    now = env.now_iso()
    out = {}

    def op():
        transport.ensure()
        transport.sync_down()
        roster = transport.cluster.load_roster()
        participant = roster.get(st.machine_id)
        if not participant:
            raise ConvergenceError(
                f"this machine ({st.machine_id}) is not in the roster for '{st.project_id}'")
        nf, ns = _write_canonical(transport, participant, st.encoded_dir, roster.rewrite_home, union=True)
        participant.last_converged = now
        transport.cluster.save_roster(roster)
        transport.publish(f"push {st.project_id} from {st.machine_id}")
        out["files"], out["subs"] = nf, ns

    _publishing(op)
    st.last_converged = now
    st.last_converged_commit = gitutil.current_commit(transport.cluster.root)
    st.save()
    return {"project_id": st.project_id, "files": out["files"], "substitutions": out["subs"],
            "cluster": transport.cluster.root, "secret_warnings": warnings}


# --------------------------------------------------------------------------- #
# pull
# --------------------------------------------------------------------------- #
def pull(project_root=None, project_id=None) -> dict:
    st = _resolve_state(project_root, project_id)
    transport = open_transport(st.project_id, st.remote, st.cluster_root)
    transport.ensure()
    transport.sync_down()
    roster = transport.cluster.load_roster()
    participant = roster.get(st.machine_id)
    if not participant:
        raise ConvergenceError(
            f"this machine ({st.machine_id}) is not in the roster for '{st.project_id}'")

    backup = _backup_local_context(st.encoded_dir)
    local_dir = _local_context_dir(st.encoded_dir)
    os.makedirs(local_dir, exist_ok=True)
    n_files = n_subs = 0
    for cf in transport.cluster.context_files():
        text, n = participant.localize(_read(cf), roster.rewrite_home)
        with open(os.path.join(local_dir, os.path.basename(cf)), "w", encoding="utf-8") as fh:
            fh.write(text)
        n_files += 1
        n_subs += n

    st.last_converged = env.now_iso()
    st.last_converged_commit = gitutil.current_commit(transport.cluster.root)
    st.save()
    return {"project_id": st.project_id, "files": n_files, "substitutions": n_subs,
            "backup": backup, "local_dir": local_dir}


def sync(project_root=None, project_id=None) -> dict:
    pl = pull(project_root, project_id)
    ph = push(project_root, project_id)
    return {"project_id": ph["project_id"], "pulled": pl["files"],
            "pushed": ph["files"], "backup": pl["backup"]}


# --------------------------------------------------------------------------- #
# secret scan (design §6.5)
# --------------------------------------------------------------------------- #
def _scan_files(encoded_dir: str) -> dict:
    out = {}
    for f in _local_context_files(encoded_dir):
        findings = secrets.scan_text(_read(f))
        if findings:
            out[os.path.basename(f)] = findings
    return out


def scan_local(project_root=None, project_id=None) -> dict:
    st = _resolve_state(project_root, project_id)
    return _scan_files(st.encoded_dir)


# --------------------------------------------------------------------------- #
# projects (discovery) + status
# --------------------------------------------------------------------------- #
def list_projects(remote: str) -> list[str]:
    """Project ids in a cluster repo — the set of `project/<id>` branches."""
    return sorted(b[len("project/"):] for b in gitutil.remote_branches(remote)
                  if b.startswith("project/"))


def status(project_root=None, project_id=None) -> dict:
    st = _resolve_state(project_root, project_id)
    transport = open_transport(st.project_id, st.remote, st.cluster_root)
    transport.ensure()
    transport.sync_down()  # reflect the branch's latest in "behind"
    cluster = transport.cluster
    roster = cluster.load_roster()
    participant = roster.get(st.machine_id)

    local_files = {os.path.basename(f): f for f in _local_context_files(st.encoded_dir)}
    cluster_files = {os.path.basename(f): f for f in cluster.context_files()}

    dirty = []
    for name, lf in local_files.items():
        if name not in cluster_files:
            dirty.append(name)
        elif participant:
            canon = participant.canonicalize(_read(lf), roster.rewrite_home)[0]
            remote_text = cluster.read_context(name)
            if union_jsonl(remote_text, canon) != remote_text:
                dirty.append(name)
    behind = [n for n in cluster_files if n not in local_files]

    return {
        "project_id": st.project_id, "machine_id": st.machine_id,
        "cluster": st.cluster_root, "remote": st.remote, "branch": st.branch,
        "project_root": st.project_root, "last_converged": st.last_converged,
        "participants": [(p.machine_id, p.os, p.project_root, p.last_converged)
                         for p in roster.participants],
        "local_count": len(local_files), "cluster_count": len(cluster_files),
        "dirty": sorted(dirty), "behind": sorted(behind),
    }
