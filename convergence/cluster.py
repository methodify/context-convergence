"""The cluster: a directory holding canonical context for one or more projects.

Layout (design §3.4) — in Sprint 1 this is a plain local directory; Sprint 3
points it at a private git repo, but the on-disk shape is identical:

    <cluster>/
    ├── README.md                  (machine-managed; do not hand-edit)
    ├── .convergence/config.json   (cluster-level config + schema version)
    └── projects/<project_id>/
        ├── roster.json
        └── context/               (canonical form of the context dir)
            └── <session>.jsonl
"""

from __future__ import annotations

import glob
import json
import os

from .roster import Roster

SCHEMA_VERSION = 1

_README = """\
# Convergence context cluster

This directory is **managed by `convergence`** — do not hand-edit. It holds the
*canonical form* (machine-neutral, path-rewritten) of Claude Code project
context, plus a roster describing how to expand it per machine.

Keep this repository **private**: transcripts can contain secrets.
"""


class Cluster:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    # -- locations -------------------------------------------------------- #
    @property
    def config_path(self) -> str:
        return os.path.join(self.root, ".convergence", "config.json")

    def project_dir(self, project_id: str) -> str:
        return os.path.join(self.root, "projects", project_id)

    def roster_path(self, project_id: str) -> str:
        return os.path.join(self.project_dir(project_id), "roster.json")

    def context_dir(self, project_id: str) -> str:
        return os.path.join(self.project_dir(project_id), "context")

    # -- lifecycle -------------------------------------------------------- #
    def ensure(self) -> None:
        """Create the cluster scaffold if absent (idempotent)."""
        os.makedirs(os.path.join(self.root, ".convergence"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "projects"), exist_ok=True)
        if not os.path.exists(self.config_path):
            with open(self.config_path, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": SCHEMA_VERSION}, fh, indent=2)
                fh.write("\n")
        readme = os.path.join(self.root, "README.md")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as fh:
                fh.write(_README)

    def has_project(self, project_id: str) -> bool:
        return os.path.exists(self.roster_path(project_id))

    # -- roster ----------------------------------------------------------- #
    def load_roster(self, project_id: str) -> Roster:
        return Roster.load(self.roster_path(project_id))

    def save_roster(self, roster: Roster) -> None:
        os.makedirs(self.project_dir(roster.project_id), exist_ok=True)
        roster.save(self.roster_path(roster.project_id))

    # -- canonical context ------------------------------------------------ #
    def context_files(self, project_id: str) -> list[str]:
        return sorted(glob.glob(os.path.join(self.context_dir(project_id), "*.jsonl")))

    def write_context(self, project_id: str, filename: str, text: str) -> None:
        cdir = self.context_dir(project_id)
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, filename), "w", encoding="utf-8") as fh:
            fh.write(text)
