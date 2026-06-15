"""Roster: the per-project metadata that drives path rewriting (design §3.3).

The roster is the source of truth for *how to expand the canonical sentinel back
to a real path on each machine*. It lives in the cluster at
`projects/<project_id>/roster.json`. Each participant is one machine; joining a
project means appending a participant entry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .pathmap import (
    DEFAULT_SENTINEL,
    build_mappings,
    canonicalize_jsonl,
    encode_project_dir,
    localize_jsonl,
    native_sep,
)


@dataclass
class Participant:
    """One machine's entry — both its path identity and its sync bookkeeping.

    The path identity is `home` + `project_root`; `encoded_dir` is derived. From
    these (plus the cluster's rewrite-home policy) it builds the ordered mapping
    set that canonicalize/localize use."""

    machine_id: str
    os: str
    home: str
    project_root: str
    last_converged: str | None = None
    last_converged_commit: str | None = None

    @property
    def encoded_dir(self) -> str:
        """The `~/.claude/projects/<name>` dir name for this machine's root.
        Computed (encoding is one-way) — never decoded. See pathmap §1."""
        return encode_project_dir(self.project_root)

    @property
    def native_sep(self) -> str:
        """This machine's path separator (`\\` on Windows, else `/`), used to
        normalize path tails to/from the canonical `/`."""
        return native_sep(self.os)

    def mappings(self, rewrite_home: bool = True):
        return build_mappings(self.home, self.project_root, self.encoded_dir, rewrite_home)

    def canonicalize(self, text: str, rewrite_home: bool = True) -> tuple[str, int]:
        return canonicalize_jsonl(text, self.mappings(rewrite_home), self.native_sep)

    def localize(self, text: str, rewrite_home: bool = True) -> tuple[str, int]:
        return localize_jsonl(text, self.mappings(rewrite_home), self.native_sep)

    def to_dict(self) -> dict:
        return {
            "machine_id": self.machine_id,
            "os": self.os,
            "home": self.home,
            "project_root": self.project_root,
            "encoded_dir": self.encoded_dir,  # derived, stored for human legibility
            "last_converged": self.last_converged,
            "last_converged_commit": self.last_converged_commit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Participant":
        return cls(
            machine_id=d["machine_id"],
            os=d["os"],
            home=d["home"],
            project_root=d["project_root"],
            last_converged=d.get("last_converged"),
            last_converged_commit=d.get("last_converged_commit"),
        )


@dataclass
class Roster:
    project_id: str
    canonical_sentinel: str = DEFAULT_SENTINEL  # informational: the project-root sentinel
    rewrite_home: bool = True  # cluster-wide policy: also rewrite the home prefix
    participants: list[Participant] = field(default_factory=list)

    def get(self, machine_id: str) -> Participant | None:
        return next((p for p in self.participants if p.machine_id == machine_id), None)

    def upsert(self, participant: Participant) -> None:
        """Add the participant, or replace the existing entry with the same
        machine_id (re-init / repeated join on the same machine)."""
        for i, p in enumerate(self.participants):
            if p.machine_id == participant.machine_id:
                self.participants[i] = participant
                return
        self.participants.append(participant)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "canonical_sentinel": self.canonical_sentinel,
            "rewrite_home": self.rewrite_home,
            "participants": [p.to_dict() for p in self.participants],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Roster":
        return cls(
            project_id=d["project_id"],
            canonical_sentinel=d.get("canonical_sentinel", DEFAULT_SENTINEL),
            rewrite_home=d.get("rewrite_home", True),
            participants=[Participant.from_dict(p) for p in d.get("participants", [])],
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")

    @classmethod
    def load(cls, path: str) -> "Roster":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))
