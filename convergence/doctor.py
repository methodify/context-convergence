"""doctor — the honesty command.

Scans a local context directory and reports what the canonicalizer can and
cannot safely round-trip, BEFORE anything is written. This is how the user
trusts the tool with irreplaceable context.

Reports:
  - Inferred project root (and whether inference succeeded).
  - Distinct `cwd` values, flagging subdir-cwds and sibling roots.
  - Per-tier rewrite coverage: how many string values contain the project root,
    the own context dir, and (under the home-rewrite policy) the home prefix.
  - Residue: any real project-root or home path still present after canonicalize
    (would be machine-specific in the cluster).
  - A real-data idempotency check: localize(canonicalize(file)) reverses without
    data loss.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field

from . import env
from .pathmap import (
    SENTINEL_CONTEXT_DIR,
    SENTINEL_ENCODED_DIR,
    SENTINEL_HOME,
    SENTINEL_PROJECT_ROOT,
    canonicalize_jsonl,
    infer_project_root,
    localize_jsonl,
    normalize_jsonl,
)
from .roster import Participant


def _under(child: str, parent: str) -> bool:
    """Is `child` `parent` or a path beneath it? Separator-agnostic (POSIX `/`
    and Windows `\\`) so Windows subdir cwds aren't misreported as siblings."""
    return child == parent or child.startswith(parent + "/") or child.startswith(parent + "\\")


def _iter_records(path: str):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _walk_strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)
    elif isinstance(obj, str):
        yield obj


@dataclass
class DoctorReport:
    context_dir: str
    files: list[str] = field(default_factory=list)
    record_count: int = 0
    project_root: str | None = None
    home: str = ""
    rewrite_home: bool = True
    cwds: Counter = field(default_factory=Counter)
    tier_hits: Counter = field(default_factory=Counter)  # sentinel label -> #strings
    residue_root: int = 0
    residue_home: int = 0
    roundtrip_failures: list[str] = field(default_factory=list)

    @property
    def subdir_cwds(self) -> list[str]:
        if not self.project_root:
            return []
        return [c for c in self.cwds if c != self.project_root and _under(c, self.project_root)]

    @property
    def sibling_roots(self) -> list[str]:
        if not self.project_root:
            return list(self.cwds)
        return [c for c in self.cwds if not _under(c, self.project_root)]

    @property
    def ok(self) -> bool:
        # Home residue is advisory, not a failure: under best-effort home
        # rewriting, nested paths (e.g. /System/Volumes/Data/Users/... firmlinks)
        # and malformed doubled-home paths legitimately remain, and they
        # round-trip losslessly. Only lossy round-trips or project-root residue
        # are real problems.
        return (self.project_root is not None
                and not self.roundtrip_failures
                and self.residue_root == 0)


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Per-file workers (module level so they're picklable for ProcessPoolExecutor).
# Each streams its file line-by-line (bounded memory) and returns small results.
# --------------------------------------------------------------------------- #
def _file_cwds(path: str):
    rc = 0
    cwds = Counter()
    for rec in _iter_records(path):
        rc += 1
        if isinstance(rec, dict) and isinstance(rec.get("cwd"), str):
            cwds[rec["cwd"]] += 1
    return path, rc, cwds


def _file_check(path, mappings, sep, project_root, home, rewrite_home,
                context_anchor, encoded_dir):
    roundtrip_ok = True
    residue_root = residue_home = 0
    tier = Counter()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            canon, _ = canonicalize_jsonl(line, mappings, sep)
            if roundtrip_ok and localize_jsonl(canon, mappings, sep)[0] != normalize_jsonl(line):
                roundtrip_ok = False
            if project_root in canon:
                residue_root += canon.count(project_root)
            if rewrite_home and home in canon:
                residue_home += canon.count(home)
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for sval in _walk_strings(rec):
                if context_anchor in sval:
                    tier[SENTINEL_CONTEXT_DIR] += 1
                elif project_root in sval:
                    tier[SENTINEL_PROJECT_ROOT] += 1
                elif encoded_dir in sval:
                    tier[SENTINEL_ENCODED_DIR] += 1
                elif rewrite_home and home in sval:
                    tier[SENTINEL_HOME] += 1
    return path, roundtrip_ok, residue_root, residue_home, tier


def _run(tasks, workers, progress, label):
    """Run (func, args) tasks, in a process pool when workers > 1, else
    sequentially. Falls back to sequential on any pool failure (e.g. fussy
    native-Windows multiprocessing). Yields results as they complete, printing
    per-file progress."""
    n = len(tasks)
    if workers > 1:
        try:
            from concurrent.futures import ProcessPoolExecutor, as_completed
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(fn, *args): args[0] for fn, args in tasks}
                done = 0
                for fut in as_completed(futs):
                    done += 1
                    if progress:
                        _progress(f"  {label} {done}/{n}  {os.path.basename(futs[fut])}")
                    yield fut.result()
            return
        except Exception as e:  # noqa: BLE001 — degrade, never fail the scan
            if progress:
                _progress(f"  (parallel unavailable: {e}; scanning sequentially)")
    for i, (fn, args) in enumerate(tasks, 1):
        if progress:
            _progress(f"  {label} {i}/{n}  {os.path.basename(args[0])} "
                      f"({os.path.getsize(args[0]) // 1048576} MB)")
        yield fn(*args)


