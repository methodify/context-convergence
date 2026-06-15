"""Option B: cross-platform path-separator translation.

Canonical form uses `/` universally inside rewritten paths; localize converts
`/` to the target machine's native separator. POSIX is a no-op; only Windows
participants translate.
"""

from __future__ import annotations

import json
import unittest

from convergence.pathmap import native_sep, normalize_jsonl
from convergence.roster import Participant

WIN = Participant("win", "windows", "C:\\Users\\Bryon", "C:\\LocalData\\projects\\submatrix-rust")
MAC = Participant("mac", "darwin", "/Users/bryon", "/Users/bryon/src/submatrix-rust")


def _doc(root, sep):
    f = sep.join([root, "crates", "cam", "src", "foo.rs"])
    return json.dumps({"cwd": root, "f": f}, separators=(",", ":")) + "\n"


def _f(jsonl_text):
    """The decoded `f` field of a one-line JSONL doc (so backslash assertions
    aren't confused by JSON's `\\` escaping in the serialized text)."""
    return json.loads(jsonl_text.strip())["f"]


class SeparatorTest(unittest.TestCase):
    def test_native_sep(self):
        self.assertEqual(native_sep("windows"), "\\")
        self.assertEqual(native_sep("darwin"), "/")
        self.assertEqual(native_sep("linux"), "/")

    def test_canonical_form_is_os_neutral(self):
        # The same logical content on Windows and on Mac canonicalizes identically.
        canon_w, _ = WIN.canonicalize(_doc("C:\\LocalData\\projects\\submatrix-rust", "\\"))
        canon_m, _ = MAC.canonicalize(_doc("/Users/bryon/src/submatrix-rust", "/"))
        self.assertEqual(canon_w, canon_m)
        self.assertIn("{{CC_PROJECT_ROOT}}/crates/cam/src/foo.rs", canon_w)
        self.assertNotIn("\\", canon_w)            # canonical uses forward slashes only

    def test_windows_to_mac_localizes_to_posix_paths(self):
        canon, _ = WIN.canonicalize(_doc("C:\\LocalData\\projects\\submatrix-rust", "\\"))
        mac_text, _ = MAC.localize(canon)
        self.assertEqual(_f(mac_text),
                         "/Users/bryon/src/submatrix-rust/crates/cam/src/foo.rs")
        self.assertNotIn("\\", mac_text)           # no backslash tails on the Mac at all

    def test_mac_to_windows_localizes_to_backslash_paths(self):
        canon, _ = MAC.canonicalize(_doc("/Users/bryon/src/submatrix-rust", "/"))
        win_text, _ = WIN.localize(canon)
        self.assertEqual(_f(win_text),
                         "C:\\LocalData\\projects\\submatrix-rust\\crates\\cam\\src\\foo.rs")

    def test_windows_same_machine_roundtrip(self):
        doc = _doc("C:\\LocalData\\projects\\submatrix-rust", "\\")
        canon, _ = WIN.canonicalize(doc)
        self.assertEqual(WIN.localize(canon)[0], normalize_jsonl(doc))  # lossless on Windows

    def test_posix_unaffected(self):
        doc = _doc("/Users/bryon/src/submatrix-rust", "/")
        canon, _ = MAC.canonicalize(doc)
        self.assertEqual(MAC.localize(canon)[0], normalize_jsonl(doc))
        # canonical tail is just the forward-slash tail, unchanged.
        self.assertIn("{{CC_PROJECT_ROOT}}/crates/cam/src/foo.rs", canon)


if __name__ == "__main__":
    unittest.main(verbosity=2)
