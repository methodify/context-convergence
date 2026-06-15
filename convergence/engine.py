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
import tempfile

from . import env, gitutil, merge, secrets
from .errors import ConvergenceError, LockBusy  # noqa: F401 (re-exported)
from .localstate import LocalState
from .lock import project_lock
from .pathmap import (
    CANON_VERSION,
    canonicalize_jsonl,
    canonicalize_value,
    encode_project_dir,
    localize_jsonl,
    localize_value,
    normalize_jsonl,
)
from .roster import Participant, Roster
from .transport import PushRejected, open_transport, project_branch, union_jsonl

_PUBLISH_ATTEMPTS = 5


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _abs_root(project_root: str | None) -> str:
    return os.path.abspath(os.path.expanduser(project_root or os.getcwd()))


def _local_context_dir(encoded_dir: str) -> str:
    return os.path.join(env.claude_projects_dir(), encoded_dir)


def _local_context_files(encoded_dir: str) -> list[str]:
    """Top-level session transcripts only (used for the 'has context' check)."""
    return sorted(glob.glob(os.path.join(_local_context_dir(encoded_dir), "*.jsonl")))


def _kind_of(relpath: str) -> str:
    return "jsonl" if relpath.endswith(".jsonl") else "text"


def _fingerprint(path: str) -> list:
    """A cheap change token (size, mtime_ns) — the git/make/rsync heuristic. A
    `--full` run bypasses it for the rare edit that preserves both."""
    s = os.stat(path)
    return [s.st_size, s.st_mtime_ns]


def _current_fingerprints(encoded_dir: str) -> dict:
    base = _local_context_dir(encoded_dir)
    return {rel: _fingerprint(os.path.join(base, rel)) for rel, _ in _context_entries(encoded_dir)}


def _context_entries(encoded_dir: str) -> list[tuple[str, str]]:
    """Files convergence syncs from the local context dir, as (relpath, kind).

    Included: top-level `*.jsonl` transcripts, and the entire `memory/` subtree
    (markdown — as core as the transcripts on modern Claude Code). EXCLUDED: the
    per-session `<uuid>/` subfolders that hold tool results and subagent
    transcripts — treated as ephemeral for now. kind is 'jsonl' or 'text'.
    """
    base = _local_context_dir(encoded_dir)
    entries = [(os.path.basename(f), "jsonl")
               for f in sorted(glob.glob(os.path.join(base, "*.jsonl")))]
    for f in sorted(glob.glob(os.path.join(base, "memory", "**", "*"), recursive=True)):
        if os.path.isfile(f):
            # relpaths are ALWAYS forward-slash: that's git's and the cluster's
            # convention, and Windows file APIs accept `/` too. os.path.relpath
            # would emit `\` on Windows, breaking git pathspecs (show/diff).
            entries.append((os.path.relpath(f, base).replace(os.sep, "/"), "text"))
    return entries


def _read(path: str) -> str:
    """Read a context file STRICTLY. A non-UTF-8 byte would be silently swapped
    for U+FFFD under errors='replace' and then written back mangled — and the
    push round-trip guard can't catch it (it compares the mangled read to
    itself). So fail loud instead: better to refuse than to corrupt."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except UnicodeDecodeError as e:
        raise ConvergenceError(
            f"{path} is not valid UTF-8 (at byte {e.start}); refusing to process "
            f"it to avoid corrupting context. Inspect the file, then retry.")


def _atomic_write(path: str, text: str) -> None:
    """Write `text` to `path` atomically: a temp file in the same directory then
    os.replace (an atomic rename). A crash mid-write leaves the existing file
    intact — never a truncated/half-written live context file."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".convergence-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)  # atomic on the same filesystem
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


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


