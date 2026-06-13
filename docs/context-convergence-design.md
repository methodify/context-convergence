# Context Convergence — Product Design

**Status:** Initial design
**Date:** 2026-06-13
**Authors:** Bryon (user) + Claude (design partner)
**Org:** methodify
**Working name:** `convergence` (CLI), `context-convergence` (repo)

A tool that treats Claude Code project context — the conversation history, ideas, alignment, and decisions that accumulate in `~/.claude/projects/` — as a first-class, portable, multi-machine asset. It syncs that context to a private git repo and **rewrites machine-local paths** on checkout so the context "just works" regardless of where a given machine keeps its source tree.

---

## 1. Context & Motivation

### The problem

A Claude Code project accumulates two distinct bodies of value:

1. **Code and content** — lives in git/GitHub, already portable, already versioned, already solved.
2. **Context** — the session transcripts, todos, and accumulated working memory under `~/.claude/projects/<encoded-path>/`. This is where the *thinking* lives: the why behind the code, the rejected alternatives, the alignment built up over weeks. It is, as a rule, **trapped on a single machine.**

The asymmetry is the whole problem. You can `git clone` your project onto a new machine in seconds and have all the code — but none of the context that produced it. The MAM/MAMA/PDT methodology is *built* on the premise that context is the real product; context-convergence extends that premise across machines.

### The concrete scenario

I work on a project in my desktop's WSL2. The project lives at, say, `/home/bryon/src/submatrix`. Claude Code stores its context under `~/.claude/projects/-home-bryon-src-submatrix/`. The encoded directory name **is the absolute path** with separators flattened — it's machine-specific by construction.

Now I want to work on the same project from my MacBook for a few weeks. There, my home is `/Users/bryon`, and the project lives at `/Users/bryon/src/submatrix`. Even though my *convention* is identical (`~/src/{project}`), the absolute path differs, so:

- The encoded project directory name is different (`-Users-bryon-src-submatrix`).
- **Every absolute path embedded inside the context content is wrong** — transcripts reference `/home/bryon/src/submatrix/...`, tool calls cite WSL paths, file references point at a directory layout that doesn't exist on the Mac.

Copying the context naively gets you a transcript full of dead paths. The magic — and the reason this is a real tool and not a `cp` — is **deep path rewriting** so the context is correct for the local machine.

### What we want

