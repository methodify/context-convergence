"""Regression: a Windows drive root must not infinite-loop `_ancestors`.

`os.path.dirname("C:\\") == "C:\\"` (a fixpoint), so the old `while path != "/"`
loop appended `C:\\` forever and ate all RAM. We can't run native ntpath on a
POSIX test box, so patch `os.path.dirname` to ntpath's to reproduce it.
"""

from __future__ import annotations

import ntpath
import unittest
from unittest import mock

from convergence import pathmap
from convergence.roster import Participant


WIN_ROOT = "C:\\LocalData\\projects\\submatrix-rust"
WIN_HOME = "C:\\Users\\BryonWilliams"
ENCODED = "C--LocalData-projects-submatrix-rust"


def _win_participant():
    return Participant(machine_id="win", os="windows", home=WIN_HOME,
                       project_root=WIN_ROOT)


class WindowsDialectTest(unittest.TestCase):
    """A Rust/Windows project's transcripts mostly hold the project root in
    cargo's `C:/` and git-bash's `/c/` dialects, not Claude Code's native `C:\\`.
    Canonicalize must catch all three (and any case)."""

    def setUp(self):
        self.maps = pathmap.build_mappings(WIN_HOME, WIN_ROOT, ENCODED)
        self.sep = "\\"

    def _canon(self, s):
        return pathmap.canonicalize_value(s, self.maps, self.sep)[0]

    def test_all_root_dialects_canonicalize(self):
        for raw in (
            r'cd "C:\LocalData\projects\submatrix-rust\crates" && cargo build',
            r'cd "C:/LocalData/projects/submatrix-rust/crates" && cargo build',
            r'cd "/c/LocalData/projects/submatrix-rust/crates" && cargo build',
            r'see c:\localdata\projects\submatrix-rust\Cargo.toml',   # lowercase
        ):
            out = self._canon(raw)
            self.assertIn(pathmap.SENTINEL_PROJECT_ROOT, out, raw)
            self.assertNotIn("LocalData", out, raw)      # no residue
            self.assertNotIn("/c/", out, raw)

    def test_home_dialects_canonicalize(self):
        for raw in (
            r'load C:\Users\BryonWilliams\.cargo\registry\foo',
            r'load C:/Users/BryonWilliams/.cargo/registry/foo',
            r'load /c/Users/BryonWilliams/.cargo/registry/foo',
        ):
            out = self._canon(raw)
            self.assertIn(pathmap.SENTINEL_HOME, out, raw)
            self.assertNotIn("BryonWilliams", out, raw)

    def test_sibling_project_is_left_alone(self):
        # The sibling Python project 'submatrix' (no -rust) has no portable
        # target — must NOT be mangled by the root anchor.
        raw = r'engine at C:/LocalData/projects/submatrix/tmp/colmap.exe'
        out = self._canon(raw)
        self.assertIn("submatrix/tmp", out)
        self.assertNotIn(pathmap.SENTINEL_PROJECT_ROOT, out)

    def test_canonical_stability_through_windows_roundtrip(self):
        # The push guard's property: canonicalize(localize(canon)) == canon, even
        # though localize collapses every dialect to the one native form.
        p = _win_participant()
        for raw in (
            r'cargo built C:/LocalData/projects/submatrix-rust/target/x',
            r'/c/Users/BryonWilliams/.submatrix/jobs/run',
        ):
            canon = pathmap.canonicalize_value(raw, self.maps, self.sep)[0]
            back = pathmap.localize_value(canon, self.maps, self.sep)[0]
            recanon = pathmap.canonicalize_value(back, self.maps, self.sep)[0]
            self.assertEqual(recanon, canon, raw)

    def test_posix_paths_unaffected_by_windows_logic(self):
        # A POSIX participant must still match exactly/case-sensitively.
        maps = pathmap.build_mappings("/Users/bob", "/Users/bob/src/proj",
                                      "-Users-bob-src-proj")
        out = pathmap.canonicalize_value("/Users/bob/src/proj/main.py", maps, "/")[0]
        self.assertIn(pathmap.SENTINEL_PROJECT_ROOT, out)
        # case must NOT fold on POSIX
        out2 = pathmap.canonicalize_value("/users/BOB/src/proj/main.py", maps, "/")[0]
        self.assertNotIn(pathmap.SENTINEL_PROJECT_ROOT, out2)


class WindowsAncestorTest(unittest.TestCase):
    def test_drive_root_terminates(self):
        win = "C:\\LocalData\\projects\\submatrix-rust"
        with mock.patch("convergence.pathmap.os.path.dirname", ntpath.dirname):
            anc = pathmap._ancestors(win)
        self.assertEqual(anc[0], win)
        self.assertEqual(anc[-1], "C:\\")          # terminated at the drive root
        self.assertLess(len(anc), 12)              # did NOT loop forever

    def test_infer_root_from_windows_cwd(self):
        # Claude Code encodes C:\LocalData\projects\submatrix-rust the same way
        # we do ([^A-Za-z0-9] -> -), so inference recovers the backslash root.
        encoded = "C--LocalData-projects-submatrix-rust"
        with mock.patch("convergence.pathmap.os.path.dirname", ntpath.dirname):
            root = pathmap.infer_project_root(
                encoded, ["C:\\LocalData\\projects\\submatrix-rust"])
        self.assertEqual(root, "C:\\LocalData\\projects\\submatrix-rust")

    def test_posix_ancestors_still_terminate(self):
        self.assertEqual(pathmap._ancestors("/")[-1], "/")
        anc = pathmap._ancestors("/Users/x/src/proj")
        self.assertEqual(anc[0], "/Users/x/src/proj")
        self.assertIn("/", anc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
