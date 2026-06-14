"""The correctness core. Run: python -m unittest discover -s tests

The load-bearing invariant is idempotent, inverse round-tripping:
    localize(canonicalize(local), root)        == local
    canonicalize(localize(canonical), root)    == canonical
for any participant root. These are property tests (randomized corpora) plus
targeted boundary cases and a round-trip against the real local context dir.

Two levels are tested: `*_value` (a single decoded string — where the boundary
logic lives) and `*_jsonl` (full documents — where parsing/re-serialization and
the path-surface live).
"""

from __future__ import annotations

import glob
import json
import os
import random
import unittest

from convergence.pathmap import (
    DEFAULT_SENTINEL,
    SENTINEL_CONTEXT_DIR,
    SENTINEL_ENCODED_DIR,
    SENTINEL_HOME,
    SENTINEL_PROJECT_ROOT,
    build_mappings,
    canonicalize_jsonl_root as canonicalize_jsonl,
    canonicalize_value_root as canonicalize_value,
    encode_project_dir,
    infer_project_root,
    localize_jsonl_root as localize_jsonl,
    localize_value_root as localize_value,
)
from convergence.pathmap import canonicalize_jsonl as canonicalize_jsonl_m
from convergence.pathmap import localize_jsonl as localize_jsonl_m
from convergence.pathmap import canonicalize_value as canonicalize_value_m
from convergence.pathmap import localize_value as localize_value_m
from convergence.roster import Participant

# Known (real_root -> encoded_dir) pairs captured from this machine on 2026-06-13.
ENCODING_PAIRS = [
    ("/Users/bryonwilliams/src/context-convergence", "-Users-bryonwilliams-src-context-convergence"),
    ("/Users/bryonwilliams/src/deepdrift.zero", "-Users-bryonwilliams-src-deepdrift-zero"),
    ("/Users/bryonwilliams/projects/token-track", "-Users-bryonwilliams-projects-token-track"),
    ("/Users/bryonwilliams/Library/Mobile Documents/iCloud~md~obsidian/Documents/tick",
     "-Users-bryonwilliams-Library-Mobile-Documents-iCloud-md-obsidian-Documents-tick"),
]


class TestEncoding(unittest.TestCase):
    def test_known_pairs(self):
        for root, encoded in ENCODING_PAIRS:
            self.assertEqual(encode_project_dir(root), encoded)

    def test_encoding_is_lossy(self):
        a = encode_project_dir("/Users/x/src/deepdrift.zero")
        b = encode_project_dir("/Users/x/src/deepdrift-zero")
        c = encode_project_dir("/Users/x/src/deepdrift_zero")
        self.assertEqual(a, b)
        self.assertEqual(b, c)


class TestBoundaryAnchoring(unittest.TestCase):
    ROOT = "/Users/bryonwilliams/src/catalog"

    def _canon(self, value):
        return canonicalize_value(value, self.ROOT)[0]

    def test_rewrites_root_itself(self):
        self.assertEqual(self._canon(self.ROOT), DEFAULT_SENTINEL)

    def test_rewrites_child_paths(self):
        self.assertEqual(self._canon(f"{self.ROOT}/app/main.py"),
                         f"{DEFAULT_SENTINEL}/app/main.py")

    def test_does_not_rewrite_suffixed_sibling(self):
        for sib in ("-backup", "_old", ".bak", "2"):
            value = f"{self.ROOT}{sib}/x"
            self.assertEqual(self._canon(value), value, f"clobbered {sib}")

    def test_does_not_rewrite_as_suffix_of_longer_path(self):
        value = f"/mnt{self.ROOT}"
        self.assertEqual(self._canon(value), value)

    def test_rewrites_path_after_real_newline(self):
        # The decoded-string win: a path on its own line IS rewritten, unlike at
        # the raw-text level where the preceding `\n` escape blocked it.
        value = f"some stdout line\n{self.ROOT}/app\nmore"
        self.assertEqual(self._canon(value),
                         f"some stdout line\n{DEFAULT_SENTINEL}/app\nmore")

    def test_rewrites_trailing_period_punctuation(self):
        # "git init in <root>. Single monorepo" — the period is punctuation.
        for tail in (". Next", '.","x', ".</tool>", "."):
            self.assertEqual(self._canon(self.ROOT + tail), DEFAULT_SENTINEL + tail)

    def test_does_not_rewrite_extension_dot(self):
        # <root>.bak / <root>.py are filenames, not the bare root.
        for ext in (".bak", ".py", ".jsonl"):
            self.assertEqual(self._canon(self.ROOT + ext), self.ROOT + ext)

    def test_handles_root_with_dot(self):
        root = "/Users/bryonwilliams/src/deepdrift.zero"
        self.assertEqual(canonicalize_value(f"{root}/x.py", root)[0],
                         f"{DEFAULT_SENTINEL}/x.py")