- Sync the per-project context for **one scoped project** up to a private GitHub repo (the user's personal "context cluster").
- Carry **metadata** describing the path structure on each participating machine (a roster).
- On a second machine, check the context out into the correct local `~/.claude/projects/<encoded-local-path>/`, and **deep-rewrite embedded paths** so all references resolve locally.
- Make the roundtrip feel seamless — sync in the background or "when you're done" — so context follows you between machines the way code already does.

### What we're explicitly NOT building (v1)

- **Not a replacement for git on the code.** The code stays in its own repo. Convergence syncs *context only*. The two are orthogonal and intentionally decoupled.
- **Not a real-time collaborative editor.** This is sync-and-checkout, not live multiplayer on a shared session. Last-writer-wins with conflict surfacing, not OT/CRDT merge (see §7 for why, and the door we leave open).
- **Not a hosted service.** No server, no auth surface beyond the user's existing GitHub credentials. The transport is a private git repo the user already controls.
- **Not a multi-user sharing tool.** v1 is one person, many machines. Team-shared context is a future question, not a v1 goal.
- **Not format-aware beyond what it must be.** Convergence understands the *structure* of `~/.claude/projects/` and how to rewrite paths inside it. It does not try to understand or summarize the semantic content of transcripts.

---

## 2. Mental model & vocabulary

| Term | Meaning |
|------|---------|
| **Context cluster** | The private GitHub repo that holds synced context for one or more projects. One per user (or per security boundary). |
| **Project context** | The `~/.claude/projects/<encoded-path>/` directory for a single project on the local machine. |
| **Participant** | A machine that has joined a project's sync (has an entry in the roster). |
| **Roster** | The metadata list of participants and their local path structures for a given project. Source of truth for path rewriting. |
| **Canonical form** | A machine-neutral representation of the context in which all participant paths are replaced by a portable placeholder. What actually lives in the cluster repo. |
| **Local form** | The materialized, path-rewritten context on a specific machine, living in `~/.claude/projects/`. |
| **Converge** | The verb. To reconcile local form ⇄ canonical form (push, pull, or both). |

The key conceptual move: **the cluster repo never stores any one machine's local paths as authoritative.** It stores a *canonical form* with placeholders, plus a roster that says how to expand the placeholder per machine. Push = localize→canonicalize. Pull = canonicalize→localize.

---

## 3. Architecture

### 3.1 The canonical form (the crux)

This is the load-bearing idea. Everything else is plumbing.

`~/.claude/projects/<encoded-path>/` contains, principally, JSONL session transcripts plus a few sidecar files. Inside those transcripts, the project's absolute path appears in many forms:

- The **encoded directory name** itself (`-home-bryon-src-submatrix`).
- **Absolute paths** in tool calls, tool results, file reads, cwd fields, todo file references.
- Possibly **home-relative** and **other-anchored** paths depending on how a session was driven.

The canonical form replaces every occurrence of a participant's project root with a sentinel — e.g. `{{CC_PROJECT_ROOT}}` — and stores enough roster metadata to expand it back. So:

```
/home/bryon/src/submatrix/scan/mesh.py      (desktop local form)
        ↓ canonicalize
{{CC_PROJECT_ROOT}}/scan/mesh.py             (canonical form in cluster)
        ↓ localize (on Mac)
/Users/bryon/src/submatrix/scan/mesh.py      (mac local form)
```

**Why a sentinel and not "just rewrite on checkout from the roster":** storing canonical form keeps the cluster repo diffable and machine-neutral. Two machines pushing produce diffs against the *same* canonical base, so git's own merge machinery does useful work instead of every line colliding on differing absolute paths. (This is the difference between a sync tool that fights git and one that rides it.)

### 3.2 What needs rewriting — the path-surface inventory

The hard engineering is enumerating *every* place a path hides. A non-exhaustive starter inventory the implementor must validate against real transcripts:

- The encoded **directory name** under `~/.claude/projects/`.
- `cwd` fields in transcript records.
- Tool-use inputs: `file_path`, `path`, `notebook_path`, `command` strings containing the root (bash calls), glob/grep patterns.
- Tool results: file contents echoing their own path, directory listings, error messages with paths.
- Todo/plan sidecar files that reference absolute paths.
- The home directory portion *independent* of the project root (some references may be `~/.claude/...` or `/home/bryon/...` outside the project tree — decide policy: rewrite project-root only, or also home? **v1 recommendation: project-root only**, and flag stray home refs rather than rewriting them, to avoid corrupting references to things that genuinely differ per machine).

**Design rule:** rewriting is **anchored and bounded**, never a blind string replace. Replace `<participant project root>` as a path-boundary-aware token (must be followed by `/`, `"`, whitespace, or end), never as a naked substring. A blind `s/home\/bryon/Users\/bryon/g` is exactly the footgun this tool exists to replace; the canonicalizer must be path-aware, not text-aware.

### 3.3 Roster & metadata

Each project tracked in the cluster has a manifest, e.g. `projects/<project-id>/roster.json`:

```json
{
  "project_id": "submatrix",
  "canonical_sentinel": "{{CC_PROJECT_ROOT}}",
  "participants": [
    {
      "machine_id": "desktop-wsl",
      "os": "linux",
      "home": "/home/bryon",
      "project_root": "/home/bryon/src/submatrix",
      "encoded_dir": "-home-bryon-src-submatrix",
      "last_converged": "2026-06-13T19:04:00Z",
      "last_converged_commit": "a1b2c3d"
    },
    {
      "machine_id": "macbook",
      "os": "darwin",
      "home": "/Users/bryon",
      "project_root": "/Users/bryon/src/submatrix",
      "encoded_dir": "-Users-bryon-src-submatrix",
      "last_converged": null,
      "last_converged_commit": null
    }
  ]
}
```

- `project_id` is **stable and machine-neutral** (chosen at first init, not derived from a path) — this is the join key across machines.
- `machine_id` identifies a participant; generated on first join, stored locally so the same machine is recognized on repeat runs.
- The roster is what makes "machine 2 has joined the party" concrete: joining = appending a participant entry + materializing local form.

### 3.4 Repo layout (cluster)

```
context-cluster/                 (private GitHub repo)
├── README.md                    (explains this is machine-managed; do not hand-edit)
├── .convergence/
│   └── config.json              (cluster-level config, schema version)
└── projects/
    └── submatrix/
        ├── roster.json
        └── context/             (canonical form of ~/.claude/projects/<dir>/)
            ├── <session>.jsonl   (canonicalized)
            └── ...
```

One repo, many projects. Project-level isolation in subtrees keeps a single private repo serving the whole cluster while allowing per-project rosters.

### 3.5 Local state

On each machine, a small local marker ties a working project to its cluster identity without polluting the project repo:

- Stored **outside** the project's own git repo (e.g. `~/.convergence/<project-id>.json` or under the cluster checkout), so convergence never adds noise to the user's actual project commits.
- Records `machine_id`, `project_id`, mapping to the local encoded dir, and last-converged commit for fast dirty-checking.

---

## 4. Core operations (CLI surface)

Design the CLI to be boring and predictable — it manages something precious, so it should be legible and dry-run-friendly.

```
convergence init        # first machine: create/register a project in the cluster
convergence join        # subsequent machine: pull a project's context here, localized
convergence push        # localize → canonicalize → commit/push to cluster
convergence pull         # fetch cluster → canonicalize-merge → localize to ~/.claude
convergence sync        # pull then push (the everyday verb)
convergence status      # what's dirty, what's behind, roster summary
convergence roster      # show/inspect participants for current project
convergence doctor      # validate path-surface assumptions against actual transcripts
```

### 4.1 `init`
- Run from inside a project working dir (or with `--project-root`).
- Detect the local `~/.claude/projects/<encoded-dir>/`.
- Prompt for / derive a stable `project_id`.
- Create `projects/<project-id>/` in the cluster, write roster with this machine as first participant, canonicalize current context, commit, push.

### 4.2 `join`
- Run on a new machine from the desired project root.
- Resolve cluster + `project_id`.
- Append this machine to the roster (capturing home, project_root, os, encoded_dir).
- Pull canonical context, **localize** into the correct local `~/.claude/projects/<encoded-dir>/`.
- This is the "machine 2 joins the party" moment.

### 4.3 `push` / `pull` / `sync`
- `push`: read local form → canonicalize (using *this* machine's roster entry) → stage in cluster checkout → commit → push. Refuse if local path-surface inventory finds an un-canonicalizable path (fail loud, per house style).
- `pull`: `git pull` cluster → for each changed session, canonical-merge → localize to local form. Surface conflicts rather than silently picking a side.
- `sync`: pull then push. The verb users alias to a hotkey or hook.

### 4.4 `doctor`
The honesty command. Scans local transcripts and reports:
- Path forms found vs. path forms the canonicalizer knows how to handle.
- Stray home/absolute references outside the project root (flagged, not rewritten).
- Roster drift (a machine whose recorded `project_root` no longer matches reality — e.g. user moved the project).
This is how the user trusts the tool with irreplaceable context: it tells them what it can and cannot safely round-trip *before* it touches anything.

---

## 5. The seamless experience layer

The CLI is the substrate. On top of it, two opt-in modes for "just works":

### 5.1 "When you're done" (recommended default)
A Claude Code **Stop hook** (or SessionEnd) runs `convergence sync` when a session ends. Context converges at natural boundaries — you finish working on the desktop, it pushes; you start on the Mac, `join`/`pull` already ran or runs on `SessionStart`. Deterministic, low-surprise, rides the methodology's existing session rhythm. Fits the methodical-cc grain: convergence happens at the same beats as sprint/decision artifacts.

### 5.2 "Background" (power mode)
A lightweight watcher (debounced filesystem watch on the project's context dir, or an interval timer) pushes periodically. More magical, more failure modes (mid-session pushes capturing half-written transcripts, more frequent conflicts). Gate behind explicit opt-in; do not ship as default.

**Recommendation:** ship 5.1 as the blessed path; offer 5.2 as documented-but-advanced. The Stop-hook model is the one that matches how the rest of the org's tooling behaves.

---

## 6. Safety, trust & failure modes

Context is irreplaceable and un-regenerable. The tool's prime directive is **never silently corrupt or lose context.**

- **Read-bias on the local side.** Pull writes to `~/.claude/projects/` — back up the target dir before overwriting (timestamped, kept N deep), so a bad localize is recoverable.
- **Fail loud, never guess.** If the path-surface inventory finds something the canonicalizer can't anchor confidently, stop and report — don't ship a half-rewritten transcript. (Mirrors the docs plugin's "non-md matches fail loudly.")
- **Idempotency.** Canonicalize(localize(x)) == x and vice versa, for any participant. This is a testable invariant and should have property tests — it's the correctness core.
- **Boundary-anchored rewriting only.** Per §3.2 — never naked substring replace.
- **Private repo, user's own creds.** No new auth surface. Strongly recommend (and `doctor`-warn if absent) that the cluster repo is **private** — transcripts can contain secrets, tokens, internal detail.
- **Secret hygiene (flag, v1.5):** an optional scan that warns when context about to be pushed contains apparent secrets, reusing the spirit of methodical-cc's privacy-scan prompt.
- **Conflict surfacing.** When two machines diverge on the same session, present the conflict with machine + timestamp context; never auto-resolve destructively. Default resolution policy: newer `last_converged` wins *at file granularity* with the loser preserved in a backup, not silently dropped.

---

## 7. The CRDT question (deliberately deferred)

Naive sync is last-writer-wins. JSONL transcripts are **append-mostly**, which is the friendly case — divergence is usually "machine A added sessions X,Y; machine B added session Z," a union, not a true conflict. v1 should exploit append-mostly structure: merge by session-record union where possible, fall back to LWW-with-backup only on genuine same-record divergence.

True concurrent editing of the *same* session from two machines is the hard case CRDTs would solve. It's also rare in the one-user-many-machines model (you're not usually live in the same session on two machines at once). So: **v1 leans on git + append-mostly union + LWW-with-backup. CRDT is a v2 escape hatch if real-world divergence proves messier than the append-mostly assumption predicts.** Don't pay the CRDT complexity tax until the data says you must. (Consistent with your earlier CRDT exploration for the mineral app — same instinct, applied with restraint.)

---

## 8. Build order (suggested sprints)

1. **Sprint 0 — Spike the canonicalizer.** Take a real `~/.claude/projects/<dir>/` from desktop, write canonicalize + localize, prove the idempotency invariant on actual transcripts. This de-risks everything. If path-surface is messier than §3.2 assumes, you want to know on day one. **Output:** `doctor` + canonicalize/localize as a library, no sync yet.
2. **Sprint 1 — Single-machine roundtrip.** `init` + `push` + `pull` against a local "cluster" dir (no GitHub yet). Roster of one. Prove repo layout + canonical form survive a roundtrip.
3. **Sprint 2 — Second machine.** `join` + multi-participant roster + localize-on-checkout. This is the headline feature working end to end. Use two real machines (your WSL + Mac).
4. **Sprint 3 — Git transport.** Point the cluster at a private GitHub repo; pull/push/sync over real git; append-mostly union merge; conflict surfacing.
5. **Sprint 4 — Seamless layer.** Stop-hook integration (`5.1`), `status` polish, backups, secret-scan flag.
6. **Sprint 5 (optional) — Background mode + hardening.**

Sprint 0 is non-negotiable and gates the rest — the whole product is a bet that path-surface is enumerable and rewriting is safely reversible. Prove that first.

---

## 9. Open questions for the design session

1. **Language.** Python (fast to spike, matches `mcc` tooling) vs. Rust (deterministic, single-binary distribution, matches your stated leanings). Recommendation: **spike Sprint 0 in Python** to learn the path-surface fast, then decide on Rust for the shipped tool once the canonicalizer's shape is known. Don't let the rewrite-target question block the spike.
2. **Home-dir references outside the project root** — flag-only (v1 recommendation) or rewrite? Decide explicitly; it changes the inventory scope.
3. **`project_id` derivation** — fully manual, or seeded from the project's git remote URL (stable, machine-neutral, already unique)? The git-remote seed is attractive: it ties context identity to code identity without coupling the two repos.
4. **One cluster repo vs. per-project repos.** Design assumes one cluster, many project subtrees. Confirm that matches how you'd manage privacy/sharing boundaries.
5. **Relationship to methodical-cc artifacts.** Does convergence sync *only* `~/.claude/projects/`, or also opt-in repo-local `.mcc/` state? v1 recommendation: context dir only; methodical-cc artifacts already live in the project's own git. Confirm.
6. **Naming.** `context-convergence` (repo) / `convergence` (CLI) — or shorter? `cvg`? `converge`? Worth a beat given how the org-naming exercise went.

---

## 10. Spirit check (why this belongs in methodify)

Every methodify tool rests on one conviction: **the thinking is the artifact, and it deserves first-class custody.** methodical-cc crystallizes design conversation into deltas and decisions. dynamics-tools grounds the model in a real domain. mdocs makes scattered thinking shareable. Context-convergence is that conviction stated most directly: the context *is* the gold mine, and a gold mine you can only reach from one machine is half-buried. This tool digs it out and makes it portable — without ever pretending it's safe to do that with a naive copy.
