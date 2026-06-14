"""Sprint 4: the Stop-hook seamless layer (design §5.1)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from convergence import engine, env, hooks
from convergence.pathmap import encode_project_dir

ROOT = "/Users/tester/src/demo"


class HookInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        self.settings = os.path.join(self.tmp, "settings.json")
        os.environ.update(CLAUDE_SETTINGS_PATH=self.settings,
                          CONVERGENCE_HOME=os.path.join(self.tmp, "conv"))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self):
        with open(self.settings) as fh:
            return json.load(fh)

    def test_install_is_idempotent(self):
        r1 = hooks.install()
        self.assertTrue(r1["changed"])
        r2 = hooks.install()
        self.assertFalse(r2["changed"])
        entries = self._load()["hooks"]["Stop"]
        cmds = [h["command"] for e in entries for h in e["hooks"]]
        self.assertEqual(sum(hooks.MARKER in c for c in cmds), 1)  # exactly one

    def test_install_preserves_existing_hooks(self):
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": "echo keep-me"}]}]}}, fh)
        hooks.install()
        cmds = [h["command"] for e in self._load()["hooks"]["Stop"] for h in e["hooks"]]
        self.assertIn("echo keep-me", cmds)
        self.assertTrue(any(hooks.MARKER in c for c in cmds))

    def test_status_and_uninstall(self):
        hooks.install()
        self.assertTrue(hooks.status()["installed"]["Stop"])
        r = hooks.uninstall()
        self.assertEqual(r["removed"], 1)
        self.assertFalse(hooks.status()["installed"]["Stop"])

    def test_uninstall_leaves_unrelated_hooks(self):
        with open(self.settings, "w") as fh:
            json.dump({"hooks": {"Stop": [
                {"hooks": [{"type": "command", "command": "echo keep-me"}]}]}}, fh)
        hooks.install()
        hooks.uninstall()
        cmds = [h["command"] for e in self._load()["hooks"]["Stop"] for h in e["hooks"]]
        self.assertEqual(cmds, ["echo keep-me"])


class HookSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        os.environ.update(
            CLAUDE_PROJECTS_DIR=os.path.join(self.tmp, "claude", "projects"),
            CONVERGENCE_HOME=os.path.join(self.tmp, "conv"),
            CONVERGENCE_MACHINE_ID="machine-A",
            CONVERGENCE_NOW="2026-06-14T00:00:00Z",
        )

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hook_sync_on_non_project_is_silent_and_safe(self):
        # cwd is not a convergence project — must return 0 and not raise.
        rc = hooks.hook_sync(project_root="/Users/tester/src/never-inited")
        self.assertEqual(rc, 0)

    def test_hook_sync_syncs_a_real_project(self):
        d = os.path.join(env.claude_projects_dir(), encode_project_dir(ROOT))
        os.makedirs(d)
        with open(os.path.join(d, "sess.jsonl"), "w") as fh:
            fh.write(json.dumps({"cwd": ROOT}) + "\n")
        engine.init(ROOT, cluster=os.path.join(self.tmp, "cluster"))

        rc = hooks.hook_sync(project_root=ROOT)
        self.assertEqual(rc, 0)
        with open(env.hook_log_path(), encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("sync demo", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
