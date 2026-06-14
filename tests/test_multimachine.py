"""Sprint 2: the second machine. Two simulated machines (distinct HOME, project
root, context dir, machine id) sharing one cluster dir — the local stand-in for
the private git remote that arrives in Sprint 3.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from convergence import engine, env
from convergence.cluster import Cluster
from convergence.pathmap import encode_project_dir

ROOT_A = "/Users/alice/src/demo"
ROOT_B = "/home/bob/work/demo"


def _record(root):
    return {"cwd": root,
            "message": {"content": [{"input": {"file_path": f"{root}/a.py"}}]},
            "toolUseResult": {"filePath": f"{root}/a.py",
                              "stdout": f"made {root}/a.py\nok\n"}}


def _jsonl(*recs):
    return "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for r in recs)


def _slurp(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


class MultiMachineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        self.cluster = os.path.join(self.tmp, "cluster")  # shared between machines

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _use(self, name, machine_id, os_name="linux"):
        """Switch ambient identity to a given simulated machine."""
        base = os.path.join(self.tmp, name)
        os.environ.update(
            HOME=os.path.join(base, "home"),
            CLAUDE_PROJECTS_DIR=os.path.join(base, "claude", "projects"),
            CONVERGENCE_HOME=os.path.join(base, "conv"),
            CONVERGENCE_MACHINE_ID=machine_id,
            CONVERGENCE_OS=os_name,
            CONVERGENCE_NOW="2026-06-13T12:00:00Z",
        )

    def _ctx_dir(self, root):
        return os.path.join(env.claude_projects_dir(), encode_project_dir(root))

    def _seed_local(self, root, text):
        d = self._ctx_dir(root)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "sess.jsonl"), "w") as fh:
            fh.write(text)

    # ------------------------------------------------------------------ #
    def test_join_localizes_onto_second_machine(self):
        self._use("A", "machine-A", os_name="darwin")
        self._seed_local(ROOT_A, _jsonl(_record(ROOT_A), _record(ROOT_A)))
        engine.init(ROOT_A, cluster=self.cluster)

        self._use("B", "machine-B", os_name="linux")
        r = engine.join(ROOT_B, cluster=self.cluster)
        self.assertEqual(r["participants"], 2)

        b_text = _slurp(os.path.join(self._ctx_dir(ROOT_B), "sess.jsonl"))
        self.assertIn(f"{ROOT_B}/a.py", b_text)     # localized to B's paths
        self.assertNotIn(ROOT_A, b_text)            # no machine-A paths leaked
        self.assertNotIn("{{CC_PROJECT_ROOT}}", b_text)

        # Cluster stays machine-neutral; A's local form is untouched.
        canon = _slurp(Cluster(self.cluster).context_files()[0])
        self.assertNotIn(ROOT_A, canon)
        self.assertNotIn(ROOT_B, canon)
        self._use("A", "machine-A")
        self.assertIn(ROOT_A, _slurp(os.path.join(self._ctx_dir(ROOT_A), "sess.jsonl")))

        roster = Cluster(self.cluster).load_roster()
        self.assertEqual({p.machine_id for p in roster.participants}, {"machine-A", "machine-B"})
        self.assertEqual(roster.get("machine-B").project_root, ROOT_B)
        self.assertEqual(roster.get("machine-A").os, "darwin")
        self.assertEqual(roster.get("machine-B").os, "linux")

    def test_full_convergence_loop_B_to_A(self):
        # A creates the project; B joins; B authors NEW context and pushes;
        # A pulls and sees B's work with A's local paths. The headline.
        self._use("A", "machine-A")
        self._seed_local(ROOT_A, _jsonl(_record(ROOT_A)))
        engine.init(ROOT_A, cluster=self.cluster)

        self._use("B", "machine-B")
        engine.join(ROOT_B, cluster=self.cluster)
        with open(os.path.join(self._ctx_dir(ROOT_B), "newsess.jsonl"), "w") as fh:
            fh.write(_jsonl(_record(ROOT_B)))
        engine.push(project_root=ROOT_B)

        self._use("A", "machine-A")
        engine.pull(project_root=ROOT_A)
        a_new = os.path.join(self._ctx_dir(ROOT_A), "newsess.jsonl")
        self.assertTrue(os.path.exists(a_new))      # B's session reached A
        text = _slurp(a_new)
        self.assertIn(f"{ROOT_A}/a.py", text)        # with A's local paths
        self.assertNotIn(ROOT_B, text)               # not B's

    def test_join_unknown_project_fails_loud(self):
        self._use("A", "machine-A")
        self._seed_local(ROOT_A, _jsonl(_record(ROOT_A)))
        engine.init(ROOT_A, cluster=self.cluster)
        self._use("B", "machine-B")
        with self.assertRaises(engine.ConvergenceError):
            engine.join(ROOT_B, cluster=self.cluster, project_id="nope")

    def test_rejoin_same_machine_does_not_duplicate(self):
        self._use("A", "machine-A")
        self._seed_local(ROOT_A, _jsonl(_record(ROOT_A)))
        engine.init(ROOT_A, cluster=self.cluster)
        self._use("B", "machine-B")
        engine.join(ROOT_B, cluster=self.cluster)
        engine.join(ROOT_B, cluster=self.cluster)  # re-join
        roster = Cluster(self.cluster).load_roster()
        self.assertEqual(len(roster.participants), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
