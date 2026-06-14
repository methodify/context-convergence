"""Default-remote config: name the cluster once, not on every init/join/projects."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from convergence import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        os.environ["CONVERGENCE_HOME"] = os.path.join(self.tmp, "conv")
        os.environ.pop("CONVERGENCE_REMOTE", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unset_by_default(self):
        self.assertIsNone(config.get_default_remote())
        self.assertIsNone(config.resolve_remote(None))

    def test_set_get_clear(self):
        config.set_default_remote("git@h:you/cluster.git")
        self.assertEqual(config.get_default_remote(), "git@h:you/cluster.git")
        config.clear_default_remote()
        self.assertIsNone(config.get_default_remote())

    def test_resolution_precedence(self):
        config.set_default_remote("DEFAULT")
        # explicit beats env beats default
        os.environ["CONVERGENCE_REMOTE"] = "ENVR"
        self.assertEqual(config.resolve_remote("EXPLICIT"), "EXPLICIT")
        self.assertEqual(config.resolve_remote(None), "ENVR")
        os.environ.pop("CONVERGENCE_REMOTE")
        self.assertEqual(config.resolve_remote(None), "DEFAULT")

    def test_isolated_per_convergence_home(self):
        config.set_default_remote("A")
        os.environ["CONVERGENCE_HOME"] = os.path.join(self.tmp, "other")
        self.assertIsNone(config.get_default_remote())  # different machine/home


class CliDefaultRemoteTest(unittest.TestCase):
    """init adopts the first remote as default; later commands omit --remote."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        os.environ.update(
            CLAUDE_PROJECTS_DIR=os.path.join(self.tmp, "claude", "projects"),
            CONVERGENCE_HOME=os.path.join(self.tmp, "conv"),
            CONVERGENCE_MACHINE_ID="machine-A",
            CONVERGENCE_NOW="2026-06-14T00:00:00Z",
        )
        os.environ.pop("CONVERGENCE_REMOTE", None)
        from convergence import gitutil
        self.remote = os.path.join(self.tmp, "cluster.git")
        gitutil.init_bare(self.remote)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, root):
        import json
        from convergence import env
        from convergence.pathmap import encode_project_dir
        d = os.path.join(env.claude_projects_dir(), encode_project_dir(root))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "s.jsonl"), "w") as fh:
            fh.write(json.dumps({"cwd": root}) + "\n")

    def _run(self, argv):
        import contextlib
        import io
        from convergence.__main__ import main
        with contextlib.redirect_stdout(io.StringIO()):
            return main(argv)

    def test_init_adopts_default_then_projects_needs_no_remote(self):
        self._seed("/Users/x/src/alpha")
        self.assertEqual(self._run(["init", "/Users/x/src/alpha", "--remote", self.remote]), 0)
        self.assertEqual(config.get_default_remote(), self.remote)  # adopted

        # A second project can omit --remote entirely.
        self._seed("/Users/x/src/beta")
        self.assertEqual(self._run(["init", "/Users/x/src/beta"]), 0)

        # projects with no --remote uses the default.
        self.assertEqual(self._run(["projects"]), 0)

    def test_init_with_explicit_remote_does_not_clobber_existing_default(self):
        config.set_default_remote("KEEP-ME")
        self._seed("/Users/x/src/alpha")
        self._run(["init", "/Users/x/src/alpha", "--remote", self.remote])  # one-off unique
        self.assertEqual(config.get_default_remote(), "KEEP-ME")  # default untouched


if __name__ == "__main__":
    unittest.main(verbosity=2)
