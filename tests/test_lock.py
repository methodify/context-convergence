"""Hardening #4: per-project concurrency lock."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from convergence import engine, env, hooks
from convergence.lock import _lock_path, project_lock
from convergence.pathmap import encode_project_dir

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

ROOT = "/Users/tester/src/demo"


@unittest.skipUnless(fcntl, "flock not available")
class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        os.environ.update(
            CLAUDE_PROJECTS_DIR=os.path.join(self.tmp, "claude", "projects"),
            CONVERGENCE_HOME=os.path.join(self.tmp, "conv"),
            CONVERGENCE_MACHINE_ID="machine-A",
            CONVERGENCE_NOW="2026-06-14T00:00:00Z",
        )
        ctx = os.path.join(env.claude_projects_dir(), encode_project_dir(ROOT))
        os.makedirs(ctx)
        with open(os.path.join(ctx, "s.jsonl"), "w") as fh:
            fh.write(json.dumps({"cwd": ROOT}) + "\n")
        engine.init(ROOT, cluster=os.path.join(self.tmp, "cluster"))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hold(self):
        """Hold the project lock as a separate open file description (flock
        conflicts across descriptions even within one process)."""
        path = _lock_path("demo")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = open(path, "w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        return fd

    def _release(self, fd):
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()

    def test_mutating_op_raises_lock_busy_when_held(self):
        fd = self._hold()
        try:
            with self.assertRaises(engine.LockBusy):
                engine.push(project_id="demo")
            with self.assertRaises(engine.LockBusy):
                engine.pull(project_id="demo")
        finally:
            self._release(fd)

    def test_hook_skips_and_logs_when_busy(self):
        fd = self._hold()
        try:
            self.assertEqual(hooks.hook_sync(project_root=ROOT), 0)  # never breaks the session
            with open(env.hook_log_path(), encoding="utf-8") as fh:
                self.assertIn("skipped", fh.read())
        finally:
            self._release(fd)

    def test_lock_released_after_op(self):
        engine.push(project_id="demo")           # acquires then releases
        fd = open(_lock_path("demo"), "w")        # we can take it now -> it was freed
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # no raise
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            fd.close()

    def test_reentrant_within_process(self):
        with project_lock("demo"):
            with project_lock("demo"):  # nested acquire must not deadlock/raise
                pass

    def test_different_projects_do_not_block_each_other(self):
        fd = self._hold()  # holds "demo"
        try:
            with project_lock("other-project"):  # different id -> independent lock
                pass
        finally:
            self._release(fd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
