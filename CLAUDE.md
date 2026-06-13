# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

**Sprint 0 is complete** (2026-06-13). The canonicalize/localize core + `doctor` exist in `convergence/`, with property tests in `tests/`, all stdlib Python. The idempotency invariant is proven against real transcripts (242 / 105,623 / 42,714 records across three corpora — all round-trip losslessly). Read `docs/sprint-0-findings.md` for what the dogfooding overturned; `docs/context-convergence-design.md` remains the product source of truth. **Next: Sprint 1** (single-machine roundtrip — `init`/`push`/`pull`, roster of one, local cluster dir).

**Language: Python** (Sprint 0, stdlib only). The *ship*-language is still open (Rust is a candidate); the spike code is not automatically the product. Keep the property tests as the portable spec.

### Commands

```sh
python3 -m unittest discover -s tests              # run the suite (no deps)
python3 -m unittest discover -s tests -v           # verbose
python3 -m convergence doctor <context_dir>        # scan + safety report (infers root)
python3 -m convergence canonicalize <ctx_dir> <out_dir> [--root R]
python3 -m convergence localize <in_dir> <ctx_dir> --root R   # R = target machine's root
```

Real context dirs live at `~/.claude/projects/<encoded-dir>/`. `doctor` on this project's own dir is the fastest smoke test.

## What this tool is

`convergence` is a CLI that treats Claude Code project context (the JSONL transcripts under `~/.claude/projects/<encoded-path>/`) as a portable, multi-machine asset. It syncs that context to a private git repo (a "context cluster") and **deep-rewrites machine-local absolute paths** on checkout so the context resolves correctly on whatever machine pulls it. The code already lives in the user's own git repo and is out of scope — convergence syncs *context only*.

## The load-bearing idea (do not lose this)

The whole product is a bet that **the path-surface inside transcripts is enumerable and rewriting is safely reversible.** Everything else is plumbing.

- **Canonical form** is what lives in the cluster repo: every occurrence of a participant's project root is replaced by a sentinel (`{{CC_PROJECT_ROOT}}`). The cluster never stores any one machine's local paths as authoritative — this is what keeps the repo diffable and lets git's own merge machinery work instead of every line colliding on differing absolute paths.
- **canonicalize** (local → canonical) and **localize** (canonical → local) are the two core transforms, parameterized by a machine's **roster** entry (`home`, `project_root`, `encoded_dir`, `os`).
- The correctness core is the invariant `canonicalize(localize(x)) == x` and vice versa, for any participant. This must have **property tests** — it is the spec.

### Hard rules for the rewriter

These were validated against real data in Sprint 0 — see `docs/sprint-0-findings.md` and the docstring of `convergence/pathmap.py`. Three corrections that a future instance must not undo:

- **Rewrite JSON-decoded string values, not raw file text.** In raw JSONL a path after a newline reads `...catalog\n/Users/...` — the `\n` escape makes the boundary indistinguishable from `/mnt/Users/.../catalog` (root as suffix of a longer path, must NOT rewrite). Parse each line, rewrite within each decoded string leaf, re-serialize. Compact dumps (`ensure_ascii=False, separators=(',',':')`) reproduce Claude Code's bytes exactly (verified 105,623/105,623), so diffs stay clean.
- **The encoded dir name is lossy — never decode it.** Encoding maps every non-alphanumeric char to `-`, so the root is unrecoverable from the dir name. Get the root from a `cwd` field or the roster. `infer_project_root` recovers it by *encoding* cwd ancestors and matching the dir name (also handles subdir cwds).
- **Boundary-anchored, never naked substring replace.** Rewrite the root only as a whole path prefix: not preceded by alphanumeric; not followed by a name char (`catalog-backup`/`catalog2`/`catalog_old` survive); a `.` is an extension only if followed by alphanumeric (`catalog.bak` survives) but a trailing `.` before whitespace/quote/end is punctuation and IS rewritten.

Other invariants:

- **v1 scope: project-root only.** Refs outside the project tree (home paths incl. `~/.claude/projects/<encoded>/…`, sibling roots) are **flagged, not rewritten** (doctor surfaces them). On machine B these stay machine-A-specific — a known v1 limitation (findings §"Known v1 limitations").
- **Literal-sentinel escaping is load-bearing here.** This repo's own context contains `{{CC_PROJECT_ROOT}}` verbatim. The `{{S(_LIT)*}}` escape family makes canonicalize/localize a true inverse pair; don't remove it or convergence can't sync its own context.
- **Fail loud, never guess.** Never ship a half-rewritten transcript. Context is irreplaceable; the prime directive is *never silently corrupt or lose context.*
- **Back up before localize.** Pull writes into `~/.claude/projects/` — back up the target (timestamped, kept N deep) before overwriting. *(Not yet built — Sprint 4.)*

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
- **Ship-language** — Sprint 0 spike is Python (decided); the language for the *shipped* tool (Python vs. Rust) is still open, decide after the canonicalizer's shape is known.
- **`project_id` derivation** — manual vs. seeded from the git remote URL (the join key must be stable and machine-neutral).
- **Home-dir refs outside project root** — flag-only (v1 rec) vs. rewrite.
- **Scope** — sync `~/.claude/projects/` only, or also opt-in repo-local `.mcc/` state (v1 rec: context dir only).

## Org context

This is a **methodify** org project. The repo carries PDT (Product Design Thinking) and MAMA (Multi-Agent) methodology tooling — `/pdt:*` and `/mama:*` skills, and a methodical-cc bus. The SessionStart hooks suggest `/pdt:init` or `/mama:arch-init`; use those for design/sprint workflow, but the design doc remains the source of truth. The project's own thesis ("the thinking is the artifact, context deserves first-class custody") is the same conviction the rest of the org's tooling rests on.
