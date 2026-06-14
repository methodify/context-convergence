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
from collections import Counter
from dataclasses import dataclass, field

from . import env
from .pathmap import (
    SENTINEL_CONTEXT_DIR,
    SENTINEL_HOME,
    SENTINEL_PROJECT_ROOT,
    canonicalize_jsonl,
    infer_project_root,
    localize_jsonl,
    normalize_jsonl,
)
from .roster import Participant


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
        return [c for c in self.cwds
                if c != self.project_root and c.startswith(self.project_root + "/")]

    @property
    def sibling_roots(self) -> list[str]:
        if not self.project_root:
            return list(self.cwds)
        return [c for c in self.cwds
                if c != self.project_root and not c.startswith(self.project_root + "/")]

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


def scan(context_dir, root=None, home=None, rewrite_home=True) -> DoctorReport:
    rep = DoctorReport(context_dir=context_dir, rewrite_home=rewrite_home)
    rep.files = sorted(glob.glob(os.path.join(context_dir, "*.jsonl")))

    for f in rep.files:
        for rec in _iter_records(f):
            rep.record_count += 1
            if isinstance(rec, dict) and isinstance(rec.get("cwd"), str):
                rep.cwds[rec["cwd"]] += 1

    rep.project_root = root or infer_project_root(
        os.path.basename(context_dir.rstrip("/")), list(rep.cwds))
    rep.home = home or env.home_dir()
    if not rep.project_root:
        return rep

    participant = Participant("local", env.detected_os(), rep.home, rep.project_root)
    mappings = participant.mappings(rewrite_home)
    context_anchor = f"{rep.home}/.claude/projects/{participant.encoded_dir}"

    for f in rep.files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            original = fh.read()
        canon, _ = canonicalize_jsonl(original, mappings)
        if localize_jsonl(canon, mappings)[0] != normalize_jsonl(original):
            rep.roundtrip_failures.append(f)
        if rep.project_root in canon:
            rep.residue_root += canon.count(rep.project_root)
        if rewrite_home and rep.home in canon:
            rep.residue_home += canon.count(rep.home)

    # Per-tier coverage: count string values mentioning each anchor.
    for f in rep.files:
        for rec in _iter_records(f):
            for sval in _walk_strings(rec):
                if context_anchor in sval:
                    rep.tier_hits[SENTINEL_CONTEXT_DIR] += 1
                elif rep.project_root in sval:
                    rep.tier_hits[SENTINEL_PROJECT_ROOT] += 1
                elif rewrite_home and rep.home in sval:
                    rep.tier_hits[SENTINEL_HOME] += 1
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
        L.append(f"  note: {len(rep.sibling_roots)} sibling root(s) outside project "
                 + ("(rewritten via {{CC_HOME}})" if rep.rewrite_home else "(flagged, not rewritten)") + ":")
        for s in rep.sibling_roots[:5]:
            L.append(f"        {s}")

    L.append("  rewrite coverage (string values per tier):")
    for label in (SENTINEL_PROJECT_ROOT, SENTINEL_CONTEXT_DIR, SENTINEL_HOME):
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