class TestIdempotencyProperty(unittest.TestCase):
    """Randomized property tests of the inverse/idempotent round-trip, at the
    decoded-string-value level where the boundary logic lives."""

    ROOTS = [
        "/Users/bryonwilliams/src/catalog",
        "/home/bryon/work/submatrix",
        "/Users/x/src/deepdrift.zero",
    ]
    SUFFIXES_INPROJECT = ["", "/a/b.py", "/x", "/a/b/c/d.jsonl", "."]
    # Sibling paths that SHARE the root prefix but must NOT be rewritten.
    SUFFIXES_SIBLING = ["-backup/y", ".bak", "_old/z", "2/w"]
    NOISE = ["hello", "/etc/passwd", "/Users/bryonwilliams/.claude/x",
             "catalog", "the root is here:", "}{][", "/Users/bryonwilliams"]
    SENTINEL_NOISE = ["{{CC_PROJECT_ROOT}}", "{{CC_PROJECT_ROOT_LIT}}"]
    # Non-alnum separators -> clean boundaries (mirrors decoded transcript text).
    SEPS = [" ", "\n", '"', ":", "=", "(", "', '", "\t"]

    def _random_value(self, rnd, root, *, realistic, siblings=True):
        joins = self.SEPS if realistic else self.SEPS + ["", ""]
        sufs = self.SUFFIXES_INPROJECT + (self.SUFFIXES_SIBLING if siblings else [])
        parts = []
        for _ in range(rnd.randint(1, 12)):
            r = rnd.random()
            if r < 0.45:
                parts.append(root + rnd.choice(sufs))
            elif r < 0.6:
                parts.append(rnd.choice(self.SENTINEL_NOISE))
            else:
                parts.append(rnd.choice(self.NOISE))
            parts.append(rnd.choice(joins))
        return "".join(parts)

    def test_inverse_roundtrip_always_holds(self):
        """localize(canonicalize(x)) == x for ANY input, including adversarial
        adjacency, sibling paths, and literal sentinels — the load-bearing
        invariant."""
        rnd = random.Random(20260613)
        for _ in range(4000):
            root = rnd.choice(self.ROOTS)
            local = self._random_value(rnd, root, realistic=False)
            canon, _ = canonicalize_value(local, root)
            self.assertEqual(localize_value(canon, root)[0], local)
            self.assertEqual(
                canonicalize_value(localize_value(canon, root)[0], root)[0], canon)

    def test_canonicalize_is_idempotent(self):
        """A second canonicalize pass finds nothing to rewrite — the canonical
        form holds no remaining boundary-anchored root occurrence. (A plain
        substring check would be wrong: a sibling like `<root>-backup` legitly
        keeps the root as a non-anchored substring.)"""
        rnd = random.Random(99)
        for _ in range(4000):
            root = rnd.choice(self.ROOTS)
            local = self._random_value(rnd, root, realistic=True)
            canon, _ = canonicalize_value(local, root)
            self.assertEqual(canonicalize_value(canon, root)[1], 0)
            self.assertEqual(localize_value(canon, root)[0], local)

    def test_sibling_paths_are_not_rewritten(self):
        """v1 policy made explicit: paths sharing the root prefix but not equal
        to it survive untouched, so they remain machine-specific in canonical
        form (doctor flags these). This is a known, deliberate limitation."""
        root = "/home/bryon/src/submatrix"
        for suf in self.SUFFIXES_SIBLING:
            value = root + suf
            self.assertEqual(canonicalize_value(value, root)[0], value)

    def test_literal_sentinel_survives_roundtrip(self):
        root = "/Users/b/src/proj"
        for lit in ("{{CC_PROJECT_ROOT}}", "see {{CC_PROJECT_ROOT}} then /Users/b/src/proj/x"):
            canon, _ = canonicalize_value(lit, root)
            self.assertEqual(localize_value(canon, root)[0], lit)

    def test_cross_machine_canonical_form_is_identical(self):
        """The point of canonical form: identical machine-neutral bytes whoever
        produced them, so git diffs stay clean across machines."""
        rnd = random.Random(7)
        a, b = "/home/bryon/src/submatrix", "/Users/bryonwilliams/src/submatrix"
        for _ in range(1000):
            # siblings=False: identity holds only for in-project refs, which are
            # the ones canonical form actually neutralizes (see sibling test).
            shape = self._random_value(rnd, "\x00ROOT\x00", realistic=True, siblings=False)
            local_a = shape.replace("\x00ROOT\x00", a)
            local_b = shape.replace("\x00ROOT\x00", b)
            canon_a, _ = canonicalize_value(local_a, a)
            canon_b, _ = canonicalize_value(local_b, b)
            self.assertEqual(canon_a, canon_b)
            self.assertEqual(localize_value(canon_a, b)[0], local_b)


