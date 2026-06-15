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
from functools import lru_cache

# Bump whenever the canonicalization logic changes (encoding, mappings, sentinel
# scheme, separator handling, ...). Stored in local state; a mismatch forces a
# full re-process so incremental sync never serves stale-canonical files after an
# upgrade. (This is the encoded-dir-tier upgrade case, made automatic.)
CANON_VERSION = 1

DEFAULT_SENTINEL = "{{CC_PROJECT_ROOT}}"
# The three rewrite tiers (cluster-wide policy; see build_mappings). Ordered here
# most-specific to least; applied longest-anchor-first so specific beats general.
SENTINEL_PROJECT_ROOT = "{{CC_PROJECT_ROOT}}"
SENTINEL_CONTEXT_DIR = "{{CC_PROJECT_CONTEXT_DIR}}"  # <home>/.claude/projects/<encoded>
SENTINEL_ENCODED_DIR = "{{CC_ENCODED_DIR}}"          # the bare <encoded> dir name
SENTINEL_HOME = "{{CC_HOME}}"
ALL_SENTINELS = (SENTINEL_CONTEXT_DIR, SENTINEL_PROJECT_ROOT, SENTINEL_ENCODED_DIR, SENTINEL_HOME)

# A leading alphanumeric would mean the root is the tail of a longer path run
# (e.g. `/mnt/Users/.../catalog` — a different path that ends with our root).
_LEADING_TOKEN_CHARS = r"A-Za-z0-9"

# The path tail after an anchor: runs of (separator + a path-name segment).
# Captured so its separators can be normalized between platforms (canonical form
# uses `/`). The segment class is deliberately RESTRICTIVE — only characters that
# are unambiguously part of a filename — so the greedy tail never consumes across
# a token boundary (`(`, `,`, `=`, a quote, …) and swallows the next path. Worst
# case it stops early at an unusual filename char, which is harmless: anything
# past the last captured separator has no separator to normalize.
_TAIL = r"((?:[\\/][A-Za-z0-9._~+-]*)*)"


def native_sep(os_name: str) -> str:
    return "\\" if os_name == "windows" else "/"


def _to_canonical_seps(tail: str, src_sep: str) -> str:
    # Normalize a path tail to the canonical `/` (only Windows differs).
    return tail.replace("\\", "/") if src_sep == "\\" else tail


def _from_canonical_seps(tail: str, dst_sep: str) -> str:
    # Convert a canonical `/` tail to the target machine's native separator.
    return tail.replace("/", "\\") if dst_sep == "\\" else tail

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


# All the compiled regexes below depend ONLY on a constant anchor/sentinel
# string, but they're applied to every string value in a transcript (hundreds of
# thousands per file). Memoize them — recompiling per value (with re.escape on
# the hot path) was the dominant cost (~13s of 17s on a 36MB file).
@lru_cache(maxsize=512)
def _escape_family(sentinel: str):
    base, close = _sentinel_parts(sentinel)
    return re.compile(re.escape(base) + r"((?:_LIT)*)" + re.escape(close)), base, close


@lru_cache(maxsize=512)
def _unescape_family(sentinel: str):
    base, close = _sentinel_parts(sentinel)
    return re.compile(re.escape(base) + r"((?:_LIT)+)" + re.escape(close)), base, close


@lru_cache(maxsize=512)
def _exact_sentinel(sentinel: str):
    base, close = _sentinel_parts(sentinel)
    return re.compile(re.escape(base) + r"(?!_LIT)" + re.escape(close) + _TAIL)


def _escape_literals(text: str, sentinel: str) -> str:
    """Lift any pre-existing sentinel-family token one level so localize can
    restore it: `{{S}}` -> `{{S_LIT}}`, `{{S_LIT}}` -> `{{S_LIT_LIT}}`, ... The
    whole `{{S(_LIT)*}}` family maps injectively up one level, so it is
    regress-free. Needed because this project's own context contains the
    sentinel string verbatim.
    """
    fam, base, close = _escape_family(sentinel)
    return fam.sub(lambda m: base + m.group(1) + "_LIT" + close, text)


