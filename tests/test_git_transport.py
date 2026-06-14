"""Sprint 3: git transport. A local bare repo stands in for a private remote
(faithful clone/fetch/push/merge, no network). Two machines clone it
independently and converge through it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from convergence import engine, env, gitutil
from convergence.pathmap import encode_project_dir

ROOT_A = "/Users/alice/src/demo"
ROOT_B = "/home/bob/work/demo"


def _git_available():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _rec(root, name="a.py"):
    return {"cwd": root, "message": {"content": [{"input": {"file_path": f"{root}/{name}"}}]}}


def _jsonl(*recs):
    return "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in recs)


def _slurp(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


@unittest.skipUnless(_git_available(), "git not available")
class GitTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        self.remote = os.path.join(self.tmp, "remote.git")
        gitutil.init_bare(self.remote)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _use(self, name, machine_id, os_name="linux"):
        base = os.path.join(self.tmp, name)
        os.environ.update(
            HOME=os.path.join(base, "home"),
            CLAUDE_PROJECTS_DIR=os.path.join(base, "claude", "projects"),
            CONVERGENCE_HOME=os.path.join(base, "conv"),
            CONVERGENCE_MACHINE_ID=machine_id,
            CONVERGENCE_OS=os_name,
            CONVERGENCE_NOW="2026-06-13T12:00:00Z",
        )
        return os.path.join(base, "cluster")  # this machine's working clone

    def _ctx(self, root):
        return os.path.join(env.claude_projects_dir(), encode_project_dir(root))

    def _seed(self, root, text, name="sess.jsonl"):
        d = self._ctx(root)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as fh:
            fh.write(text)

    def _names(self, root):
        return sorted(os.path.basename(f) for f in
                      __import__("glob").glob(os.path.join(self._ctx(root), "*.jsonl")))

    # ------------------------------------------------------------------ #
    def test_init_to_fresh_remote_then_join(self):
        cl_a = self._use("A", "machine-A", "darwin")
        self._seed(ROOT_A, _jsonl(_rec(ROOT_A), _rec(ROOT_A)))
        engine.init(ROOT_A, cluster=cl_a, remote=self.remote)
        self.assertTrue(gitutil.remote_has_branch(self.remote, "project/demo"))  # branch on remote

        cl_b = self._use("B", "machine-B", "linux")
        r = engine.join(ROOT_B, cluster=cl_b, remote=self.remote)
        self.assertEqual(r["participants"], 2)
        b_text = _slurp(os.path.join(self._ctx(ROOT_B), "sess.jsonl"))
        self.assertIn(f"{ROOT_B}/a.py", b_text)
        self.assertNotIn(ROOT_A, b_text)

    def test_full_sync_loop_over_git(self):
        cl_a = self._use("A", "machine-A", "darwin")
        self._seed(ROOT_A, _jsonl(_rec(ROOT_A)))
        engine.init(ROOT_A, cluster=cl_a, remote=self.remote)

        cl_b = self._use("B", "machine-B", "linux")
        engine.join(ROOT_B, cluster=cl_b, remote=self.remote)
        self._seed(ROOT_B, _jsonl(_rec(ROOT_B, "feature.py")), name="feat.jsonl")
        engine.sync(project_root=ROOT_B)  # pull (noop) then push to remote

        self._use("A", "machine-A", "darwin")
        engine.sync(project_root=ROOT_A)
        feat = os.path.join(self._ctx(ROOT_A), "feat.jsonl")
        self.assertTrue(os.path.exists(feat))
        self.assertIn(f"{ROOT_A}/feature.py", _slurp(feat))   # localized to A
        self.assertNotIn(ROOT_B, _slurp(feat))

    def test_disjoint_sessions_union_across_machines(self):
        cl_a = self._use("A", "machine-A", "darwin")
        self._seed(ROOT_A, _jsonl(_rec(ROOT_A)), name="sessA.jsonl")
        engine.init(ROOT_A, cluster=cl_a, remote=self.remote)

        cl_b = self._use("B", "machine-B", "linux")
        engine.join(ROOT_B, cluster=cl_b, remote=self.remote)

        # A authors a second session and pushes.
        self._use("A", "machine-A", "darwin")
        self._seed(ROOT_A, _jsonl(_rec(ROOT_A)), name="sessA2.jsonl")
        engine.push(project_root=ROOT_A)

        # B authors its own session and pushes (B has not pulled A2 yet).
        self._use("B", "machine-B", "linux")
        self._seed(ROOT_B, _jsonl(_rec(ROOT_B)), name="sessB.jsonl")
        engine.push(project_root=ROOT_B)

        # After both sync, every machine holds the union of all sessions.
        self._use("A", "machine-A", "darwin")
        engine.sync(project_root=ROOT_A)
        self.assertEqual(self._names(ROOT_A), ["sessA.jsonl", "sessA2.jsonl", "sessB.jsonl"])

        self._use("B", "machine-B", "linux")
        engine.sync(project_root=ROOT_B)
        self.assertEqual(self._names(ROOT_B), ["sessA.jsonl", "sessA2.jsonl", "sessB.jsonl"])

    def test_projects_are_isolated_branches(self):
        # The point of the model: many projects in one repo, each its own orphan
        # branch; joining one fetches ONLY that project's history.
        A_ALPHA = "/Users/alice/src/alpha"
        A_BETA = "/Users/alice/src/beta"
        B_ALPHA = "/home/bob/work/alpha"

        self._use("A", "machine-A", "darwin")
        self._seed(A_ALPHA, _jsonl(_rec(A_ALPHA)))
        engine.init(A_ALPHA, remote=self.remote)          # managed clone, branch project/alpha
        self._seed(A_BETA, _jsonl(_rec(A_BETA)))
        engine.init(A_BETA, remote=self.remote)           # branch project/beta

        branches = set(gitutil.remote_branches(self.remote))
        self.assertEqual(branches, {"main", "project/alpha", "project/beta"})
        self.assertEqual(engine.list_projects(self.remote), ["alpha", "beta"])

        # Machine B joins ONLY alpha.
        self._use("B", "machine-B", "linux")
        engine.join(B_ALPHA, remote=self.remote)
        clone = env.clone_dir("alpha")                    # under B's CONVERGENCE_HOME
        refs = gitutil._git(["branch", "-a"], cwd=clone).stdout
        self.assertIn("project/alpha", refs)
        self.assertNotIn("beta", refs)                    # beta never fetched
        self.assertNotEqual(                              # beta's objects unreachable
            gitutil._git(["log", "project/beta"], cwd=clone, check=False).returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self._ctx(B_ALPHA), "sess.jsonl")))

    def test_fresh_cluster_gets_main_readme(self):
        self._use("A", "machine-A", "darwin")
        self._seed(ROOT_A, _jsonl(_rec(ROOT_A)))
        engine.init(ROOT_A, remote=self.remote)
        self.assertIn("main", gitutil.remote_branches(self.remote))
        # main holds the README; the project branch does NOT.
        tmp = os.path.join(self.tmp, "inspect")
        gitutil.clone_single_branch(self.remote, "main", tmp)
        self.assertTrue(os.path.exists(os.path.join(tmp, "README.md")))
        self.assertFalse(os.path.exists(os.path.join(tmp, "roster.json")))

    def test_history_accumulates_on_remote(self):
        cl_a = self._use("A", "machine-A", "darwin")
        self._seed(ROOT_A, _jsonl(_rec(ROOT_A)))
        engine.init(ROOT_A, cluster=cl_a, remote=self.remote)
        self._seed(ROOT_A, _jsonl(_rec(ROOT_A), _rec(ROOT_A, "b.py")))
        engine.push(project_root=ROOT_A)
        log = gitutil._git(["log", "--oneline"], cwd=cl_a).stdout.strip().splitlines()
        self.assertGreaterEqual(len(log), 2)  # init + push commits


if __name__ == "__main__":
    unittest.main(verbosity=2)
