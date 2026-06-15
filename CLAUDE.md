# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

**Sprints 0–4 + the home-path gap are complete** (2026-06-13/14), all stdlib Python, 64 tests green.
- **Sprint 0** — canonicalize/localize core + `doctor`. Idempotency invariant proven against real transcripts (242 / 105,623 / 42,714 records — all round-trip losslessly). See `docs/sprint-0-findings.md` for what the dogfooding overturned.
- **Sprint 1** — single-machine roundtrip: `init`/`push`/`pull`/`status` against a local cluster dir, real `roster.json`, roster of one. Byte-identical roundtrip proven on this project's own context.
- **Sprint 2** — second machine: `join`, multi-participant roster, localize-on-checkout. Headline loop verified — context authored on machine B reaches machine A localized to A's paths, cluster staying machine-neutral.
- **Sprint 3** — git transport: `--remote` makes the cluster a working clone of a private git repo; `sync` = pull then push. Verified over a hermetic bare repo: init→join→sync loop, disjoint-session union across machines, accumulating git history.
- **Sprint 4** — seamless layer + safety: the blessed Stop-hook (`hook install` wires a global Claude Code Stop hook running `hook-sync`, which resolves the project from cwd and syncs, failing soft so it can never break a session) and the opt-in secret scan (`scan`, `push --scan-secrets [--strict]`, design §6.5).
- **Home-path gap closed** — three-tier rewriting (root + own context dir + home prefix) replaced project-root-only. On real corpora this drove home residue from thousands to ~0 (catalog 9144→0); the key unlock was rewriting path-keyed dict KEYS, not just values. The remaining handful are lossless nested/malformed paths (advisory).

`docs/context-convergence-design.md` remains the product source of truth. The v1 build order is done. **Remaining** is optional: Sprint 5 (background watcher mode, §5.2) and packaging (a `pyproject.toml` for a clean `convergence` console script — the Stop hook currently runs via `PYTHONPATH`).

### Storage & transport model (branch-per-project)

**One cluster repo, one orphan branch per project** (`project/<id>`). A machine keeps a **single-branch working clone per project** under `~/.convergence/clones/<id>/`, so cloning one project never fetches the others' history and pushing one project never conflicts with another (different branches). The repo's `main` branch holds only a human-readable README. The set of `project/*` branches *is* the project registry — `convergence projects --remote <url>` lists them via `ls-remote`, no clone. A project's tree is flat: `roster.json` + `context/*.jsonl` at the branch root (no `projects/<id>/` nesting — the branch *is* the project).

git is **pure transport + history**; all merging happens in canonical space via `transport.union_jsonl`, so the clone is a cache of the branch and git-level conflicts never arise. Each publishing op (`init`/`join`/`push`) runs `sync_down` (fetch + `reset --hard origin/<branch>`) → re-derives canonical state from local truth, unioned with the branch's latest → commit → push, retrying if non-fast-forward.

**Incremental sync (Stage 1, file-level)** — sync touches only what changed. **push** skips a file whose `(size, mtime_ns)` matches the fingerprint stored from last push AND which still exists in the cluster (`LocalState.file_fingerprints`; the presence check guards a wiped clone). **pull** localizes only files `gitutil.diff_names(<last_localized_commit>, HEAD)` reports, backing up only those. Subtleties baked in (and tested): the pull baseline is a **separate** `last_localized_commit` — conflating it with `last_converged_commit` makes another machine's sessions never localize after a push; a `pathmap.CANON_VERSION` mismatch forces a full pass (a canonicalizer upgrade re-processes "unchanged" files — bump it when canonicalization changes); `--full` overrides; fingerprints persist only after a successful push; a pure-skip push writes nothing (no roster bump → no commit). *(Stage 2 — line-level append for giant single sessions — is TODO; a changed file is still reprocessed whole.)* The everyday merge is append-mostly union: disjoint sessions union; a session extended on two machines keeps both sides. `init` creates the orphan branch (+ seeds `main`'s README on a fresh cluster); `join` does `clone --single-branch`. Without `--remote`, `open_transport` returns a `LocalTransport` and a `--cluster <dir>` holds one project (mainly a test convenience).

**Language: Python, stdlib-only — locked in** (2026-06-13). The ship-language question is closed; Rust was ruled out. Don't add third-party deps (tests use `unittest`). Keep the property tests as the correctness spec.

