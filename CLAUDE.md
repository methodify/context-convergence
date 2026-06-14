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

### Transport model (Sprint 3)

git is **pure transport + history**; all merging happens in canonical space via `transport.union_jsonl`, so the local clone is a cache of the remote and git-level conflicts never arise. Each publishing op (`init`/`join`/`push`) runs `sync_down` (fetch + `reset --hard origin/main`) → re-derives canonical state from local truth, unioned with the remote's latest → commit → push, retrying the whole thing if the push is non-fast-forward. The everyday merge is append-mostly union: disjoint sessions union; a session extended on two machines keeps both sides' records rather than dropping either. Without `--remote`, `open_transport` returns a no-op `LocalTransport` and the cluster dir is the cluster (Sprint 1/2).

**Language: Python, stdlib-only — locked in** (2026-06-13). The ship-language question is closed; Rust was ruled out. Don't add third-party deps (tests use `unittest`). Keep the property tests as the correctness spec.

### Commands

```sh
python3 -m unittest discover -s tests              # run the suite (no deps)
python3 -m unittest discover -s tests -v           # verbose
python3 -m unittest tests.test_engine              # one module

# sync verbs — omit --remote for a local cluster dir (Sprint 1/2);
# pass --remote <git-url> for a private git cluster (Sprint 3)
python3 -m convergence init <project_root> --cluster <clone_dir> [--remote URL] [--project-id ID]
python3 -m convergence join <project_root> --cluster <clone_dir> [--remote URL] [--project-id ID]
python3 -m convergence push|pull|sync|status [project_root] [--project-id ID]
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

`pathmap.py` (path-mapping core, no I/O) · `roster.py` (`Participant`/`Roster` + persistence) · `cluster.py` (`Cluster` filesystem layout, §3.4) · `localstate.py` (per-machine project marker, §3.5) · `env.py` (location/clock/machine-id/os/settings resolution, all env-overridable) · `gitutil.py` (thin checked git wrappers) · `transport.py` (`LocalTransport`/`GitTransport` + `union_jsonl`) · `secrets.py` (curated secret patterns) · `hooks.py` (Stop-hook install + soft-failing `hook_sync`) · `engine.py` (the verbs; fail-loud round-trip guard on push, backup-before-overwrite on pull, publish-retry, opt-in secret scan) · `doctor.py` (honesty scan) · `__main__.py` (CLI).

The push guard compares against `normalize_jsonl(text)` (compact re-serialization), not raw bytes — it verifies *data* reversibility, not incidental whitespace. Tests are sandboxed via the `env.py` overrides; `CLAUDE_SETTINGS_PATH` redirects hook install away from the real settings.json.

## What this tool is

`convergence` is a CLI that treats Claude Code project context (the JSONL transcripts under `~/.claude/projects/<encoded-path>/`) as a portable, multi-machine asset. It syncs that context to a private git repo (a "context cluster") and **deep-rewrites machine-local absolute paths** on checkout so the context resolves correctly on whatever machine pulls it. The code already lives in the user's own git repo and is out of scope — convergence syncs *context only*.

## The load-bearing idea (do not lose this)

The whole product is a bet that **the path-surface inside transcripts is enumerable and rewriting is safely reversible.** Everything else is plumbing.

- **Canonical form** is what lives in the cluster repo: machine-specific anchors are replaced by sentinels. The cluster never stores any one machine's local paths as authoritative — this is what keeps the repo diffable and lets git's own merge machinery work instead of every line colliding on differing absolute paths.
- **Three rewrite tiers** (`build_mappings`, applied longest-anchor-first so specific beats general), a cluster-wide policy fixed at init (`roster.rewrite_home`):
  1. project root → `{{CC_PROJECT_ROOT}}`
  2. `<home>/.claude/projects/<encoded>` → `{{CC_PROJECT_CONTEXT_DIR}}` (own context dir; both home AND the lossy encoded segment change per machine, so it gets its own exact sentinel)
  3. `<home>` → `{{CC_HOME}}` (covers `~/.claude/*`, dotfiles, and sibling projects by the `~/src/{project}` convention; opt out with `init --no-rewrite-home`)
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
- **Back up before localize.** Pull writes into `~/.claude/projects/` — back up the target (timestamped) before overwriting (`engine._backup_local_context`).

## Working with real context data

The tool operates on `~/.claude/projects/<encoded-dir>/`. The encoded dir is the absolute project path with **every non-alphanumeric char** mapped to `-` (e.g. this project: `-Users-bryonwilliams-src-context-convergence`). Each dir holds `<session-uuid>.jsonl` transcripts plus sidecars (e.g. a `memory/` dir). Always test the canonicalizer against **real** transcript dirs, not synthetic ones — that is what surfaced every Sprint 0 correction. Calibration corpora for pressure-testing: `~/.claude/projects/-Users-bryonwilliams-src-catalog` and `-Users-bryonwilliams-src-deepdrift-zero` (both large; the latter exercises the lossy dot-root and sibling-root cases).

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
