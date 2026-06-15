"""The cluster tree: one project's canonical context, plus its roster.

In the branch-per-project model the cluster repo holds one orphan branch per
project, and a machine clones a single project's branch. So a working clone (or,
for a local no-git cluster, a plain directory) holds exactly ONE project's tree:

    <root>/
    ├── roster.json
    └── context/                 (canonical form of the context dir)
        └── <session>.jsonl

(The repo's `main` branch separately holds a README explaining the cluster; that
is managed by the git transport, not here.)
"""

from __future__ import annotations

import glob
import os

from .errors import ConvergenceError
from .roster import Roster

README_MAIN = """\
# Convergence context cluster

This repository is **managed by `convergence`** — do not hand-edit.

It stores Claude Code project *context* (session transcripts), one **project per
branch** (`project/<id>`), in machine-neutral *canonical form* with paths
replaced by sentinels. Each project branch holds a `roster.json` (how to expand
the sentinels per machine) and a `context/` directory. Clone a single project
with `convergence join … --remote <this repo>`; list projects with
`convergence projects --remote <this repo>`.

Keep this repository **private**: transcripts can contain secrets.
"""


class Cluster:
    """One project's tree, rooted at a working clone (git) or a local dir."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    @property
    def roster_path(self) -> str:
        return os.path.join(self.root, "roster.json")

    @property
    def context_dir(self) -> str:
        return os.path.join(self.root, "context")

    def ensure(self) -> None:
        os.makedirs(self.context_dir, exist_ok=True)

    def has_roster(self) -> bool:
        return os.path.exists(self.roster_path)

    # -- roster ----------------------------------------------------------- #
    def load_roster(self) -> Roster:
        return Roster.load(self.roster_path)

    def save_roster(self, roster: Roster) -> None:
        os.makedirs(self.root, exist_ok=True)
        roster.save(self.roster_path)

    # -- canonical context ------------------------------------------------ #
    def context_files(self) -> list[str]:
        """Relative paths of every canonical file under context/ (recursively),
        e.g. 'sess.jsonl' and 'memory/MEMORY.md'."""
        out = []
        for f in sorted(glob.glob(os.path.join(self.context_dir, "**", "*"), recursive=True)):
            if os.path.isfile(f):
                out.append(os.path.relpath(f, self.context_dir))
        return out

    def has_context(self, relpath: str) -> bool:
        return os.path.exists(os.path.join(self.context_dir, relpath))

    def read_context(self, relpath: str) -> str | None:
        path = os.path.join(self.context_dir, relpath)
        try:
            with open(path, encoding="utf-8") as fh:  # strict: never mangle on read
                return fh.read()
        except FileNotFoundError:
            return None
        except UnicodeDecodeError as e:
            raise ConvergenceError(
                f"cluster file {relpath} is not valid UTF-8 (at byte {e.start}); "
                f"refusing to localize it to avoid corrupting context.")

    def write_context(self, relpath: str, text: str) -> None:
        path = os.path.join(self.context_dir, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