def _unescape_literals(text: str, sentinel: str) -> str:
    """Inverse of _escape_literals: lower escaped family members one level.
    Zero-`_LIT` members are left alone (those are real, root-derived sentinels,
    already expanded to the root before this runs)."""
    fam, base, close = _unescape_family(sentinel)
    return fam.sub(lambda m: base + m.group(1)[len("_LIT"):] + close, text)


# --------------------------------------------------------------------------- #
# String-value primitives (operate on a single DECODED string)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=512)
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
        + _TAIL                     # consume the path tail so we can normalize seps
    )


Mapping = tuple[str, str]  # (anchor path, sentinel)


def build_mappings(home: str, project_root: str, encoded_dir: str,
                   rewrite_home: bool = True) -> list[Mapping]:
    """The cluster-wide rewrite tiers for one machine, ordered longest-anchor
    first so the specific beats the general:

      1. project root                  -> {{CC_PROJECT_ROOT}}
      2. <home>/.claude/projects/<enc> -> {{CC_PROJECT_CONTEXT_DIR}}  (the tool's
         own context dir; both home AND the lossy encoded segment change per
         machine, so it gets its own exact sentinel)
      3. <encoded_dir>                 -> {{CC_ENCODED_DIR}}  (the bare encoded
         dir name, which appears standalone or inside tilde paths
         `~/.claude/projects/<enc>` — common in memory files, where the absolute
         context-dir tier doesn't reach)
      4. <home>                        -> {{CC_HOME}}  (optional; covers dotfiles,
         other ~/.claude paths, and sibling projects by the ~/ convention)

    Applied longest-first, so the absolute context-dir path wins over the bare
    encoded name where both could match. Every machine builds this with its OWN
    home/root/encoded but the SAME policy, so the canonical form is identical
    across machines.
    """
    m: list[Mapping] = [
        (project_root, SENTINEL_PROJECT_ROOT),
        (f"{home}/.claude/projects/{encoded_dir}", SENTINEL_CONTEXT_DIR),
        (encoded_dir, SENTINEL_ENCODED_DIR),
    ]
    if rewrite_home:
        m.append((home, SENTINEL_HOME))
    return sorted(m, key=lambda t: len(t[0]), reverse=True)


_SENTINEL_PROBE = "{{CC_"  # all sentinels start with this; gate the escape passes


def canonicalize_value(s: str, mappings: list[Mapping], src_sep: str = "/") -> tuple[str, int]:
    """Canonicalize one decoded string value against an ordered mapping set.
    Escapes any literal sentinels first, then boundary-replaces each anchor
    (longest first), normalizing the matched path tail's separators from the
    source machine's native sep to canonical `/`. Returns (value, n).

    Cheap substring pre-checks gate every regex: a value that doesn't contain a
    given anchor (or any sentinel) skips that scan entirely — most string values
    mention at most one anchor, so this avoids the bulk of the regex work."""
    if _SENTINEL_PROBE in s:
        s = _escape_literals_multi(s, [sent for _, sent in mappings])
    n = 0
    for anchor, sentinel in mappings:
        if anchor not in s:           # a substring miss is a guaranteed regex miss
            continue
        def repl(m, sent=sentinel):
            return sent + _to_canonical_seps(m.group(1), src_sep)
        s, c = _boundary_pattern(anchor).subn(repl, s)
        n += c
    return s, n


