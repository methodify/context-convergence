"""--dry-run: accurate plan, ZERO changes to local context / remote / state."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from convergence import engine, env, gitutil
from convergence.cluster import Cluster
from convergence.localstate import LocalState
from convergence.pathmap import encode_project_dir

A, B = "/Users/alice/src/proj", "/home/bob/work/proj"


def _git_ok():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _slurp(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _jsonl(*recs):
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs)


@unittest.skipUnless(_git_ok(), "git not available")
class DryRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        self.remote = os.path.join(self.tmp, "remote.git")
        gitutil.init_bare(self.remote)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _use(self, name, mid):
        base = os.path.join(self.tmp, name)
        os.environ.update(
            HOME=os.path.join(base, "home"),
            CLAUDE_PROJECTS_DIR=os.path.join(base, "cc"),
            CONVERGENCE_HOME=os.path.join(base, "conv"),
            CONVERGENCE_MACHINE_ID=mid, CONVERGENCE_NOW="2026-06-15T00:00:00Z",
        )
        os.environ.pop("CONVERGENCE_REMOTE", None)

    def _ctx(self, root):
        return os.path.join(env.claude_projects_dir(), encode_project_dir(root))

    def _seed(self, root, name, rec=None):
        d = self._ctx(root)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as fh:
            fh.write(_jsonl(rec or {"cwd": root}))

    def _names(self, root):
        return sorted(os.path.basename(f) for f in glob.glob(os.path.join(self._ctx(root), "*.jsonl")))

    # ------------------------------------------------------------------ #
    def test_push_dry_run_plans_but_changes_nothing(self):
        self._use("A", "mach-A")
        self._seed(A, "sessA.jsonl")
        engine.init(A, remote=self.remote)
        self._seed(A, "sessNEW.jsonl")           # one new local file

        remote_before = gitutil._git(["rev-parse", "HEAD"],
                                     cwd=env.clone_dir("proj")).stdout
        st_before = _slurp(LocalState.path_for("proj"))

        r = engine.push(project_root=A, dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["files"], 1)          # sessNEW would be pushed
        self.assertEqual(r["changed"], ["sessNEW.jsonl"])

        # Nothing actually changed: remote HEAD, cluster contents, local state.
        self.assertEqual(gitutil._git(["rev-parse", "HEAD"], cwd=env.clone_dir("proj")).stdout,
                         remote_before)
        self.assertIsNone(Cluster(env.clone_dir("proj")).read_context("sessNEW.jsonl"))
        self.assertEqual(_slurp(LocalState.path_for("proj")), st_before)

        # And a real push afterwards still does the work (dry run didn't consume it).
        r2 = engine.push(project_root=A)
        self.assertEqual(r2["files"], 1)

    def test_pull_dry_run_reports_incoming_without_writing(self):
        self._use("A", "mach-A")
        self._seed(A, "sessA.jsonl")
        engine.init(A, remote=self.remote)
        self._use("B", "mach-B")
        engine.join(B, remote=self.remote)

        self._use("A", "mach-A")
        self._seed(A, "fromA.jsonl")
        engine.push(project_root=A)

        self._use("B", "mach-B")
        before = self._names(B)
        r = engine.pull(project_root=B, dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertIn("fromA.jsonl", r["new"])   # would arrive
        self.assertEqual(self._names(B), before)  # but local dir is untouched
        self.assertNotIn("fromA.jsonl", self._names(B))

    def test_sync_dry_run_changes_nothing_both_sides(self):
        self._use("A", "mach-A")
        self._seed(A, "sessA.jsonl")
        engine.init(A, remote=self.remote)
        self._seed(A, "local-new.jsonl")

        remote_before = gitutil._git(["rev-parse", "HEAD"], cwd=env.clone_dir("proj")).stdout
        local_before = self._names(A)

        r = engine.sync_full(project_root=A, dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertIn("local-new.jsonl", r["push_changed"])

        self.assertEqual(gitutil._git(["rev-parse", "HEAD"], cwd=env.clone_dir("proj")).stdout,
                         remote_before)
        self.assertEqual(self._names(A), local_before)

    def test_dry_run_surfaces_divergence_without_pushing_it(self):
        self._use("A", "mach-A")
        self._seed(A, "X.jsonl")
        engine.init(A, remote=self.remote)
        self._use("B", "mach-B")
        engine.join(B, remote=self.remote)

        # A and B both grow X differently.
        self._use("A", "mach-A")
        with open(os.path.join(self._ctx(A), "X.jsonl"), "a") as fh:
            fh.write(_jsonl({"turn": "A"}))
        engine.push(project_root=A)
        self._use("B", "mach-B")
        with open(os.path.join(self._ctx(B), "X.jsonl"), "a") as fh:
            fh.write(_jsonl({"turn": "B"}))

        r = engine.push(project_root=B, dry_run=True)
        self.assertIn("session-divergence", [c["kind"] for c in r["conflicts"]])
        # B did not actually push its divergent X.
        canon = Cluster(env.clone_dir("proj")).read_context("X.jsonl")
        self.assertNotIn("\"turn\":\"B\"", canon)


if __name__ == "__main__":
    unittest.main(verbosity=2)