### Commands

```sh
python3 -m unittest discover -s tests              # run the suite (no deps)
python3 -m unittest discover -s tests -v           # verbose
python3 -m unittest tests.test_engine              # one module

# the cluster is one private git repo; each project is a branch project/<id>.
# clone is managed under ~/.convergence/clones/<id>/ — you just pass --remote.
python3 -m convergence init <project_root> --remote <git-url> [--project-id ID] [--no-rewrite-home]
python3 -m convergence join <project_root> --remote <git-url> [--project-id ID]
python3 -m convergence projects [--remote <git-url>]         # list projects (branches)
python3 -m convergence remote [show|set <url>|clear]         # machine-level default remote
python3 -m convergence push|pull|sync|status [project_root] [--project-id ID]
#   (--cluster <dir> instead of --remote = a local no-git cluster, one project per dir)
# --remote is optional on init/join/projects: falls back to the default remote
# (config.resolve_remote: explicit > CONVERGENCE_REMOTE env > stored default).
# The first git --remote a machine uses is auto-adopted as its default.
python3 -m convergence push <project_root> --scan-secrets [--strict]   # opt-in secret scan
python3 -m convergence scan [project_root]                             # secret scan, no sync
python3 -m convergence hook install|uninstall|status [--event Stop|SessionEnd]

# low-level path mapping (Sprint 0)
python3 -m convergence doctor <context_dir>        # scan + safety report (infers root)
python3 -m convergence canonicalize <ctx_dir> <out_dir> [--root R]
python3 -m convergence localize <in_dir> <ctx_dir> --root R   # R = target machine's root
```

Real context dirs live at `~/.claude/projects/<encoded-dir>/`. `doctor` on this project's own dir is the fastest smoke test. **When testing the sync verbs, always set `CLAUDE_PROJECTS_DIR`, `CONVERGENCE_HOME` (and `CONVERGENCE_MACHINE_ID`/`CONVERGENCE_NOW`) to temp dirs** — `pull` writes into `~/.claude/projects/`, which holds irreplaceable real context. All four env overrides live in `convergence/env.py`.

### Module map