def localize_value(s: str, mappings: list[Mapping], dst_sep: str = "/") -> tuple[str, int]:
    """Inverse of canonicalize_value: expand each real (zero-`_LIT`) sentinel to
    its anchor, converting the canonical `/` tail to the target machine's native
    separator, then restore escaped literal sentinels."""
    if _SENTINEL_PROBE not in s:      # no sentinels at all -> nothing to localize
        return s, 0
    n = 0
    for anchor, sentinel in mappings:
        if sentinel[:-2] not in s:    # sentinel base absent -> skip
            continue
        def repl(m, a=anchor):  # a=: bind; anchors may hold backslashes
            return a + _from_canonical_seps(m.group(1), dst_sep)
        s, c = _exact_sentinel(sentinel).subn(repl, s)
        n += c
    s = _unescape_literals_multi(s, [sent for _, sent in mappings])
    return s, n


def _escape_literals_multi(text: str, sentinels) -> str:
    for sent in sentinels:
        text = _escape_literals(text, sent)
    return text


def _unescape_literals_multi(text: str, sentinels) -> str:
    for sent in sentinels:
        text = _unescape_literals(text, sent)
    return text


def _single(root: str, sentinel: str) -> list[Mapping]:
    return [(root, sentinel)]


def canonicalize_value_root(s, root, sentinel=DEFAULT_SENTINEL, src_sep="/"):
    """Single-anchor convenience (root only) used by lower-level callers/tests."""
    return canonicalize_value(s, _single(root, sentinel), src_sep)


def localize_value_root(s, root, sentinel=DEFAULT_SENTINEL, dst_sep="/"):
    return localize_value(s, _single(root, sentinel), dst_sep)


# --------------------------------------------------------------------------- #
# JSONL entry points (the real surface — walk records, transform string leaves)
# --------------------------------------------------------------------------- #
def _map_strings(obj, fn):
    """Return obj with fn applied to every string leaf AND every dict key;
    accumulate a count. Keys matter: tool results keep path-keyed maps (e.g.
    snapshot `trackedFileBackups` keyed by absolute file path), and those keys
    are just as machine-specific as values."""
    if isinstance(obj, dict):
        out = {}
        total = 0
        for k, v in obj.items():
            if isinstance(k, str):
                k, ck = fn(k)
                total += ck
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


def canonicalize_jsonl(text: str, mappings: list[Mapping], src_sep: str = "/") -> tuple[str, int]:
    """Canonicalize a full JSONL document (local form -> canonical form)."""
    return _transform_jsonl(text, lambda s: canonicalize_value(s, mappings, src_sep))


def localize_jsonl(text: str, mappings: list[Mapping], dst_sep: str = "/") -> tuple[str, int]:
    """Localize a full JSONL document (canonical form -> local form)."""
    return _transform_jsonl(text, lambda s: localize_value(s, mappings, dst_sep))


def canonicalize_jsonl_root(text, root, sentinel=DEFAULT_SENTINEL):
    """Single-anchor convenience (root only)."""
    return canonicalize_jsonl(text, _single(root, sentinel))


def localize_jsonl_root(text, root, sentinel=DEFAULT_SENTINEL):
    return localize_jsonl(text, _single(root, sentinel))


def normalize_jsonl(text: str) -> str:
    """Re-serialize every JSON line compactly without touching any string value.

    This is the formatting normalization canonicalize/localize apply incidentally
    (parse + compact dump). Used by the push guard to check *data* reversibility
    rather than byte-identity of incidental formatting — real Claude Code lines
    are already compact, so for them normalize is the identity."""
    return _transform_jsonl(text, lambda s: (s, 0))[0]


# --------------------------------------------------------------------------- #
# Root inference
# --------------------------------------------------------------------------- #
def _ancestors(path: str) -> list[str]:
    """`/a/b/c` -> ['/a/b/c', '/a/b', '/a', '/'] up to the filesystem root.

    Terminates at the root on EVERY platform by detecting that `os.path.dirname`
    has reached a fixpoint — POSIX `/` and Windows `C:\\` both satisfy
    dirname(p) == p. (The old `while path != "/"` check looped forever on Windows,
    where the drive root is never `/`, eating unbounded memory.)"""
    out = []
    seen = set()
    while path and path not in seen:
        out.append(path)
        seen.add(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
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
