# Sprint 0 — Canonicalizer Spike: Findings

**Date:** 2026-06-13
**Status:** Complete. The load-bearing bet holds — path-surface is enumerable and rewriting is safely reversible.
**Deliverable:** `convergence/` (canonicalize/localize library + `doctor`), `tests/` (idempotency property tests), stdlib-only Python.

Sprint 0's job (per the design, §8) was to de-risk everything by proving the
idempotency invariant against *real* transcripts before building any sync. It
did — and it overturned three assumptions in the original design that would
have caused silent corruption if built on naively.

## Corpora used

| Corpus | Records | Why |
|---|---|---|
| `-Users-bryonwilliams-src-context-convergence` | 242 | Day-one corpus; **self-referential** (contains the sentinel string) |
| `-Users-bryonwilliams-src-catalog` | 105,623 | Scale; deep subdir cwds; widest path-surface |
| `-Users-bryonwilliams-src-deepdrift-zero` | 42,714 | **Lossy** dot-in-name root; sibling roots in one dir |

All three round-trip losslessly (`localize(canonicalize(x)) == x`) with zero
real-root residue. `doctor` reports `=> OK` on each.

## Finding 1 — The encoded dir name is lossy and irreversible

Claude Code encodes `~/.claude/projects/<name>` by replacing **every
non-alphanumeric character** (`/`, `.`, `_`, `~`, space) with `-`. Verified
across all local projects.

- The real path of `-Users-bryonwilliams-src-deepdrift-zero` is
  `/Users/bryonwilliams/src/deepdrift**.**zero` — a *dot*, not a hyphen.
  `deepdrift.zero`, `deepdrift-zero`, `deepdrift_zero` all encode identically.
- **Consequence:** you cannot decode the project root from the dir name. It must
  come from the transcript `cwd` field (ground truth) or the roster.
- **Design impact:** §3.2 lists "the encoded directory name itself" as something
  to rewrite. You can compute a *target* machine's dir name by **encoding** its
  known root, but never recover a root by **decoding**. `infer_project_root`
  does the former: it tests each `cwd` and its ancestors and returns the one
  whose encoding matches the dir name. This also transparently handles…

## Finding 2 — cwd is often a *subdir*, and sibling roots coexist

- catalog's most common `cwd` is `~/src/catalog/**app**`, not the root. Root
  inference must walk ancestors (it does).
- deepdrift's context dir contains `cwd` values for a **sibling** project,
  `~/src/dd-crossover`, worked on in the same sessions. These are outside the
  project root and, per the v1 project-root-only policy, are **flagged not
  rewritten** (doctor surfaces them). They remain machine-specific in canonical
  form — a known, deliberate v1 limitation.

## Finding 3 — Rewriting must operate on JSON-decoded string values

This was the subtle one. At the raw-text level a path after a newline reads
`...catalog\n/Users/...`, so the next path's `/Users` is preceded by the `n` of
the `\n` escape — **indistinguishable from `/mnt/Users/.../catalog`** (root as
the suffix of a longer, unrelated path, which must NOT be rewritten).

- Fix: parse each JSONL line, rewrite within each **decoded** string value
  (where `\n` is a real newline → clean boundary), re-serialize.
- Cost: none. Compact re-serialization
  (`json.dumps(obj, ensure_ascii=False, separators=(',',':'))`) reproduces
  Claude Code's lines **byte-for-byte** — verified on 105,623/105,623 catalog
  records. So git diffs stay clean.

## Mechanisms the dogfooding forced into existence

- **Boundary-anchored matching.** Rewrite the root only as a whole path prefix:
  not preceded by an alphanumeric; not followed by a name char (`catalog-backup`,
  `catalog2`, `catalog_old` survive); a following `.` is an extension *only* if
  followed by alphanumeric (`catalog.bak` survives) — a trailing `.` before
  whitespace/quote/end is sentence punctuation and **is** rewritten ("git init
  in `<root>`. Single monorepo…"). That last rule cleared the only residue real
  data produced.
- **Literal-sentinel escaping.** This repo's own context contains
  `{{CC_PROJECT_ROOT}}` verbatim (the design discusses it). Naive localize would
  expand those. The `{{S(_LIT)*}}` family is lifted one level on canonicalize
  and lowered on localize — regress-free, so a literal sentinel survives the
  round-trip exactly. Without this, convergence could not sync its own context.

## The path-surface inventory (measured, not assumed)

Fields where the project root actually appears, by frequency (union across
corpora): `.cwd` (dominant), `.message.content[].input.{file_path,command,path,
output_path,app_path,old_string,new_string,content,prompt}`,
`.toolUseResult.{filePath,file.filePath,stdout,stderr,content,originalFile,
newString,oldString}`, `.toolUseResult.structuredPatch[].lines[]`,
`.message.content[].content[].text`, `.attachment.{filename,content[].description,
content.file.filePath}`. The JSON-walking transform covers all of these by
construction (it rewrites *every* string leaf), so the inventory is for `doctor`
reporting and human trust, not for targeting.

## Known v1 limitations (carry forward)

1. **Home-path references are flagged, not rewritten** — including
   `~/.claude/projects/<encoded-dir>/…` self-references (2,258 in deepdrift).
   On machine B these point at machine A's encoded dir and are stale. Rewriting
   the home/`.claude` surface is a deliberate future scope decision (design open
   question #2).
2. **Sibling roots** (Finding 2) remain machine-specific in canonical form.
3. **Unparseable JSONL lines** pass through untouched (0 seen in practice;
   `doctor` would surface them).

## Verdict

The bet behind the product holds. Proceed to Sprint 1 (single-machine roundtrip:
`init`/`push`/`pull`, roster of one, local cluster dir) on this canonicalizer.