`pathmap.py` (path-mapping core, no I/O) · `roster.py` (`Participant`/`Roster` + persistence) · `cluster.py` (`Cluster` = one project's tree: `roster.json` + `context/`) · `config.py` (machine-level default remote) · `errors.py` (`ConvergenceError`/`LockBusy`) · `lock.py` (per-project flock) · `localstate.py` (per-machine project marker → its clone, remote, branch) · `env.py` (location/clock/machine-id/os/settings/`clone_dir` resolution, all env-overridable) · `gitutil.py` (branch-aware git wrappers: orphan branches, single-branch clone, `ls-remote`, `show_file`) · `transport.py` (`LocalTransport`/`GitTransport` + `project_branch` + `union_jsonl`) · `merge.py` (`three_way_merge` via `git merge-file`, `is_diverged`) · `secrets.py` (curated secret patterns) · `hooks.py` (Stop-hook install + soft-failing `hook_sync`) · `engine.py` (the verbs + `list_projects`; fail-loud round-trip guard on push, backup-before-overwrite on pull, publish-retry, opt-in secret scan) · `doctor.py` (honesty scan) · `__main__.py` (CLI).

**Cross-platform (Windows ⇄ Mac/Linux):**
- **Separator translation** — canonical form uses `/` universally inside rewritten paths. The rewrite consumes each matched path's *tail* and normalizes its separators: canonicalize source-native→`/`, localize `/`→target-native. The native sep comes from each participant's `os` in the roster (`Participant.native_sep`), so POSIX is a pure no-op (every conversion is identity off Windows) and the canonical form is **OS-neutral** (Windows and Mac canonicalize identical content identically → clean cross-platform git merges). The tail regex (`pathmap._TAIL`) uses a restrictive path-name segment class so it never consumes across a token boundary; worst case it stops early at an unusual filename char (harmless — nothing past the last captured separator needs normalizing).
- **Termination** — `_ancestors`/path-walking terminates via `dirname(p) == p` (true at POSIX `/` AND Windows `C:\`), never `p != "/"` — the latter infinite-loops on Windows drive roots (was a 66 GB RAM blowout in `doctor`).
- `doctor` streams line-by-line with progress (bounded memory on 500 MB+ transcripts) and classifies subdir-vs-sibling separator-agnostically (`_under`).
- **Still POSIX-only:** the `fcntl` concurrency lock no-ops on native Windows (no per-project serialization there).

The push guard compares against `normalize_jsonl(text)` (compact re-serialization), not raw bytes — it verifies *data* reversibility, not incidental whitespace. Tests are sandboxed via the `env.py` overrides; `CLAUDE_SETTINGS_PATH` redirects hook install away from the real settings.json.

## What this tool is

`convergence` is a CLI that treats Claude Code project context (the JSONL transcripts under `~/.claude/projects/<encoded-path>/`) as a portable, multi-machine asset. It syncs that context to a private git repo (a "context cluster") and **deep-rewrites machine-local absolute paths** on checkout so the context resolves correctly on whatever machine pulls it. The code already lives in the user's own git repo and is out of scope — convergence syncs *context only*.

## The load-bearing idea (do not lose this)

The whole product is a bet that **the path-surface inside transcripts is enumerable and rewriting is safely reversible.** Everything else is plumbing.

- **Canonical form** is what lives in the cluster repo: machine-specific anchors are replaced by sentinels. The cluster never stores any one machine's local paths as authoritative — this is what keeps the repo diffable and lets git's own merge machinery work instead of every line colliding on differing absolute paths.
- **Four rewrite tiers** (`build_mappings`, applied longest-anchor-first so specific beats general), a cluster-wide policy fixed at init (`roster.rewrite_home`):
  1. project root → `{{CC_PROJECT_ROOT}}`
  2. `<home>/.claude/projects/<encoded>` → `{{CC_PROJECT_CONTEXT_DIR}}` (own context dir; both home AND the lossy encoded segment change per machine, so it gets its own exact sentinel)
  3. `<encoded_dir>` → `{{CC_ENCODED_DIR}}` (the bare encoded dir name — appears standalone or inside tilde paths `~/.claude/projects/<encoded>`, common in **memory** files where the absolute context-dir tier doesn't reach; localizes the tilde case correctly because `~` is already portable and the sentinel fixes the encoded segment)
  4. `<home>` → `{{CC_HOME}}` (covers `~/.claude/*`, dotfiles, and sibling projects by the `~/src/{project}` convention; opt out with `init --no-rewrite-home`)
  Not rewritten (inherent limit): *other* projects' encoded dirs (e.g. a memory note citing `~/.claude/projects/-Users-…-catalog/`) — there's no portable target since we can't know machine B's layout for a different project.
- **canonicalize** (local → canonical) and **localize** (canonical → local) are the two core transforms, parameterized by a machine's **roster** entry (`home`, `project_root`, `encoded_dir`).
- The correctness core is the invariant `canonicalize(localize(x)) == x` and vice versa, for any participant. This must have **property tests** — it is the spec.

### Hard rules for the rewriter

These were validated against real data in Sprint 0 — see `docs/sprint-0-findings.md` and the docstring of `convergence/pathmap.py`. Three corrections that a future instance must not undo:

- **Rewrite JSON-decoded string values, not raw file text.** In raw JSONL a path after a newline reads `...catalog\n/Users/...` — the `\n` escape makes the boundary indistinguishable from `/mnt/Users/.../catalog` (root as suffix of a longer path, must NOT rewrite). Parse each line, rewrite within each decoded string leaf, re-serialize. Compact dumps (`ensure_ascii=False, separators=(',',':')`) reproduce Claude Code's bytes exactly (verified 105,623/105,623), so diffs stay clean.
- **The encoded dir name is lossy — never decode it.** Encoding maps every non-alphanumeric char to `-`, so the root is unrecoverable from the dir name. Get the root from a `cwd` field or the roster. `infer_project_root` recovers it by *encoding* cwd ancestors and matching the dir name (also handles subdir cwds).
- **Boundary-anchored, never naked substring replace.** Rewrite an anchor only as a whole path prefix: not preceded by alphanumeric; not followed by a name char (`catalog-backup`/`catalog2`/`catalog_old` survive); a `.` is an extension only if followed by alphanumeric (`catalog.bak` survives) but a trailing `.` before whitespace/quote/end is punctuation and IS rewritten. The leading "not preceded by alphanumeric" guard protects nested paths (e.g. macOS firmlink `/System/Volumes/Data/Users/…`) — at the cost of leaving rare malformed doubled-home paths un-rewritten (lossless; doctor reports them as advisory).
- **Rewrite dict KEYS, not just values.** Tool results keep path-keyed maps (snapshot `trackedFileBackups` keyed by absolute path); those keys are as machine-specific as values. `_map_strings` transforms both. (Missing this left thousands of un-rewritten home refs — caught by dogfooding doctor's residue check.)

Other invariants:

- **Refs with no exact equivalent stay machine-specific** (lossless, advisory in doctor): another project's context dir (its encoded segment can't be recomputed for B), nested/firmlink paths, malformed doubled-home paths. Everything with an exact per-machine equivalent (root, own context dir, home prefix) IS rewritten.
- **Literal-sentinel escaping is load-bearing here.** This repo's own context contains `{{CC_PROJECT_ROOT}}` verbatim. The `{{S(_LIT)*}}` escape family makes canonicalize/localize a true inverse pair; don't remove it or convergence can't sync its own context.
- **Fail loud, never guess.** Never ship a half-rewritten transcript. Context is irreplaceable; the prime directive is *never silently corrupt or lose context.*

### Corruption resistance (local context dir is sacred)

The protections that keep `~/.claude/projects/` safe — do not weaken these:

- **Nothing deletes local context.** The only `rmtree` in the package is `_prune_backups`, scoped strictly to `~/.convergence/backups/<encoded>/` (keeps the newest `_BACKUP_KEEP`). Nothing ever removes a file under `~/.claude/projects/`.
- **git never touches the local dir.** `reset --hard`/`clean -fd` run only with `cwd =` the clone (`~/.convergence/clones/<id>/`). `open_transport` **refuses** any cluster/clone path overlapping `claude_projects_dir` (`transport._assert_safe_cluster_dir`) — guards the `--cluster` footgun.
- **Backup before overwrite.** pull/join copy every synced file (byte-exact) to `~/.convergence/backups/<encoded>/<ts>/` *before* writing; if backup throws, the run aborts before any overwrite.
- **Atomic writes.** Local writes go through `_atomic_write` (temp in same dir + `os.replace`) — a crash mid-write leaves the existing file intact, never truncated.
- **`sync` is push-THEN-pull.** Push first so this machine's local-ahead content (memory edited in place, a continued transcript) is union-merged into the cluster *before* pull overwrites the local dir. Pull-first would clobber unpushed local work (recoverable only from backup). Don't reorder.
- **Per-project lock** (`lock.project_lock`, an `flock` on `~/.convergence/locks/<id>.lock`). init/join/push/pull/sync/status acquire it non-blocking; a second concurrent run raises `LockBusy` (the Stop hook logs-and-skips, a manual command tells the user to retry). Re-entrant within a process (sync holds it across push+pull). flock auto-releases on process exit, so no stale locks.
- **Round-trip guard on push** refuses any file that doesn't reverse losslessly.
- **Strict reads on the sync path.** `engine._read` and `Cluster.read_context` read UTF-8 strictly and raise `ConvergenceError` on a decode error, rather than `errors="replace"` silently swapping bad bytes for U+FFFD and writing them back (which the guard can't catch). (doctor stays tolerant — it's read-only analysis.)
- **Backups are collision-proof and pruned** (`-N` suffix on same-second; newest `_BACKUP_KEEP` retained).
- **Memory is 3-way merged, not LWW** (see Merge model above) — co-edits on different regions converge; true overlaps surface as conflicts rather than silently dropping a side.

## Working with real context data

The tool operates on `~/.claude/projects/<encoded-dir>/`. The encoded dir is the absolute project path with **every non-alphanumeric char** mapped to `-` (e.g. this project: `-Users-bryonwilliams-src-context-convergence`). Each dir holds `<session-uuid>.jsonl` transcripts, a `memory/` subdir (the persistent file memory), and per-session `<uuid>/` subfolders (tool results / subagent transcripts).

**What syncs** (`engine._context_entries`): top-level `*.jsonl` transcripts **and the entire `memory/` subtree** — memory is first-class context on modern Claude Code. Memory files are markdown (kind `text`): no JSON escaping, so they're path-rewritten with the text-level `canonicalize_value`/`localize_value` (same four-tier mappings) and compared byte-for-byte by the push guard. **Excluded for now:** the per-session `<uuid>/` subfolders (tool results / subagents) — treated as ephemeral; revisit if their absence bites. (Tilde paths like `~/.claude/...` in memory are left as-is — already portable; only absolute paths are rewritten.)

**Merge model** (`merge.py`, `engine._write_canonical`): the cluster keeps the canonical form; merges happen in canonical space (machine-neutral) on push.
- **Transcripts (.jsonl)** — append-only records. Identical/extension → write; disjoint case → `union_jsonl`; but if the SAME session was grown on two machines (`merge.is_diverged`: both have records past their common prefix), it is **not** concatenated — the cluster's lineage is kept and a `session-divergence` conflict is surfaced (the local lineage is preserved in pull's backup). Two diverging conversations can't become a coherent thread.
- **Memory (.md)** — living documents. Line-level **3-way merge** (`git merge-file` = diff3) against the base (`last_converged_commit` via `gitutil.show_file`): non-overlapping edits (different backlog items) merge silently; overlapping same-region edits land `<<<<<<<` markers and a `memory-conflict` is surfaced. `MEMORY.md` is special-cased to `--union` (append-only index — both bullets kept, never conflicts). No git base (local no-git cluster) → local-wins.
Conflicts surface loudly on `push`/`sync` and `status` lists memory files still carrying markers. Always test the canonicalizer against **real** transcript dirs, not synthetic ones — that is what surfaced every Sprint 0 correction. Calibration corpora for pressure-testing: `~/.claude/projects/-Users-bryonwilliams-src-catalog` and `-Users-bryonwilliams-src-deepdrift-zero` (both large; the latter exercises the lossy dot-root and sibling-root cases).

Ad-hoc probe for where the project path hides in a transcript:

```sh
python3 - "<file>.jsonl" <<'PY'
import sys,json
root="<project-root>"
hits=set()
def walk(o,p=""):
    if isinstance(o,dict):
        for k,v in o.items(): walk(v,p+"."+k)
    elif isinstance(o,list):
        for v in o: walk(v,p+"[]")
    elif isinstance(o,str) and root in o: hits.add(p)
for l in open(sys.argv[1]): walk(json.loads(l))
print("\n".join(sorted(hits)))
PY
```

## CLI surface (target)

`init` (first machine: register project in cluster) · `join` (new machine: pull + localize here) · `push` (localize→canonicalize→commit) · `pull` (fetch→merge→localize) · `sync` (pull then push — the everyday verb) · `status` · `roster` · `doctor` (validate path-surface before touching anything). Design the CLI to be boring, legible, and dry-run-friendly — it manages something precious.

## Build order (Sprint 0 gates everything)

1. **Sprint 0 — Spike the canonicalizer** against a real transcript dir; prove the idempotency invariant. Output: `doctor` + canonicalize/localize as a library, no sync. **Non-negotiable and gates the rest.**
2. Single-machine roundtrip (`init`/`push`/`pull` against a local cluster dir).
3. Second machine (`join` + multi-participant roster + localize-on-checkout) — the headline feature.
4. Git transport (private GitHub repo; append-mostly union merge; conflict surfacing).
5. Seamless layer (Stop-hook `sync`; backups; secret-scan flag).

Sync strategy is **git + append-mostly union + last-writer-wins-with-backup**, not CRDT. JSONL is append-mostly, so divergence is usually a union of disjoint sessions. CRDT is a deliberately deferred v2 escape hatch — don't pay its complexity tax until real-world data proves the append-mostly assumption wrong.

## Undecided (don't silently resolve these)

These are open questions from the design — surface them rather than picking for the user:
- **`project_id` derivation** — manual vs. seeded from the git remote URL (the join key must be stable and machine-neutral).
- **Scope** — sync `~/.claude/projects/` only, or also opt-in repo-local `.mcc/` state (v1 rec: context dir only).

## Org context

This is a **methodify** org project. The repo carries PDT (Product Design Thinking) and MAMA (Multi-Agent) methodology tooling — `/pdt:*` and `/mama:*` skills, and a methodical-cc bus. The SessionStart hooks suggest `/pdt:init` or `/mama:arch-init`; use those for design/sprint workflow, but the design doc remains the source of truth. The project's own thesis ("the thinking is the artifact, context deserves first-class custody") is the same conviction the rest of the org's tooling rests on.
