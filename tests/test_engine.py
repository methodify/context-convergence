"""Sprint 1: init / push / pull / status against a local cluster.

Fully sandboxed — CLAUDE_PROJECTS_DIR, CONVERGENCE_HOME, CONVERGENCE_MACHINE_ID
and CONVERGENCE_NOW are redirected to a temp dir, so these never read or write
the real ~/.claude/projects.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from convergence import engine, env
from convergence.cluster import Cluster
from convergence.localstate import LocalState
from convergence.pathmap import encode_project_dir


def _slurp(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()

ROOT = "/Users/tester/src/demo"


def _record(root):
    return {
        "cwd": root,
        "message": {"content": [{"input": {"file_path": f"{root}/a.py",
                                            "command": f"cd {root} && ls"}}]},
        "toolUseResult": {"filePath": f"{root}/a.py",
                          "stdout": f"built {root}/a.py\nok\n",
                          "home_ref": "/Users/tester/.claude/projects/x"},
    }


def _jsonl(*records):
    return "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for r in records)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        os.environ.update(
            CLAUDE_PROJECTS_DIR=os.path.join(self.tmp, "claude", "projects"),
            CONVERGENCE_HOME=os.path.join(self.tmp, "conv"),
            CONVERGENCE_MACHINE_ID="machine-A",
            CONVERGENCE_NOW="2026-06-13T12:00:00Z",
        )
        self.cluster_dir = os.path.join(self.tmp, "cluster")
        self.encoded = encode_project_dir(ROOT)
        self.ctx_dir = os.path.join(env.claude_projects_dir(), self.encoded)
        os.makedirs(self.ctx_dir)
        self.original = _jsonl(_record(ROOT), _record(ROOT))
        with open(os.path.join(self.ctx_dir, "sess.jsonl"), "w") as fh:
            fh.write(self.original)
        # a sidecar that must NOT be synced in Sprint 1 (jsonl only)
        os.makedirs(os.path.join(self.ctx_dir, "memory"))
        with open(os.path.join(self.ctx_dir, "memory", "note.md"), "w") as fh:
            fh.write("ignore me")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- init ------------------------------------------------------------- #
    def test_init_creates_cluster_roster_and_canonical_context(self):
        r = engine.init(ROOT, cluster_root=self.cluster_dir)
        self.assertEqual(r["project_id"], "demo")
        self.assertEqual(r["files"], 1)
        self.assertGreater(r["substitutions"], 0)

        cluster = Cluster(self.cluster_dir)
        self.assertTrue(cluster.has_project("demo"))
        canon = _slurp(cluster.context_files("demo")[0])
        self.assertNotIn(ROOT, canon)               # canonicalized
        self.assertIn("{{CC_PROJECT_ROOT}}", canon)
        self.assertIn("/Users/tester/.claude/projects/x", canon)  # home ref untouched

        roster = cluster.load_roster("demo")
        self.assertEqual(len(roster.participants), 1)
        p = roster.participants[0]
        self.assertEqual((p.machine_id, p.project_root, p.encoded_dir),
                         ("machine-A", ROOT, self.encoded))

        st = LocalState.load("demo")
        self.assertEqual(st.project_root, ROOT)
        self.assertEqual(st.cluster_root, os.path.abspath(self.cluster_dir))

    def test_init_only_syncs_jsonl(self):
        engine.init(ROOT, cluster_root=self.cluster_dir)
        names = [os.path.basename(f) for f in Cluster(self.cluster_dir).context_files("demo")]
        self.assertEqual(names, ["sess.jsonl"])  # memory/note.md excluded

    def test_init_refuses_without_context(self):
        with self.assertRaises(engine.ConvergenceError):
            engine.init("/Users/tester/src/nonexistent", cluster_root=self.cluster_dir)

    def test_init_refuses_duplicate(self):
        engine.init(ROOT, cluster_root=self.cluster_dir)
        with self.assertRaises(engine.ConvergenceError):
            engine.init(ROOT, cluster_root=self.cluster_dir)

    def test_project_id_override(self):
        engine.init(ROOT, cluster_root=self.cluster_dir, project_id="acme-demo")
        self.assertTrue(Cluster(self.cluster_dir).has_project("acme-demo"))
        self.assertIsNotNone(LocalState.load("acme-demo"))

    # -- roundtrip -------------------------------------------------------- #
    def test_pull_restores_local_byte_identical(self):
        engine.init(ROOT, cluster_root=self.cluster_dir)
        # Wipe local, then pull from cluster: single machine -> identity.
        os.remove(os.path.join(self.ctx_dir, "sess.jsonl"))
        r = engine.pull(project_id="demo")
        self.assertEqual(r["files"], 1)
        restored = _slurp(os.path.join(self.ctx_dir, "sess.jsonl"))
        self.assertEqual(restored, self.original)

    def test_pull_backs_up_existing_local(self):
        engine.init(ROOT, cluster_root=self.cluster_dir)
        r = engine.pull(project_id="demo")
        self.assertIsNotNone(r["backup"])
        backed = _slurp(os.path.join(r["backup"], "sess.jsonl"))
        self.assertEqual(backed, self.original)

    def test_push_reflects_local_changes(self):
        engine.init(ROOT, cluster_root=self.cluster_dir)
        # Append a new record locally, then push.
        with open(os.path.join(self.ctx_dir, "sess.jsonl"), "a") as fh:
            fh.write(_jsonl(_record(ROOT)))
        engine.push(project_root=ROOT)
        canon = _slurp(Cluster(self.cluster_dir).context_files("demo")[0])
        self.assertEqual(canon.count("\n"), 3)        # 2 original + 1 appended
        self.assertNotIn(ROOT, canon)
        # last_converged advanced on roster + local state.
        self.assertEqual(LocalState.load("demo").last_converged, "2026-06-13T12:00:00Z")

    # -- status ----------------------------------------------------------- #
    def test_status_clean_then_dirty(self):
        engine.init(ROOT, cluster_root=self.cluster_dir)
        s = engine.status(project_id="demo")
        self.assertEqual(s["dirty"], [])
        self.assertEqual(s["behind"], [])
        self.assertEqual(s["local_count"], 1)
        with open(os.path.join(self.ctx_dir, "new.jsonl"), "w") as fh:
            fh.write(_jsonl(_record(ROOT)))
        s = engine.status(project_id="demo")
        self.assertEqual(s["dirty"], ["new.jsonl"])

    def test_resolve_by_root_when_no_project_id(self):
        engine.init(ROOT, cluster_root=self.cluster_dir)
        # push with project_root (not id) must resolve via local state match.
        engine.push(project_root=ROOT)
        self.assertEqual(engine.status(project_root=ROOT)["project_id"], "demo")

    def test_push_unknown_project_fails_loud(self):
        with self.assertRaises(engine.ConvergenceError):
            engine.push(project_root="/Users/tester/src/never-inited")


if __name__ == "__main__":
    unittest.main(verbosity=2)
