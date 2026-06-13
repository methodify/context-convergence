"""Per-machine, per-project local marker (design §3.5).

Ties a working project on this machine to its cluster identity, stored OUTSIDE
the project's own git repo (under `convergence_home`) so convergence never adds
noise to the user's actual project commits. Records enough to resolve push/pull
without re-deriving everything: which cluster, which project, this machine's
root + encoded dir, and the last-converged marker for dirty-checking.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import env


@dataclass
class LocalState:
    project_id: str
    machine_id: str
    cluster_root: str
    project_root: str
    encoded_dir: str
    last_converged: str | None = None
    last_converged_commit: str | None = None

    @staticmethod
    def path_for(project_id: str) -> str:
        return os.path.join(env.convergence_home(), "projects", f"{project_id}.json")

    def save(self) -> None:
        p = self.path_for(self.project_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(self.__dict__, fh, indent=2)
            fh.write("\n")

    @classmethod
    def load(cls, project_id: str) -> "LocalState | None":
        try:
            with open(cls.path_for(project_id), encoding="utf-8") as fh:
                return cls(**json.load(fh))
        except FileNotFoundError:
            return None
