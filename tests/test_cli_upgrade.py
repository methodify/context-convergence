"""`converge upgrade` builds the right forced pip command and changes nothing on
--dry-run."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from convergence import __main__


class UpgradeCliTest(unittest.TestCase):
    def test_dry_run_prints_command_and_runs_nothing(self):
        buf = io.StringIO()
        with mock.patch("convergence.__main__.subprocess.run") as run:
            with redirect_stdout(buf):
                rc = __main__.main(["upgrade", "--dry-run"])
        self.assertEqual(rc, 0)
        run.assert_not_called()                       # dry run shells out to nothing
        out = buf.getvalue()
        self.assertIn("--force-reinstall", out)       # forced (we're unversioned)
        self.assertIn(__main__.UPGRADE_REPO, out)

    def test_real_run_invokes_forced_pip_install(self):
        with mock.patch("convergence.__main__.subprocess.run") as run:
            with redirect_stdout(io.StringIO()):
                rc = __main__.main(["upgrade"])
        self.assertEqual(rc, 0)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[1:4], ["-m", "pip", "install"])
        for flag in ("--upgrade", "--force-reinstall", "--no-deps"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[-1], __main__.UPGRADE_REPO)

    def test_ref_pins_the_url(self):
        with mock.patch("convergence.__main__.subprocess.run") as run:
            with redirect_stdout(io.StringIO()):
                __main__.main(["upgrade", "--ref", "main"])
        self.assertTrue(run.call_args[0][0][-1].endswith(".git@main"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
