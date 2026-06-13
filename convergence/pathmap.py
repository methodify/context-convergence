"""The load-bearing core: encoding + boundary-anchored canonicalize/localize.

Sprint 0 findings that shape this module (validated against real transcripts on
2026-06-13):

1. Claude Code's encoded directory name maps EVERY non-alphanumeric character
   (`/`, `.`, `_`, `~`, space, ...) to `-`. So `deepdrift.zero`, `deepdrift-zero`
   and `deepdrift_zero` all encode identically. The encoding is therefore LOSSY
   and IRREVERSIBLE — the real project root can never be decoded from the dir
   name. It must come from a transcript `cwd` field (or be supplied / from the
   roster). See `infer_project_root`.

2. A session's `cwd` may be a *subdirectory* of the project root (e.g. project
   `~/src/catalog`, cwd `~/src/catalog/app`), and a single context dir can
   contain references to *sibling* roots worked on in the same sessions. v1
   policy: rewrite the project root only; flag everything else (see doctor).

3. Rewriting must run on JSON-DECODED string values, not raw file text. In raw
   JSONL a path after a newline reads `...catalog\n/Users/...`, so the next
   path's `/Users` is preceded by the `n` of the `\n` escape — indistinguishable
   at the text level from `/mnt/Users/.../catalog` (root as the suffix of a
   longer path, which must NOT be rewritten). Decoding the string value first
   turns `\n` into a real newline, making the boundary unambiguous. Compact
   re-serialization (`ensure_ascii=False, separators=(',',':')`) reproduces
   Claude Code's lines byte-for-byte, so this costs nothing in diff cleanliness.

4. Rewriting is BOUNDARY-ANCHORED, never a naked substring replace: the root is
   rewritten only when it stands as a whole path prefix, so `~/src/catalog` is
   rewritten but `~/src/catalog-backup` and `/mnt/~/src/catalog` are not.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

DEFAULT_SENTINEL = "{{CC_PROJECT_ROOT}}"

# A leading alphanumeric would mean the root is the tail of a longer path run
# (e.g. `/mnt/Users/.../catalog` — a different path that ends with our root).
_LEADING_TOKEN_CHARS = r"A-Za-z0-9"

_JSON_DUMP = dict(ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# Encoding (one-way, lossy)
# --------------------------------------------------------------------------- #
def encode_project_dir(path: str) -> str:
    """Reproduce Claude Code's `~/.claude/projects/<name>` directory encoding.

    Empirically: every character that is not `[A-Za-z0-9]` becomes `-` (leading
    separator included). NOTE: lossy — many distinct real paths collapse to the
    same encoded name. Used to compute a *target* machine's dir name from its
    known root, never to recover a root from a name.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)


# --------------------------------------------------------------------------- #
# Literal-sentinel escaping (makes canonicalize/localize a true inverse pair)
# --------------------------------------------------------------------------- #
def _sentinel_parts(sentinel: str) -> tuple[str, str]:
    if not (sentinel.startswith("{{") and sentinel.endswith("}}")):
        raise ValueError(f"sentinel must look like {{{{NAME}}}}: {sentinel!r}")
    return sentinel[:-2], "}}"  # (open_base, close)


def _escape_literals(text: str, sentinel: str) -> str:
    """Lift any pre-existing sentinel-family token one level so localize can
    restore it: `{{S}}` -> `{{S_LIT}}`, `{{S_LIT}}` -> `{{S_LIT_LIT}}`, ... The
    whole `{{S(_LIT)*}}` family maps injectively up one level, so it is
    regress-free. Needed because this project's own context contains the
    sentinel string verbatim.
    """
    base, close = _sentinel_parts(sentinel)
    fam = re.compile(re.escape(base) + r"((?:_LIT)*)" + re.escape(close))
    return fam.sub(lambda m: base + m.group(1) + "_LIT" + close, text)


def _unescape_literals(text: str, sentinel: str) -> str:
    """Inverse of _escape_literals: lower escaped family members one level.
    Zero-`_LIT` members are left alone (those are real, root-derived sentinels,
    already expanded to the root before this runs)."""
    base, close = _sentinel_parts(sentinel)
    fam = re.compile(re.escape(base) + r"((?:_LIT)+)" + re.escape(close))
    return fam.sub(lambda m: base + m.group(1)[len("_LIT"):] + close, text)


# --------------------------------------------------------------------------- #
# String-value primitives (operate on a single DECODED string)
# --------------------------------------------------------------------------- #
def _boundary_pattern(root: str) -> re.Pattern[str]:
    """Match `root` only where it stands as a whole path prefix.

    Trailing rules (validated against real transcripts):
      - A following `[A-Za-z0-9_-]` is name continuation -> NO match
        (`catalog-backup`, `catalog2`, `catalog_old` are siblings, not the root).
      - A following `.` is an extension ONLY if itself followed by an
        alphanumeric (`catalog.bak`) -> NO match; a trailing `.` before
        whitespace/quote/end is sentence punctuation ("...in <root>. Next") ->
        match. This distinction is what clears the residue dogfooding surfaced.
      - A following `/` is the normal `<root>/child` case -> match.
    """
    return re.compile(
        r"(?<![" + _LEADING_TOKEN_CHARS + r"])"
        + re.escape(root)
        + r"(?![A-Za-z0-9_-])"      # not a name-continuation character
        + r"(?!\.[A-Za-z0-9])"      # not an extension dot (allow trailing period)
    )