class TestJsonl(unittest.TestCase):
    ROOT = "/Users/b/src/proj"

    def _record(self):
        return {
            "cwd": self.ROOT,
            "message": {"content": [
                {"input": {"file_path": f"{self.ROOT}/a.py",
                           "command": f"cd {self.ROOT} && cat x"}},
            ]},
            "toolUseResult": {
                "filePath": f"{self.ROOT}/a.py",
                # path embedded after a real newline inside stdout:
                "stdout": f"running...\n{self.ROOT}/a.py:1: ok\n",
                # a sibling/home path that must NOT be rewritten:
                "extra": "/Users/b/.claude/projects/x",
            },
        }

    def test_record_roundtrip_and_surface(self):
        doc = json.dumps(self._record(), ensure_ascii=False, separators=(",", ":")) + "\n"
        canon, n = canonicalize_jsonl(doc, self.ROOT)
        self.assertGreaterEqual(n, 5)            # cwd, file_path x2, command, stdout
        self.assertNotIn(self.ROOT, canon)       # fully canonicalized
        self.assertIn("/Users/b/.claude/projects/x", canon)  # home path untouched
        self.assertEqual(localize_jsonl(canon, self.ROOT)[0], doc)  # byte-exact

    def test_cross_machine_jsonl(self):
        a, b = "/Users/b/src/proj", "/home/bryon/src/proj"
        doc_a = json.dumps(self._record(), ensure_ascii=False, separators=(",", ":")) + "\n"
        canon, _ = canonicalize_jsonl(doc_a, a)
        local_b, _ = localize_jsonl(canon, b)
        self.assertIn(f"{b}/a.py", local_b)
        self.assertNotIn(f"{a}/a.py", local_b)
        self.assertEqual(canonicalize_jsonl(local_b, b)[0], canon)

    def test_unparseable_line_passes_through(self):
        doc = "not json at all\n" + json.dumps({"cwd": self.ROOT}) + "\n"
        canon, _ = canonicalize_jsonl(doc, self.ROOT)
        self.assertTrue(canon.startswith("not json at all\n"))


class TestParticipant(unittest.TestCase):
    def test_encoded_dir_property(self):
        p = Participant("mac", "darwin", "/Users/bryonwilliams",
                        "/Users/bryonwilliams/src/deepdrift.zero")
        self.assertEqual(p.encoded_dir, "-Users-bryonwilliams-src-deepdrift-zero")

    def test_participant_roundtrip(self):
        p = Participant("mac", "darwin", "/Users/b", "/Users/b/src/proj")
        local = '{"cwd":"/Users/b/src/proj","f":"/Users/b/src/proj/a.py"}\n'
        canon, n = p.canonicalize(local)
        self.assertEqual(n, 2)
        self.assertEqual(p.localize(canon)[0], local)


