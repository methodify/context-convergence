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