def _guarded_canonicalize(participant: Participant, text: str, kind: str, rewrite_home: bool):
    """Canonicalize one file, verifying the canonical form is STABLE through the
    sync loop: canonicalize(localize(canon)) == canon. That is the property a
    convergence tool actually needs — the canonical bytes don't drift as the
    content round-trips across machines. We do NOT require localize to reproduce
    the exact original bytes, because path-separator style is semantically null
    (a Windows path's `/` and `\\` tail are the same path) and localize
    normalizes it to the machine's native separator. A genuine data-losing
    rewrite would still change the re-canonicalized form and be caught."""
    maps = participant.mappings(rewrite_home)
    sep = participant.native_sep
    if kind == "jsonl":
        canon, n = canonicalize_jsonl(text, maps, sep)
        ok = canonicalize_jsonl(localize_jsonl(canon, maps, sep)[0], maps, sep)[0] == canon
    else:
        canon, n = canonicalize_value(text, maps, sep)
        ok = canonicalize_value(localize_value(canon, maps, sep)[0], maps, sep)[0] == canon
    if not ok:
        raise ConvergenceError(
            "refusing to push: a file's canonical form is not stable through a "
            "round-trip (run `doctor` to inspect). No data was written.")
    return canon, n


def _localize_entry(participant: Participant, text: str, kind: str, rewrite_home: bool):
    maps = participant.mappings(rewrite_home)
    sep = participant.native_sep
    return localize_jsonl(text, maps, sep) if kind == "jsonl" else localize_value(text, maps, sep)