class TestInferRoot(unittest.TestCase):
    def test_infers_from_exact_cwd(self):
        self.assertEqual(
            infer_project_root("-Users-bryonwilliams-src-context-convergence",
                               ["/Users/bryonwilliams/src/context-convergence"]),
            "/Users/bryonwilliams/src/context-convergence")

    def test_infers_root_from_subdir_cwd(self):
        self.assertEqual(
            infer_project_root("-Users-bryonwilliams-src-catalog",
                               ["/Users/bryonwilliams/src/catalog/app"]),
            "/Users/bryonwilliams/src/catalog")

    def test_recovers_lossy_dot_root(self):
        self.assertEqual(
            infer_project_root("-Users-bryonwilliams-src-deepdrift-zero",
                               ["/Users/bryonwilliams/src/deepdrift.zero"]),
            "/Users/bryonwilliams/src/deepdrift.zero")

    def test_returns_none_when_unrecoverable(self):
        self.assertIsNone(infer_project_root("-totally-unrelated", ["/Users/x/y"]))


class TestRealCorpus(unittest.TestCase):
    """Round-trip the real local context dir if present (skips elsewhere)."""

    DIR = os.path.expanduser(
        "~/.claude/projects/-Users-bryonwilliams-src-context-convergence")
    ROOT = "/Users/bryonwilliams/src/context-convergence"

    def test_real_roundtrip(self):
        files = glob.glob(os.path.join(self.DIR, "*.jsonl"))
        if not files:
            self.skipTest("no local context dir on this machine")
        for f in files:
            with open(f, encoding="utf-8", errors="replace") as fh:
                original = fh.read()
            canon, n = canonicalize_jsonl(original, self.ROOT)
            self.assertGreater(n, 0, f"expected root occurrences in {f}")
            self.assertNotIn(self.ROOT, canon, f"root residue in {f}")
            self.assertEqual(localize_jsonl(canon, self.ROOT)[0], original,
                             f"round-trip changed {f}")


