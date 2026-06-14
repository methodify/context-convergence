# context-convergence

Sync Claude Code project **context** — the session transcripts under
`~/.claude/projects/<encoded-path>/` — across your machines. Your code already
travels via git; your *context* (the thinking, the why, the alignment built up
over weeks) is trapped on one machine. Convergence syncs it to a private git
repo and **deep-rewrites machine-local paths** on checkout, so a transcript made
on `/home/you/src/proj` resolves correctly when you pull it onto
`/Users/you/src/proj`.

Stdlib-only Python, no runtime dependencies.

## Install

```sh
pip install -e .
```

This puts a `convergence` command on your PATH.

## Quickstart

```sh
# First machine — register this project's context in a cluster (a git repo).
convergence init ~/src/proj --cluster ~/clusters/mine --remote git@github.com:you/context-cluster.git

# Another machine — pull it down, localized to this machine's paths.
convergence join ~/src/proj --cluster ~/clusters/mine --remote git@github.com:you/context-cluster.git

# Day to day.
convergence sync     # pull then push — run it whenever, or via the hook below
convergence status   # what's dirty / behind, plus the roster
```

Omit `--remote` to use a plain local directory as the cluster (no git).

## Make it seamless

Install a Claude Code **Stop hook** that runs `convergence sync` at the end of
every session (it resolves the project from the session's cwd and fails soft, so
it can never break a session):

```sh
convergence hook install      # adds a global Stop hook to ~/.claude/settings.json
convergence hook status
convergence hook uninstall
```

## Safety

Context is irreplaceable, so the tool is conservative:

- **doctor** — `convergence doctor ~/.claude/projects/<dir>` reports exactly what
  it can and cannot round-trip *before* anything is written.
- **Round-trip guard** — push refuses to ship any transcript it cannot reverse
  losslessly.
- **Backups** — pull/join back up the local context dir (timestamped) before
  overwriting, under `~/.convergence/backups/`.
- **Secret scan (opt-in)** — `convergence scan`, or `push --scan-secrets
  [--strict]`, warns (or refuses) when context about to be pushed contains
  apparent secrets. **Keep the cluster repo private** — transcripts can contain
  tokens and internal detail.

## How it works

The cluster stores a **canonical form** in which machine-specific anchors are
replaced by sentinels, plus a **roster** describing each machine's layout. Three
rewrite tiers (a cluster-wide policy fixed at `init`):

1. project root → `{{CC_PROJECT_ROOT}}`
2. `~/.claude/projects/<encoded>` → `{{CC_PROJECT_CONTEXT_DIR}}`
3. `~` (home) → `{{CC_HOME}}`  (opt out with `init --no-rewrite-home`)

Push = localize→canonicalize→commit→push; pull = fetch→localize. git is pure
transport and history; merging is append-mostly **union** done in canonical
space, so disjoint sessions combine cleanly and concurrent machines converge
without git-level conflicts.

## Develop

```sh
python3 -m unittest discover -s tests
```

See `CLAUDE.md` for architecture and `docs/` for the design and findings.
