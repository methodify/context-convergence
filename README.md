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

Your cluster is **one private git repo**. Each project is a branch
(`project/<id>`), and convergence manages a single-branch clone per project under
`~/.convergence/clones/` — so you only ever pass `--remote`.

```sh
# First machine — register this project's context in the cluster.
# The first --remote you use becomes this machine's default, so you name it once.
convergence init ~/src/proj --remote git@github.com:you/context-cluster.git

# More projects into the same cluster — no --remote needed.
convergence init ~/src/other-proj
convergence projects                               # list projects in the cluster

# Another machine — set the default once, then pull projects down.
convergence remote set git@github.com:you/context-cluster.git
convergence join ~/src/proj                        # localized to this machine's paths

# Day to day (these already remember the remote per project).
convergence sync                                   # pull then push (or via the hook below)
convergence status                                 # what's dirty / behind, plus the roster
```

`--remote` always overrides the default — reserve it for a second, separate
cluster. `convergence remote` shows the current default; `remote clear` unsets it.

Because each project is its own orphan branch, **joining one project never
downloads the others' history** — a machine fetches only what it asks for. Add a
hundred projects to one repo and a checkout is still just the one you want.

Prefer a local directory over git? Pass `--cluster ~/some/dir` instead of
`--remote` (one project per dir, no git).

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

One private git repo is your **cluster**; each project is an orphan branch
`project/<id>` (so projects are storage-isolated). A branch stores a **canonical
form** in which machine-specific anchors are replaced by sentinels, plus a
**roster** describing each machine's layout. Three rewrite tiers (a cluster-wide
policy fixed at `init`):

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
