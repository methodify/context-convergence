"""Merge strategies for convergence (all in canonical space, so machine-neutral).

Two kinds of context merge differently:

- **Transcripts (.jsonl)** are an append-only record. Disjoint appends union
  cleanly; the pathology is the SAME session grown on two machines (two diverging
  conversations), which cannot be merged into a coherent thread — we detect that
  (`is_diverged`) and refuse to concatenate, surfacing it instead.

- **Memory (.md)** are living documents, frequently co-edited (a shared backlog,
  a project-state note). A line/hunk-level 3-way merge against the last-converged
  base merges non-overlapping edits silently and leaves conflict markers only on
  genuine same-region edits. `git merge-file` IS diff3 and needs no repo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile


def is_diverged(ours: str, theirs: str) -> bool:
    """True if two versions of an append-only transcript diverged — i.e. both
    have records beyond their common prefix (neither is just an extension of the
    other). Equal, or one a prefix of the other, is NOT divergence."""
    a = ours.splitlines()
    b = theirs.splitlines()
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n < len(a) and n < len(b)


def three_way_merge(base: str, ours: str, theirs: str, *, union: bool = False,
                    ours_label="local", theirs_label="cluster") -> tuple[str, int]:
    """Line-level 3-way merge via `git merge-file` (diff3). Returns
    (merged_text, n_conflicts). Non-overlapping changes merge cleanly;
    overlapping changes are wrapped in `<<<<<<< / ======= / >>>>>>>` markers and
    counted. No git repo required.

    `union=True` (for append-only indexes like MEMORY.md) keeps BOTH sides of any
    overlap with no markers — so two machines each appending a bullet merge
    cleanly instead of conflicting. Always returns 0 conflicts."""
    d = tempfile.mkdtemp(prefix="cc-merge-")
    try:
        po, pb, pt = (os.path.join(d, n) for n in ("ours", "base", "theirs"))
        for path, content in ((po, ours), (pb, base), (pt, theirs)):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        args = ["git", "merge-file", "-p"]
        args += ["--union"] if union else ["-L", ours_label, "-L", "base", "-L", theirs_label]
        proc = subprocess.run(args + [po, pb, pt], capture_output=True, text=True)
        if proc.returncode == 255:  # git merge-file error
            raise RuntimeError(f"git merge-file failed: {proc.stderr.strip()}")
        return proc.stdout, proc.returncode  # returncode == number of conflicts
    finally:
        shutil.rmtree(d, ignore_errors=True)