def scan(context_dir, root=None, home=None, rewrite_home=True, progress=True,
         workers=None) -> DoctorReport:
    """Stream-scan a context dir with bounded memory, in parallel across files.
    Each JSONL line is an independent document, so per-line work parallelizes and
    the per-line round-trip equals the whole-file one."""
    rep = DoctorReport(context_dir=context_dir, rewrite_home=rewrite_home)
    rep.files = sorted(glob.glob(os.path.join(context_dir, "*.jsonl")))
    n = len(rep.files)
    if not n:
        return rep
    workers = max(1, min(workers or (os.cpu_count() or 1), n))

    # Pass 1: collect cwds to infer the root.
    for _path, rc, cwds in _run([(_file_cwds, (f,)) for f in rep.files],
                                workers, progress, "reading"):
        rep.record_count += rc
        rep.cwds.update(cwds)

    rep.project_root = root or infer_project_root(
        os.path.basename(context_dir.rstrip("/")), list(rep.cwds))
    rep.home = home or env.home_dir()
    if not rep.project_root:
        return rep

    participant = Participant("local", env.detected_os(), rep.home, rep.project_root)
    mappings = participant.mappings(rewrite_home)
    sep = participant.native_sep
    context_anchor = f"{rep.home}/.claude/projects/{participant.encoded_dir}"

    # Pass 2: round-trip + residue + tier coverage, in parallel across files.
    tasks = [(_file_check, (f, mappings, sep, rep.project_root, rep.home,
                            rewrite_home, context_anchor, participant.encoded_dir))
             for f in rep.files]
    for path, roundtrip_ok, res_root, res_home, tier in _run(tasks, workers, progress, "scanning"):
        if not roundtrip_ok:
            rep.roundtrip_failures.append(path)
        rep.residue_root += res_root
        rep.residue_home += res_home
        rep.tier_hits.update(tier)
    return rep


def format_report(rep: DoctorReport) -> str:
    L = [f"doctor: {rep.context_dir}",
         f"  files: {len(rep.files)}   records: {rep.record_count}"]
    if not rep.project_root:
        L.append("  project root: COULD NOT INFER — supply --root explicitly")
        L.append(f"    observed cwds: {list(rep.cwds)[:5]}")
        return "\n".join(L)

    L.append(f"  project root: {rep.project_root}  (inferred OK)")
    L.append(f"  home: {rep.home}   rewrite-home policy: {'on' if rep.rewrite_home else 'off'}")
    if rep.subdir_cwds:
        L.append(f"  note: {len(rep.subdir_cwds)} subdir cwd(s) under root (handled): "
                 + ", ".join(rep.subdir_cwds[:3]))
    if rep.sibling_roots:
        L.append(f"  note: {len(rep.sibling_roots)} external root(s) outside this project "
                 "(rewritten only where they fall under home; another project's paths stay "
                 "machine-specific):")
        for s in rep.sibling_roots[:5]:
            L.append(f"        {s}")

    L.append("  rewrite coverage (string values per tier):")
    for label in (SENTINEL_PROJECT_ROOT, SENTINEL_CONTEXT_DIR, SENTINEL_ENCODED_DIR, SENTINEL_HOME):
        L.append(f"        {rep.tier_hits.get(label, 0):7d}  {label}")
    L.append("  idempotency on real data:")
    L.append("        round-trip localize(canonicalize(x)) == x : "
             + ("PASS" if not rep.roundtrip_failures else f"FAIL ({len(rep.roundtrip_failures)})"))
    L.append(f"        residue — project root in canonical       : "
             + ("PASS" if rep.residue_root == 0 else f"{rep.residue_root} occurrence(s)"))
    if rep.rewrite_home:
        L.append(f"        residue — home path in canonical (advisory): "
                 + ("PASS" if rep.residue_home == 0
                    else f"{rep.residue_home} (nested/malformed paths; lossless)"))
    L.append(f"  => {'OK' if rep.ok else 'NEEDS ATTENTION'}")
    return "\n".join(L)