def _same_local_file(path: str, text: str) -> bool:
    """True if `path` already holds exactly `text`. Tolerant: an unreadable or
    non-UTF-8 file counts as different, so pull overwrites (fixes) it."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read() == text
    except (OSError, UnicodeDecodeError):
        return False


def _localize_into_local(transport, participant, encoded, rewrite_home, relpaths=None):
    """Localize cluster files into the local context dir, preserving relative
    structure. `relpaths=None` means every file (full pull); otherwise only the
    given subset (incremental pull — what git says changed). Returns the count of
    files ACTUALLY written — a target whose localized form already matches the
    local file is left untouched (no mtime churn), so localizing this machine's
    own just-pushed files is a no-op rather than a write that re-marks them dirty."""
    local_dir = _local_context_dir(encoded)
    targets = transport.cluster.context_files() if relpaths is None else relpaths
    n_files = n_subs = 0
    for relpath in targets:
        text = transport.cluster.read_context(relpath)
        if text is None:  # listed as changed but absent (deletion) — skip, never delete local
            continue
        text, n = _localize_entry(participant, text, _kind_of(relpath), rewrite_home)
        dest = os.path.join(local_dir, relpath)
        if _same_local_file(dest, text):
            continue  # already byte-identical — don't churn its mtime or count it
        _atomic_write(dest, text)
        n_files += 1
        n_subs += n
    return n_files, n_subs


_BACKUP_KEEP = 10  # timestamped backups retained per project


def _backup_local_context(encoded_dir: str, relpaths=None) -> str | None:
    """Back up synced files before pull/join overwrites them, preserving relative
    structure. `relpaths=None` backs up everything; otherwise only the given
    subset (incremental pull only overwrites those, so only those need backing
    up). Collision-proof dir name; pruned to the newest _BACKUP_KEEP."""
    base = _local_context_dir(encoded_dir)
    if relpaths is None:
        entries = _context_entries(encoded_dir)
    else:
        entries = [(r, _kind_of(r)) for r in relpaths
                   if os.path.exists(os.path.join(base, r))]
    if not entries:
        return None
    parent = os.path.join(env.convergence_home(), "backups", encoded_dir)
    stamp = env.now_iso().replace(":", "")
    dst = os.path.join(parent, stamp)
    i = 2
    while os.path.exists(dst):  # don't overwrite an existing same-second backup
        dst = os.path.join(parent, f"{stamp}-{i}")
        i += 1
    for relpath, _ in entries:
        target = os.path.join(dst, relpath)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(os.path.join(base, relpath), target)
    _prune_backups(parent)
    return dst


def _prune_backups(parent: str, keep: int = _BACKUP_KEEP) -> None:
    """Keep only the newest `keep` backup dirs under `parent`. Scoped strictly to
    `~/.convergence/backups/...` — never touches user content."""
    try:
        dirs = sorted(d for d in os.listdir(parent) if os.path.isdir(os.path.join(parent, d)))
    except FileNotFoundError:
        return
    for old in dirs[:-keep]:
        shutil.rmtree(os.path.join(parent, old), ignore_errors=True)


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


def _write_canonical(transport, participant, encoded, rewrite_home, *, union,
                     base_commit=None, prev_fingerprints=None, canon_ok=True, dry_run=False):
    """Canonicalize this machine's local context into the cluster working tree
    and merge with whatever the branch already holds. Returns
    (n_files, n_subs, conflicts, fingerprints, n_skipped, changed). With
    dry_run=True it computes the full plan (changed files, conflicts) but writes
    nothing to the cluster — the round-trip guard still runs.

    INCREMENTAL: a file whose (size, mtime) matches `prev_fingerprints` AND which
    is already present in the cluster is skipped untouched — that is the whole
    point. Skipping is safe because an unchanged local file already reached the
    cluster at our last push, and any later cluster change either supersets our
    records or is a divergence that keeps our lineage (so the cluster still has
    our content). The cluster-presence check guards a wiped/re-cloned cluster;
    `canon_ok` False (a canonicalizer-version bump, or `--full`) reprocesses all.

    - identical / new file -> write as-is.
    - transcript (.jsonl) that differs: union the appends UNLESS the two have
      genuinely diverged (both grew the same session) — then keep the cluster's
      lineage and flag it; never concatenate two threads into gibberish.
    - memory (.md) that differs: line-level 3-way merge against the base
      (last-converged commit); clean merges flow silently, real conflicts land
      conflict markers in the file and are flagged.
    """
    conflicts: list[dict] = []
    fingerprints: dict = {}
    changed: list[str] = []
    n_subs = n_skipped = 0
    incremental = canon_ok and prev_fingerprints is not None
    base_dir = _local_context_dir(encoded)

    def _write(relpath, content):
        if not dry_run:
            transport.cluster.write_context(relpath, content)

    for relpath, kind in _context_entries(encoded):
        path = os.path.join(base_dir, relpath)
        fp = _fingerprint(path)
        fingerprints[relpath] = fp
        if (incremental and prev_fingerprints.get(relpath) == fp
                and transport.cluster.has_context(relpath)):
            n_skipped += 1
            continue
        text = _read(path)
        canon, n = _guarded_canonicalize(participant, text, kind, rewrite_home)
        existing = transport.cluster.read_context(relpath) if union else None

        if existing is None or existing == canon:
            _write(relpath, canon)
        elif kind == "jsonl":
            if merge.is_diverged(canon, existing):
                conflicts.append({"path": relpath, "kind": "session-divergence"})
                # keep the cluster's lineage; the local one stays put and is
                # captured by pull's backup. Do NOT concatenate two threads.
            else:
                _write(relpath, union_jsonl(existing, canon))
        elif base_commit is None:
            # No 3-way base (local no-git cluster / single machine) — can't
            # distinguish an edit from a conflict, so local wins.
            _write(relpath, canon)
        else:  # memory document that differs -> line-level 3-way merge
            base = gitutil.show_file(transport.cluster.root, base_commit,
                                     "context/" + relpath) or ""
            # MEMORY.md is an append-only index: union both sides' bullets rather
            # than conflicting on add/add at the end. Other docs get true 3-way.
            is_index = os.path.basename(relpath) == "MEMORY.md"
            merged, nconf = merge.three_way_merge(base, canon, existing, union=is_index)
            _write(relpath, merged)
            if nconf:
                conflicts.append({"path": relpath, "kind": "memory-conflict", "count": nconf})
        changed.append(relpath)
        n_subs += n
    return len(changed), n_subs, conflicts, fingerprints, n_skipped, changed


def _save_state(pid, mid, transport, root, encoded, remote, now, *,
                fingerprints=None, last_localized_commit=None):
    commit = gitutil.current_commit(transport.cluster.root)
    LocalState(
        project_id=pid, machine_id=mid, cluster_root=transport.cluster.root,
        project_root=root, encoded_dir=encoded, remote=remote,
        branch=project_branch(pid) if remote else None,
        last_converged=now, last_converged_commit=commit,
        file_fingerprints=fingerprints, last_localized_commit=last_localized_commit,
        canon_version=CANON_VERSION,
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
    with project_lock(pid):
        return _init(root, encoded, pid, remote, cluster, rewrite_home)


def _init(root, encoded, pid, remote, cluster, rewrite_home) -> dict:
    transport = open_transport(pid, remote, cluster)
    if transport.exists():
        raise ConvergenceError(
            f"project '{pid}' already exists in the cluster — use `push`, or `join`")
    mid, now = env.machine_id(), env.now_iso()
    out = {}

    def op():
        transport.ensure()  # creates the orphan branch (+ main README on a fresh cluster)
        participant = Participant(machine_id=mid, os=env.detected_os(),
                                  home=env.home_dir(), project_root=root, last_converged=now)
        roster = Roster(project_id=pid, rewrite_home=rewrite_home, participants=[participant])
        nf, ns, _conf, fps, _sk, _ch = _write_canonical(
            transport, participant, encoded, roster.rewrite_home, union=False)
        transport.cluster.save_roster(roster)
        transport.publish(f"init {pid} from {mid}")
        out.update(files=nf, subs=ns, fingerprints=fps)

    _publishing(op)
    # The cluster commit we just created reflects local exactly, so it is also our
    # pull baseline (last_localized_commit).
    _save_state(pid, mid, transport, root, encoded, remote, now,
                fingerprints=out["fingerprints"],
                last_localized_commit=gitutil.current_commit(transport.cluster.root))
    return {"project_id": pid, "machine_id": mid, "files": out["files"],
            "substitutions": out["subs"], "cluster": transport.cluster.root,
            "remote": remote, "branch": project_branch(pid)}


# --------------------------------------------------------------------------- #
# join
# --------------------------------------------------------------------------- #
def join(project_root, remote=None, cluster=None, project_id=None) -> dict:
    root = _abs_root(project_root)
    encoded = encode_project_dir(root)
    pid = project_id or os.path.basename(root)
    with project_lock(pid):
        return _join(root, encoded, pid, remote, cluster)


def _join(root, encoded, pid, remote, cluster) -> dict:
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
    n_files, n_subs = _localize_into_local(transport, result["participant"],
                                           encoded, result["rewrite_home"])
    # Local now reflects the cluster HEAD (pull baseline); record fingerprints of
    # the just-materialized files so the first push after join is incremental.
    _save_state(pid, mid, transport, root, encoded, remote, now,
                fingerprints=_current_fingerprints(encoded),
                last_localized_commit=gitutil.current_commit(transport.cluster.root))
    return {"project_id": pid, "machine_id": mid, "files": n_files, "substitutions": n_subs,
            "participants": result["participants"], "backup": backup,
            "local_dir": _local_context_dir(encoded)}


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #
def push(project_root=None, project_id=None, scan_secrets=False, strict_secrets=False,
         full=False, dry_run=False) -> dict:
    st = _resolve_state(project_root, project_id)
    with project_lock(st.project_id):
        return _push(st, scan_secrets, strict_secrets, full, dry_run)


def _push(st, scan_secrets=False, strict_secrets=False, full=False, dry_run=False) -> dict:
    warnings = _scan_files(st.encoded_dir) if scan_secrets else {}
    if warnings and strict_secrets and not dry_run:
        n = sum(len(v) for v in warnings.values())
        raise ConvergenceError(
            f"refusing to push: {n} apparent secret(s) found across {len(warnings)} file(s) — "
            f"run `convergence scan`, or push without --strict. Nothing was written.")
    transport = open_transport(st.project_id, st.remote, st.cluster_root)
    now = env.now_iso()
    # Incremental unless --full, the canonicalizer changed, or there's no prior state.
    canon_ok = (not full) and st.canon_version == CANON_VERSION
    out = {}

    def op():
        transport.ensure()
        transport.sync_down()
        roster = transport.cluster.load_roster()
        participant = roster.get(st.machine_id)
        if not participant:
            raise ConvergenceError(
                f"this machine ({st.machine_id}) is not in the roster for '{st.project_id}'")
        nf, ns, conflicts, fps, skipped, changed = _write_canonical(
            transport, participant, st.encoded_dir, roster.rewrite_home,
            union=True, base_commit=st.last_converged_commit,
            prev_fingerprints=st.file_fingerprints, canon_ok=canon_ok, dry_run=dry_run)
        if not dry_run:
            if nf:  # only stamp/commit the roster when something actually changed
                participant.last_converged = now
                transport.cluster.save_roster(roster)
            transport.publish(f"push {st.project_id} from {st.machine_id}")
        out.update(files=nf, subs=ns, conflicts=conflicts, fingerprints=fps,
                   skipped=skipped, changed=changed)

    if dry_run:
        op()  # one pass, nothing published; no state saved
    else:
        _publishing(op)
        st.last_converged = now
        st.last_converged_commit = gitutil.current_commit(transport.cluster.root)
        st.file_fingerprints = out["fingerprints"]
        st.canon_version = CANON_VERSION
        st.save()
    return {"project_id": st.project_id, "files": out["files"], "substitutions": out["subs"],
            "skipped": out["skipped"], "cluster": transport.cluster.root,
            "secret_warnings": warnings, "conflicts": out["conflicts"],
            "changed": out["changed"], "dry_run": dry_run}


# --------------------------------------------------------------------------- #
# pull
# --------------------------------------------------------------------------- #
def pull(project_root=None, project_id=None, full=False, dry_run=False) -> dict:
    st = _resolve_state(project_root, project_id)
    with project_lock(st.project_id):
        return _pull(st, full, dry_run)


def _pull(st, full=False, dry_run=False) -> dict:
    transport = open_transport(st.project_id, st.remote, st.cluster_root)
    transport.ensure()
    transport.sync_down()
    roster = transport.cluster.load_roster()
    participant = roster.get(st.machine_id)
    if not participant:
        raise ConvergenceError(
            f"this machine ({st.machine_id}) is not in the roster for '{st.project_id}'")

    head = gitutil.current_commit(transport.cluster.root)
    # Incremental: localize only the files git says changed since we last
    # localized. Full when forced, when the canonicalizer changed, with no
    # baseline, on a non-git cluster, or if git can't compute the diff.
    relpaths = None
    canon_ok = (not full) and st.canon_version == CANON_VERSION
    if canon_ok and st.remote and st.last_localized_commit and head:
        changed = gitutil.diff_names(transport.cluster.root,
                                     st.last_localized_commit, head, "context")
        if changed is not None:
            relpaths = [c[len("context/"):] for c in changed if c.startswith("context/")]

    if dry_run:
        targets = relpaths if relpaths is not None else transport.cluster.context_files()
        local_dir = _local_context_dir(st.encoded_dir)
        new = [r for r in targets if not os.path.exists(os.path.join(local_dir, r))]
        overwrite = [r for r in targets if os.path.exists(os.path.join(local_dir, r))]
        return {"project_id": st.project_id, "files": len(targets), "substitutions": 0,
                "backup": None, "local_dir": local_dir, "dry_run": True,
                "new": sorted(new), "overwrite": sorted(overwrite)}

    backup = _backup_local_context(st.encoded_dir, relpaths)
    n_files, n_subs = _localize_into_local(transport, participant,
                                           st.encoded_dir, roster.rewrite_home, relpaths)
    # Localizing can rewrite a file on disk (path rewrite / separator
    # normalization bumps its mtime). Refresh the push fingerprints for EXACTLY
    # the files we localized, so the next push doesn't see its own just-pulled
    # files as "changed" and re-push them — which made every sync churn forever.
    # Only the localized set: an untouched local-ahead edit must still look dirty.
    localized = transport.cluster.context_files() if relpaths is None else relpaths
    local_dir = _local_context_dir(st.encoded_dir)
    fps = dict(st.file_fingerprints or {})
    for rel in localized:
        p = os.path.join(local_dir, rel)
        if os.path.exists(p):
            fps[rel] = _fingerprint(p)
    st.file_fingerprints = fps
    st.last_converged = env.now_iso()
    st.last_converged_commit = head
    st.last_localized_commit = head
    st.canon_version = CANON_VERSION
    st.save()
    return {"project_id": st.project_id, "files": n_files, "substitutions": n_subs,
            "backup": backup, "local_dir": _local_context_dir(st.encoded_dir), "dry_run": False}


def sync(project_root=None, project_id=None) -> dict:
    """Push THEN pull. Push first so this machine's local-ahead content (memory
    edited in place, a transcript continued since last push) is union-merged into
    the cluster before pull overwrites the local dir — otherwise a pull-first
    sync would clobber unpushed local work (recoverable only from backup). The
    lock is held across both halves so nothing interleaves between them."""
    return sync_full(project_root, project_id, full=False)


def sync_full(project_root=None, project_id=None, full=False, dry_run=False) -> dict:
    st = _resolve_state(project_root, project_id)
    with project_lock(st.project_id):
        ph = _push(st, full=full, dry_run=dry_run)
        pl = _pull(st, full=full, dry_run=dry_run)
    return {"project_id": ph["project_id"], "pulled": pl["files"],
            "pushed": ph["files"], "skipped": ph["skipped"], "backup": pl["backup"],
            "conflicts": ph["conflicts"], "dry_run": dry_run,
            "push_changed": ph["changed"], "pull_new": pl.get("new", []),
            "pull_overwrite": pl.get("overwrite", [])}


# --------------------------------------------------------------------------- #
# secret scan (design §6.5)
# --------------------------------------------------------------------------- #
def _scan_files(encoded_dir: str) -> dict:
    out = {}
    base = _local_context_dir(encoded_dir)
    for relpath, _kind in _context_entries(encoded_dir):
        findings = secrets.scan_text(_read(os.path.join(base, relpath)))
        if findings:
            out[relpath] = findings
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
    with project_lock(st.project_id):
        return _status(st)


def _status(st) -> dict:
    transport = open_transport(st.project_id, st.remote, st.cluster_root)
    transport.ensure()
    transport.sync_down()  # reflect the branch's latest in "behind"
    cluster = transport.cluster
    roster = cluster.load_roster()
    participant = roster.get(st.machine_id)

    base = _local_context_dir(st.encoded_dir)
    local_entries = dict(_context_entries(st.encoded_dir))   # relpath -> kind
    cluster_files = set(cluster.context_files())
    maps = participant.mappings(roster.rewrite_home) if participant else None
    sep = participant.native_sep if participant else "/"

    dirty = []
    for relpath, kind in local_entries.items():
        if relpath not in cluster_files:
            dirty.append(relpath)
        elif maps:
            remote_text = cluster.read_context(relpath)
            if kind == "jsonl":
                canon = canonicalize_jsonl(_read(os.path.join(base, relpath)), maps, sep)[0]
                if union_jsonl(remote_text, canon) != remote_text:
                    dirty.append(relpath)
            else:
                canon = canonicalize_value(_read(os.path.join(base, relpath)), maps, sep)[0]
                if canon != remote_text:
                    dirty.append(relpath)
    behind = [n for n in cluster_files if n not in local_entries]

    # Unresolved memory merge conflicts persist as conflict markers in the file.
    conflicted = []
    for relpath, kind in local_entries.items():
        if kind != "jsonl" and "<<<<<<< " in _read(os.path.join(base, relpath)):
            conflicted.append(relpath)

    return {
        "project_id": st.project_id, "machine_id": st.machine_id,
        "cluster": st.cluster_root, "remote": st.remote, "branch": st.branch,
        "project_root": st.project_root, "last_converged": st.last_converged,
        "participants": [(p.machine_id, p.os, p.project_root, p.last_converged)
                         for p in roster.participants],
        "conflicted": sorted(conflicted),
        "local_count": len(local_entries), "cluster_count": len(cluster_files),
        "dirty": sorted(dirty), "behind": sorted(behind),
    }