class TestTieredMappings(unittest.TestCase):
    """The home-path gap closed: project root + own context dir + home prefix."""

    A_HOME, A_ROOT = "/Users/alice", "/Users/alice/src/demo"
    B_HOME, B_ROOT = "/home/bob", "/home/bob/work/demo"

    def _maps(self, home, root, rewrite_home=True):
        return build_mappings(home, root, encode_project_dir(root), rewrite_home)

    def _doc(self, home, root):
        enc = encode_project_dir(root)
        return json.dumps({
            "root": f"{root}/a.py",
            "ctx": f"{home}/.claude/projects/{enc}/x.jsonl",
            "dot": f"{home}/.cargo/bin/cargo",
            "sib": f"{home}/src/other-project/y.py",
            "plain": f"running in {root}.",  # trailing-period punctuation
        }, separators=(",", ":")) + "\n"

    def test_mapping_order_is_longest_anchor_first(self):
        anchors = [a for a, _ in self._maps(self.A_HOME, self.A_ROOT)]
        self.assertEqual(anchors, sorted(anchors, key=len, reverse=True))

    def test_all_three_tiers_rewrite_and_roundtrip(self):
        doc = self._doc(self.A_HOME, self.A_ROOT)
        maps = self._maps(self.A_HOME, self.A_ROOT)
        canon, _ = canonicalize_jsonl_m(doc, maps)
        self.assertNotIn(self.A_ROOT, canon)
        self.assertNotIn(self.A_HOME, canon)        # nothing machine-specific leaks
        for sent in (SENTINEL_PROJECT_ROOT, SENTINEL_CONTEXT_DIR, SENTINEL_HOME):
            self.assertIn(sent, canon)
        self.assertEqual(localize_jsonl_m(canon, maps)[0], doc)

    def test_cross_machine_all_tiers(self):
        a_doc = self._doc(self.A_HOME, self.A_ROOT)
        canon, _ = canonicalize_jsonl_m(a_doc, self._maps(self.A_HOME, self.A_ROOT))
        b_text, _ = localize_jsonl_m(canon, self._maps(self.B_HOME, self.B_ROOT))
        b_enc = encode_project_dir(self.B_ROOT)
        self.assertIn(f"{self.B_ROOT}/a.py", b_text)
        # context dir expands to B's home AND B's (different, lossy) encoded dir:
        self.assertIn(f"{self.B_HOME}/.claude/projects/{b_enc}/x.jsonl", b_text)
        self.assertIn(f"{self.B_HOME}/.cargo/bin/cargo", b_text)
        self.assertIn(f"{self.B_HOME}/src/other-project/y.py", b_text)
        self.assertNotIn(self.A_HOME, b_text)
        # canonical form is identical regardless of which machine produced it:
        b_doc = self._doc(self.B_HOME, self.B_ROOT)
        canon_b, _ = canonicalize_jsonl_m(b_doc, self._maps(self.B_HOME, self.B_ROOT))
        self.assertEqual(canon, canon_b)

    def test_bare_encoded_dir_and_tilde_paths(self):
        # Memory files write the encoded dir bare or inside a tilde path; the
        # absolute context-dir tier doesn't reach those, the encoded-dir tier does.
        a_enc = encode_project_dir(self.A_ROOT)
        text = f"see `{a_enc}/` and ~/.claude/projects/{a_enc}/x.jsonl\n"
        maps = self._maps(self.A_HOME, self.A_ROOT)
        canon, _ = canonicalize_value_m(text, maps)
        self.assertNotIn(a_enc, canon)                  # bare encoded dir rewritten
        self.assertIn(SENTINEL_ENCODED_DIR, canon)
        self.assertEqual(localize_value_m(canon, maps)[0], text)   # round-trips on A
        # localizes to B's home (~ stays) AND B's encoded dir:
        b_text, _ = localize_value_m(canon, self._maps(self.B_HOME, self.B_ROOT))
        b_enc = encode_project_dir(self.B_ROOT)
        self.assertIn(f"`{b_enc}/`", b_text)
        self.assertIn(f"~/.claude/projects/{b_enc}/x.jsonl", b_text)
        self.assertNotIn(a_enc, b_text)

    def test_absolute_context_dir_beats_bare_encoded(self):
        # An absolute context-dir path must use the context-dir sentinel, not get
        # split by the shorter encoded-dir tier.
        a_enc = encode_project_dir(self.A_ROOT)
        s = f"{self.A_HOME}/.claude/projects/{a_enc}/m.jsonl"
        canon, _ = canonicalize_value_m(s, self._maps(self.A_HOME, self.A_ROOT))
        self.assertIn(f"{SENTINEL_CONTEXT_DIR}/m.jsonl", canon)
        self.assertNotIn(SENTINEL_ENCODED_DIR, canon)

    def test_context_dir_beats_home_tier(self):
        # The context-dir path must not be half-eaten by the shorter home anchor.
        enc = encode_project_dir(self.A_ROOT)
        doc = json.dumps({"p": f"{self.A_HOME}/.claude/projects/{enc}/m.jsonl"},
                         separators=(",", ":")) + "\n"
        canon, _ = canonicalize_jsonl_m(doc, self._maps(self.A_HOME, self.A_ROOT))
        self.assertIn(f"{SENTINEL_CONTEXT_DIR}/m.jsonl", canon)
        self.assertNotIn(SENTINEL_HOME, canon)

    def test_path_keyed_dicts_are_rewritten(self):
        # Tool results keep maps keyed by absolute path (e.g. trackedFileBackups);
        # the KEYS are as machine-specific as values and must rewrite + round-trip.
        doc = json.dumps({"snapshot": {"trackedFileBackups": {
            f"{self.A_ROOT}/a.py": {"checksum": "x"},
            f"{self.A_HOME}/.config/app.ini": {"checksum": "y"},
        }}}, separators=(",", ":")) + "\n"
        maps = self._maps(self.A_HOME, self.A_ROOT)
        canon, _ = canonicalize_jsonl_m(doc, maps)
        self.assertNotIn(self.A_HOME, canon)        # keys rewritten too
        self.assertIn(SENTINEL_PROJECT_ROOT, canon)
        self.assertIn(SENTINEL_HOME, canon)
        self.assertEqual(localize_jsonl_m(canon, maps)[0], doc)  # round-trips

    def test_rewrite_home_off_keeps_home_but_rewrites_context_dir(self):
        doc = self._doc(self.A_HOME, self.A_ROOT)
        maps = self._maps(self.A_HOME, self.A_ROOT, rewrite_home=False)
        canon, _ = canonicalize_jsonl_m(doc, maps)
        self.assertIn(SENTINEL_PROJECT_ROOT, canon)
        self.assertIn(SENTINEL_CONTEXT_DIR, canon)       # context dir still exact
        self.assertNotIn(SENTINEL_HOME, canon)
        self.assertIn(f"{self.A_HOME}/.cargo/bin/cargo", canon)  # home left as-is
        self.assertEqual(localize_jsonl_m(canon, maps)[0], doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
