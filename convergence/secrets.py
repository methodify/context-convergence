"""Secret hygiene scan (design §6.5, v1.5).

Transcripts can contain tokens, keys, and passwords — and the cluster repo,
though private, is still a durable copy. This is an *optional* scan (opt-in via
`scan`, or `push --scan-secrets`) that warns before context leaves the machine.
Curated, high-signal patterns only: the goal is to catch the obvious
catastrophes (a pasted private key, an AWS key, a provider token), not to be an
exhaustive DLP engine that drowns the user in false positives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# (name, compiled pattern). Ordered most-specific first.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("OpenAI / Anthropic key", re.compile(r"\b(?:sk|sk-ant)-[A-Za-z0-9_\-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("bearer token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9_\-\.=]{20,}")),
    ("assigned secret", re.compile(
        r"(?i)\b(api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*"
        r"['\"]?([A-Za-z0-9_\-]{16,})")),
]


@dataclass(frozen=True)
class Finding:
    kind: str
    redacted: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.redacted}"


def _redact(s: str) -> str:
    s = s.strip().strip("'\"")
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}…{s[-2:]} ({len(s)} chars)"


def scan_text(text: str) -> list[Finding]:
    """Return de-duplicated findings in a single string."""
    found: dict[tuple[str, str], Finding] = {}
    for kind, pat in _PATTERNS:
        for m in pat.finditer(text):
            raw = m.group(2) if m.lastindex and m.re.groups >= 2 else m.group(0)
            f = Finding(kind, _redact(raw))
            found[(kind, f.redacted)] = f
    return list(found.values())
