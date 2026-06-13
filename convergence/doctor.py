"""doctor — the honesty command.

Scans a local context directory and reports what the canonicalizer can and
cannot safely round-trip, BEFORE anything is written. This is how the user
trusts the tool with irreplaceable context.

Reports:
  - Inferred project root (and whether inference succeeded).
  - Distinct `cwd` values, flagging subdir-cwds and sibling roots.
  - Path-surface: JSON field locations where the project root appears
    (rewritable) vs. where home-but-not-root paths appear (flagged, per the v1
    project-root-only policy).
  - Sentinel collisions: content that already literally contains the sentinel.
  - A real-data idempotency check: localize(canonicalize(file)) == file, and the
    canonical form contains no surviving root occurrence.
"""

from __future__ import annotations

import glob
import json
import os
from collections import Counter
from dataclasses import dataclass, field

from .pathmap import (
    DEFAULT_SENTINEL,
    canonicalize_jsonl,
    infer_project_root,
    localize_jsonl,
)


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


def _walk_strings(obj, prefix=""):
    """Yield (json_path, string_value) for every string leaf."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{prefix}.{k}")
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v, f"{prefix}[]")
    elif isinstance(obj, str):
        yield prefix, obj


@dataclass
class DoctorReport:
    context_dir: str
    files: list[str] = field(default_factory=list)
    record_count: int = 0
    project_root: str | None = None
    cwds: Counter = field(default_factory=Counter)
    root_fields: Counter = field(default_factory=Counter)
    flagged_home_fields: Counter = field(default_factory=Counter)
    sentinel_collisions: int = 0
    roundtrip_failures: list[str] = field(default_factory=list)
    residue_failures: list[str] = field(default_factory=list)

    @property
    def subdir_cwds(self) -> list[str]:
        if not self.project_root:
            return []
        return [c for c in self.cwds if c != self.project_root and c.startswith(self.project_root + "/")]

    @property
    def sibling_roots(self) -> list[str]:
        """cwds neither equal to nor under the project root — separate trees."""
        if not self.project_root:
            return list(self.cwds)
        return [
            c for c in self.cwds
            if c != self.project_root and not c.startswith(self.project_root + "/")
        ]

    @property
    def ok(self) -> bool:
        return (
            self.project_root is not None
            and not self.roundtrip_failures
            and not self.residue_failures
        )


def scan(context_dir, root=None, home=None, sentinel=DEFAULT_SENTINEL) -> DoctorReport:
    rep = DoctorReport(context_dir=context_dir)
    rep.files = sorted(glob.glob(os.path.join(context_dir, "*.jsonl")))

    # Pass 1: collect cwds so we can infer the root if not supplied.
    for f in rep.files:
        for rec in _iter_records(f):
            rep.record_count += 1
            if isinstance(rec, dict) and isinstance(rec.get("cwd"), str):
                rep.cwds[rec["cwd"]] += 1

    rep.project_root = root or infer_project_root(
        os.path.basename(context_dir.rstrip("/")), list(rep.cwds)
    )
    home = home or os.path.expanduser("~")

    if rep.project_root:
        root_str = rep.project_root
        # Pass 2: path-surface inventory + per-file idempotency on real bytes.
        for f in rep.files:
            with open(f, encoding="utf-8", errors="replace") as fh:
                original = fh.read()
            canon, _ = canonicalize_jsonl(original, root_str, sentinel)
            back, _ = localize_jsonl(canon, root_str, sentinel)
            if back != original:
                rep.roundtrip_failures.append(f)
            # Canonical form must retain no boundary-anchored root occurrence.
            if root_str in canon:
                rep.residue_failures.append(f)

            for rec in _iter_records(f):
                for jpath, sval in _walk_strings(rec):
                    if sentinel in sval:
                        rep.sentinel_collisions += 1
                    if root_str in sval:
                        rep.root_fields[jpath] += 1
                    elif home in sval:
                        rep.flagged_home_fields[jpath] += 1
    return rep


def format_report(rep: DoctorReport) -> str:
    L = []
    L.append(f"doctor: {rep.context_dir}")
    L.append(f"  files: {len(rep.files)}   records: {rep.record_count}")
    if rep.project_root:
        L.append(f"  project root: {rep.project_root}  (inferred OK)")
    else:
        L.append("  project root: COULD NOT INFER — supply --root explicitly")
        L.append(f"    observed cwds: {list(rep.cwds)[:5]}")
        return "\n".join(L)

    if rep.subdir_cwds:
        L.append(f"  note: {len(rep.subdir_cwds)} subdir cwd(s) under root (handled): "
                 + ", ".join(rep.subdir_cwds[:3]))
    if rep.sibling_roots:
        L.append(f"  FLAG: {len(rep.sibling_roots)} sibling root(s) outside project "
                 "(not rewritten in v1):")
        for s in rep.sibling_roots[:5]:
            L.append(f"        {s}")

    L.append(f"  rewritable — root appears in {sum(rep.root_fields.values())} string(s):")
    for jp, n in rep.root_fields.most_common(12):
        L.append(f"        {n:7d}  {jp or '<string>'}")
    if rep.flagged_home_fields:
        L.append(f"  flagged — home-but-not-root paths in "
                 f"{sum(rep.flagged_home_fields.values())} string(s) (left as-is):")
        for jp, n in rep.flagged_home_fields.most_common(8):
            L.append(f"        {n:7d}  {jp or '<string>'}")
    if rep.sentinel_collisions:
        L.append(f"  note: sentinel string already present in {rep.sentinel_collisions} "
                 "place(s); escaped on canonicalize (round-trip verified below).")

    L.append("  idempotency on real data:")
    L.append(f"        round-trip localize(canonicalize(x))==x : "
             + ("PASS" if not rep.roundtrip_failures else f"FAIL ({len(rep.roundtrip_failures)})"))
    L.append(f"        no root residue after canonicalize        : "
             + ("PASS" if not rep.residue_failures else f"FAIL ({len(rep.residue_failures)})"))
    L.append(f"  => {'OK' if rep.ok else 'NEEDS ATTENTION'}")
    return "\n".join(L)