def canonicalize_value(
    s: str, root: str, sentinel: str = DEFAULT_SENTINEL
) -> tuple[str, int]:
    """Canonicalize one decoded string value. Escapes literal sentinels, then
    replaces boundary-anchored root occurrences. Returns (value, n_root_subs)."""
    s = _escape_literals(s, sentinel)
    return _boundary_pattern(root).subn(sentinel, s)


def localize_value(
    s: str, root: str, sentinel: str = DEFAULT_SENTINEL
) -> tuple[str, int]:
    """Localize one decoded string value — the inverse of canonicalize_value.
    Expands real (zero-`_LIT`) sentinels to root, then restores escaped
    literals. Returns (value, n_sentinel_expansions)."""
    base, close = _sentinel_parts(sentinel)
    exact = re.compile(re.escape(base) + r"(?!_LIT)" + re.escape(close))
    s, n = exact.subn(lambda _m: root, s)  # lambda: root may contain backslashes
    s = _unescape_literals(s, sentinel)
    return s, n


# --------------------------------------------------------------------------- #
# JSONL entry points (the real surface — walk records, transform string leaves)
# --------------------------------------------------------------------------- #
def _map_strings(obj, fn):
    """Return obj with fn applied to every string leaf; accumulate a count."""
    if isinstance(obj, dict):
        out = {}
        total = 0
        for k, v in obj.items():
            out[k], c = _map_strings(v, fn)
            total += c
        return out, total
    if isinstance(obj, list):
        out = []
        total = 0
        for v in obj:
            nv, c = _map_strings(v, fn)
            out.append(nv)
            total += c
        return out, total
    if isinstance(obj, str):
        return fn(obj)
    return obj, 0


def _transform_jsonl(text: str, fn) -> tuple[str, int]:
    """Apply a string-value transform fn to every JSON line. Lines that are not
    valid JSON are passed through untouched (doctor surfaces those)."""
    out_lines = []
    total = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        nl = line[len(body):]
        if not body:
            out_lines.append(line)
            continue
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            out_lines.append(line)  # leave unparseable lines exactly as-is
            continue
        new_obj, c = _map_strings(obj, fn)
        total += c
        out_lines.append(json.dumps(new_obj, **_JSON_DUMP) + nl)
    return "".join(out_lines), total


def canonicalize_jsonl(
    text: str, root: str, sentinel: str = DEFAULT_SENTINEL
) -> tuple[str, int]:
    """Canonicalize a full JSONL document (local form -> canonical form)."""
    return _transform_jsonl(text, lambda s: canonicalize_value(s, root, sentinel))


def localize_jsonl(
    text: str, root: str, sentinel: str = DEFAULT_SENTINEL
) -> tuple[str, int]:
    """Localize a full JSONL document (canonical form -> local form for root)."""
    return _transform_jsonl(text, lambda s: localize_value(s, root, sentinel))


# --------------------------------------------------------------------------- #
# Participant + root inference
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Participant:
    """One machine's roster entry — the unit of path rewriting."""

    machine_id: str
    os: str
    home: str
    project_root: str

    @property
    def encoded_dir(self) -> str:
        return encode_project_dir(self.project_root)

    def canonicalize(self, text: str, sentinel: str = DEFAULT_SENTINEL) -> tuple[str, int]:
        return canonicalize_jsonl(text, self.project_root, sentinel)

    def localize(self, text: str, sentinel: str = DEFAULT_SENTINEL) -> tuple[str, int]:
        return localize_jsonl(text, self.project_root, sentinel)


def _ancestors(path: str) -> list[str]:
    path = path.rstrip("/")
    out = []
    while path and path != "/":
        out.append(path)
        path = os.path.dirname(path)
    return out


def infer_project_root(encoded_dir_name: str, cwds: list[str]) -> str | None:
    """Recover the real project root from observed `cwd` values.

    The encoded dir name is lossy, so we cannot decode it — but we can *test
    candidates*: the project root is the cwd (or an ancestor of one) whose
    encoding equals the context directory's name. This handles the subdir-cwd
    case (cwd `~/src/catalog/app` recovers root `~/src/catalog`) and validates
    against the real encoding instead of guessing. Returns the shallowest match,
    or None (which doctor surfaces as "supply --root explicitly").
    """
    name = os.path.basename(encoded_dir_name.rstrip("/"))
    candidates: list[str] = []
    seen = set()
    for cwd in cwds:
        for anc in _ancestors(cwd):
            if anc not in seen:
                seen.add(anc)
                candidates.append(anc)
    matches = [c for c in candidates if encode_project_dir(c) == name]
    if not matches:
        return None
    return min(matches, key=lambda c: c.count("/"))
