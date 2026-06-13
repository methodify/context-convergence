"""context-convergence — Sprint 0 spike.

Treats Claude Code project context (~/.claude/projects/<encoded>/) as a portable,
multi-machine asset by rewriting machine-local paths between a machine's *local
form* and a machine-neutral *canonical form*.

This package is the Sprint 0 spike: the canonicalize/localize core + doctor, no
sync machinery. Stdlib only. See docs/context-convergence-design.md.
"""

from .pathmap import (
    DEFAULT_SENTINEL,
    canonicalize_jsonl,
    canonicalize_value,
    encode_project_dir,
    infer_project_root,
    localize_jsonl,
    localize_value,
)
from .roster import Participant, Roster

__all__ = [
    "DEFAULT_SENTINEL",
    "Participant",
    "Roster",
    "canonicalize_jsonl",
    "canonicalize_value",
    "encode_project_dir",
    "infer_project_root",
    "localize_jsonl",
    "localize_value",
]
