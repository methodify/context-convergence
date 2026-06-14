"""M2: memory 3-way merge + transcript divergence detection."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from convergence import engine, env, gitutil, merge
from convergence.cluster import Cluster
from convergence.pathmap import encode_project_dir


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# unit: the merge primitives
# --------------------------------------------------------------------------- #
@unittest.skipUnless(_git_available(), "git not available")
class MergePrimitiveTest(unittest.TestCase):
    def test_non_overlapping_edits_merge_clean(self):
        base = "l1\nl2\nl3\nl4\nl5\n"
        ours = "EDIT1\nl2\nl3\nl4\nl5\n"      # changed line 1
        theirs = "l1\nl2\nl3\nl4\nEDIT5\n"    # changed line 5
        merged, n = merge.three_way_merge(base, ours, theirs)
        self.assertEqual(n, 0)
        self.assertIn("EDIT1", merged)
        self.assertIn("EDIT5", merged)
        self.assertNotIn("<<<<<<<", merged)

    def test_overlapping_edits_conflict(self):
        base = "item: TODO\n"
        ours = "item: DONE\n"
        theirs = "item: WONTFIX\n"
        merged, n = merge.three_way_merge(base, ours, theirs)
        self.assertGreaterEqual(n, 1)
        self.assertIn("<<<<<<<", merged)
        self.assertIn("DONE", merged)
        self.assertIn("WONTFIX", merged)

    def test_union_keeps_both_appends_without_markers(self):
        base = "- a\n- b\n"
        ours = "- a\n- b\n- fromA\n"
        theirs = "- a\n- b\n- fromB\n"
        merged, n = merge.three_way_merge(base, ours, theirs, union=True)
        self.assertEqual(n, 0)
        self.assertIn("fromA", merged)
        self.assertIn("fromB", merged)
        self.assertNotIn("<<<<<<<", merged)

    def test_is_diverged(self):
        self.assertFalse(merge.is_diverged("a\nb\n", "a\nb\n"))         # equal
        self.assertFalse(merge.is_diverged("a\nb\nc\n", "a\nb\n"))      # ours extends
        self.assertFalse(merge.is_diverged("a\nb\n", "a\nb\nc\n"))      # theirs extends
        self.assertTrue(merge.is_diverged("a\nb\nX\n", "a\nb\nY\n"))    # both diverge


# --------------------------------------------------------------------------- #
# end-to-end: two machines over a git remote
# --------------------------------------------------------------------------- #
@unittest.skipUnless(_git_available(), "git not available")
class MemoryConvergenceTest(unittest.TestCase):
    A = "/Users/alice/src/proj"
    B = "/home/bob/work/proj"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        self.remote = os.path.join(self.tmp, "remote.git")
        gitutil.init_bare(self.remote)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _use(self, name, mid, os_name="linux"):
        base = os.path.join(self.tmp, name)
        os.environ.update(
            HOME=os.path.join(base, "home"),
            CLAUDE_PROJECTS_DIR=os.path.join(base, "cc"),
            CONVERGENCE_HOME=os.path.join(base, "conv"),
            CONVERGENCE_MACHINE_ID=mid, CONVERGENCE_OS=os_name,
            CONVERGENCE_NOW="2026-06-14T00:00:00Z",
        )
        os.environ.pop("CONVERGENCE_REMOTE", None)

    def _ctx(self, root):
        return os.path.join(env.claude_projects_dir(), encode_project_dir(root))

    def _seed(self, root, *, sess="s.jsonl", mem=None):
        d = self._ctx(root)
        os.makedirs(os.path.join(d, "memory"), exist_ok=True)
        with open(os.path.join(d, sess), "w") as fh:
            fh.write(json.dumps({"cwd": root}) + "\n")
        if mem:
            for name, body in mem.items():
                with open(os.path.join(d, "memory", name), "w") as fh:
                    fh.write(body)

    def _write_mem(self, root, name, body):
        with open(os.path.join(self._ctx(root), "memory", name), "w") as fh:
            fh.write(body)

    def _read_mem(self, root, name):
        with open(os.path.join(self._ctx(root), "memory", name), encoding="utf-8") as fh:
            return fh.read()

    # ------------------------------------------------------------------ #
    def test_non_overlapping_memory_edits_converge(self):
        backlog = "1: a\n2: b\n3: c\n4: d\n5: e\n"
        self._use("A", "mach-A", "darwin")
        self._seed(self.A, mem={"backlog.md": backlog})
        engine.init(self.A, remote=self.remote)

        self._use("B", "mach-B")
        engine.join(self.B, remote=self.remote)

        # A edits line 1, B edits line 5 — disjoint.
        self._use("A", "mach-A", "darwin")
        self._write_mem(self.A, "backlog.md", "1: AAA\n2: b\n3: c\n4: d\n5: e\n")
        engine.push(project_root=self.A)

        self._use("B", "mach-B")
        self._write_mem(self.B, "backlog.md", "1: a\n2: b\n3: c\n4: d\n5: BBB\n")
        r = engine.sync(project_root=self.B)       # pulls A's edit, merges, pushes
        self.assertEqual(r["conflicts"], [])
        merged = self._read_mem(self.B, "backlog.md")
        self.assertIn("1: AAA", merged)            # A's edit
        self.assertIn("5: BBB", merged)            # B's edit
        self.assertNotIn("<<<<<<<", merged)

        # A syncs and sees the fully merged backlog too.
        self._use("A", "mach-A", "darwin")
        engine.sync(project_root=self.A)
        a_merged = self._read_mem(self.A, "backlog.md")
        self.assertIn("1: AAA", a_merged)
        self.assertIn("5: BBB", a_merged)

    def test_overlapping_memory_edit_surfaces_conflict(self):
        self._use("A", "mach-A", "darwin")
        self._seed(self.A, mem={"note.md": "status: TODO\n"})
        engine.init(self.A, remote=self.remote)
        self._use("B", "mach-B")
        engine.join(self.B, remote=self.remote)

        self._use("A", "mach-A", "darwin")
        self._write_mem(self.A, "note.md", "status: DONE\n")
        engine.push(project_root=self.A)

        self._use("B", "mach-B")
        self._write_mem(self.B, "note.md", "status: WONTFIX\n")
        r = engine.push(project_root=self.B)       # same line edited both -> conflict
        kinds = [c["kind"] for c in r["conflicts"]]
        self.assertIn("memory-conflict", kinds)

    def test_memory_index_unions_without_conflict(self):
        self._use("A", "mach-A", "darwin")
        self._seed(self.A, mem={"MEMORY.md": "# index\n- [base](b.md)\n"})
        engine.init(self.A, remote=self.remote)
        self._use("B", "mach-B")
        engine.join(self.B, remote=self.remote)

        self._use("A", "mach-A", "darwin")
        self._write_mem(self.A, "MEMORY.md", "# index\n- [base](b.md)\n- [a](a.md)\n")
        engine.push(project_root=self.A)

        self._use("B", "mach-B")
        self._write_mem(self.B, "MEMORY.md", "# index\n- [base](b.md)\n- [b](b2.md)\n")
        r = engine.sync(project_root=self.B)       # both appended a bullet
        self.assertEqual(r["conflicts"], [])       # index unions, no conflict
        idx = self._read_mem(self.B, "MEMORY.md")
        self.assertIn("[a](a.md)", idx)
        self.assertIn("[b](b2.md)", idx)
        self.assertNotIn("<<<<<<<", idx)

    def test_session_divergence_detected_and_not_concatenated(self):
        self._use("A", "mach-A", "darwin")
        self._seed(self.A, sess="X.jsonl")
        engine.init(self.A, remote=self.remote)
        self._use("B", "mach-B")
        engine.join(self.B, remote=self.remote)

        # Both grow the SAME session X with different records.
        self._use("A", "mach-A", "darwin")
        with open(os.path.join(self._ctx(self.A), "X.jsonl"), "a") as fh:
            fh.write(json.dumps({"cwd": self.A, "turn": "A-only"}) + "\n")
        engine.push(project_root=self.A)

        self._use("B", "mach-B")
        with open(os.path.join(self._ctx(self.B), "X.jsonl"), "a") as fh:
            fh.write(json.dumps({"cwd": self.B, "turn": "B-only"}) + "\n")
        r = engine.push(project_root=self.B)
        self.assertIn("session-divergence", [c["kind"] for c in r["conflicts"]])
        # Cluster kept A's lineage; B's record was NOT concatenated into it.
        canon = Cluster(env.clone_dir("proj")).read_context("X.jsonl")
        self.assertIn("A-only", canon)
        self.assertNotIn("B-only", canon)


if __name__ == "__main__":
    unittest.main(verbosity=2)
