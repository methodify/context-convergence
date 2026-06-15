Machine A (where submatrix is today)

# 1. Install convergence (from the context-convergence checkout)
pip install -e /path/to/context-convergence

# 2. Point it at your cluster repo (once per machine)
convergence remote set https://github.com/yobryon/context-clusters

# 3. ⚠ SAFETY GATE — verify it understands this machine's paths BEFORE writing anything.
#    Point it at submatrix's context dir under ~/.claude/projects/<encoded>/
convergence doctor ~/.claude/projects/<submatrix-encoded-dir>
#    Want: "project root: ... (inferred OK)", round-trip PASS, residue PASS.
#    If it says "COULD NOT INFER" or shows residue -> STOP, don't init (see caveat).

# 4. Register submatrix in the cluster (creates branch project/submatrix, pushes A's context)
convergence init <submatrix-project-root> --project-id submatrix
#    e.g. convergence init ~/src/submatrix --project-id submatrix

# 5. (optional) auto-sync at session end
convergence hook install

I'm recommending --project-id submatrix explicitly on both machines so the branch matches even if the folder basename differs across OSes — don't rely on the basename default for a cross-platform project.

Machine B (the MacBook)

# 1. Install + point at the same cluster
pip install -e /path/to/context-convergence
convergence remote set https://github.com/yobryon/context-clusters

# 2. Clone the SOURCE (separate from convergence — code travels its own way)
git clone <submatrix-source-repo> ~/src/submatrix

# 3. Pull just submatrix's CONTEXT, localized to this Mac's paths
convergence join ~/src/submatrix --project-id submatrix
#    -> materializes ~/.claude/projects/<mac-encoded>/ with transcripts + memory,
#       all paths rewritten to /Users/<you>/src/submatrix/...

# 4. Resume the main session and branch it for mac work
cd ~/src/submatrix
ls ~/.claude/projects/<mac-encoded>/*.jsonl   # find the main session's guid
claude -r <guid>                              # resume it (now with A's full history, localized)
#    then inside Claude Code:  /branch mac-support

/branch forks into a new session (new guid) — that's exactly what keeps you safe: A keeps growing {guid}, B's mac work goes into the new guid file. Different files → clean union, no divergence.

Day-to-day (both machines)

convergence sync      # push your changes, pull the other side's (or let the Stop hook do it)
convergence status    # what's dirty/behind, roster, and any unresolved memory conflicts
convergence projects  # list everything in your cluster

What converges, per our M2 work:
- New/branched sessions (different guids) → union, clean.
- MEMORY.md index → both sides' entries unioned, no conflict.
- Shared docs (backlog, project-state) → 3-way merged by region; only true same-line overlaps surface <<<<<<< markers (resolve, then sync).
- Same guid grown on both → detected, not concatenated; warned, local lineage kept in backup.


