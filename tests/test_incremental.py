"""Stage 1 incremental sync: only touch what changed, safely."""

from __future__ import annotations

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

ROOT = "/Users/tester/src/demo"


def _git_ok():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _jsonl(*recs):
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs)


class LocalIncrementalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        os.environ.update(
            CLAUDE_PROJECTS_DIR=os.path.join(self.tmp, "claude", "projects"),
            CONVERGENCE_HOME=os.path.join(self.tmp, "conv"),
            CONVERGENCE_MACHINE_ID="machine-A",
        )
        self.cluster = os.path.join(self.tmp, "cluster")
        self.ctx = os.path.join(env.claude_projects_dir(), encode_project_dir(ROOT))
        os.makedirs(os.path.join(self.ctx, "memory"))
        self.sess = os.path.join(self.ctx, "sess.jsonl")
        with open(self.sess, "w") as fh:
            fh.write(_jsonl({"cwd": ROOT}))
        with open(os.path.join(self.ctx, "memory", "MEMORY.md"), "w") as fh:
            fh.write("# index\n")
        engine.init(ROOT, cluster=self.cluster)            # processes both, stores fingerprints

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unchanged_push_does_no_work(self):
        r = engine.push(project_id="demo")
        self.assertEqual(r["files"], 0)        # nothing reprocessed
        self.assertEqual(r["skipped"], 2)      # both skipped

    def test_only_changed_file_is_processed(self):
        with open(self.sess, "a") as fh:       # append -> size+mtime change
            fh.write(_jsonl({"cwd": ROOT, "turn": "more"}))
        r = engine.push(project_id="demo")
        self.assertEqual(r["files"], 1)        # only sess.jsonl
        self.assertEqual(r["skipped"], 1)      # MEMORY.md skipped

    def test_full_flag_reprocesses_everything(self):
        r = engine.push(project_id="demo", full=True)
        self.assertEqual(r["files"], 2)
        self.assertEqual(r["skipped"], 0)

    def test_canon_version_bump_forces_full(self):
        st = LocalState.load("demo")
        st.canon_version = (st.canon_version or 0) - 1   # simulate an upgrade
        st.save()
        r = engine.push(project_id="demo")
        self.assertEqual(r["files"], 2)        # all reprocessed despite matching fingerprints
        self.assertEqual(r["skipped"], 0)

    def test_windows_backslash_fingerprints_migrate_and_skip(self):
        # Simulate state written by an older Windows build: the memory relpath
        # key uses `\`. After the relpath fix entries are forward-slash, so a
        # naive lookup misses and the (unchanged) memory file looks "changed".
        # load() must migrate the key so it still skips.
        st = LocalState.load("demo")
        fps = dict(st.file_fingerprints)
        fps["memory\\MEMORY.md"] = fps.pop("memory/MEMORY.md")
        st.file_fingerprints = fps
        st.save()
        r = engine.push(project_id="demo")
        self.assertEqual(r["files"], 0)        # nothing reprocessed — migration worked
        self.assertEqual(r["skipped"], 2)

    def test_sync_converges_no_self_push_loop(self):
        # Editing a file then syncing must converge: a SECOND sync re-pushes and
        # re-pulls nothing. Regression — pull localized the files this machine had
        # just pushed, churning their mtime, so every sync re-pushed them forever.
        with open(self.sess, "a") as fh:
            fh.write(_jsonl({"turn": "more"}))
        r1 = engine.sync_full(project_id="demo")
        self.assertEqual(r1["pushed"], 1)
        r2 = engine.sync_full(project_id="demo")
        self.assertEqual(r2["pushed"], 0)   # converged: no self-push loop
        self.assertEqual(r2["pulled"], 0)

    def test_exists_guard_reprocesses_when_cluster_file_missing(self):
        # Fingerprint matches, but the cluster lost the file (wipe/re-clone) — must
        # not skip, or the file would be missing from the cluster forever.
        os.remove(os.path.join(Cluster(self.cluster).context_dir, "sess.jsonl"))
        r = engine.push(project_id="demo")
        self.assertGreaterEqual(r["files"], 1)
        self.assertIsNotNone(Cluster(self.cluster).read_context("sess.jsonl"))  # restored


@unittest.skipUnless(_git_ok(), "git not available")
class GitIncrementalTest(unittest.TestCase):
    A, B = "/Users/alice/src/proj", "/home/bob/work/proj"

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
            CONVERGENCE_MACHINE_ID=mid, CONVERGENCE_NOW="2026-06-14T00:00:00Z",
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
        import glob
        return sorted(os.path.basename(f) for f in glob.glob(os.path.join(self._ctx(root), "*.jsonl")))

    def test_pull_localizes_only_changed_files(self):
        self._use("A", "mach-A")
        self._seed(self.A, "sessA.jsonl")
        engine.init(self.A, remote=self.remote)
        self._use("B", "mach-B")
        engine.join(self.B, remote=self.remote)            # full localize, baseline set

        # A adds one new session and pushes.
        self._use("A", "mach-A")
        self._seed(self.A, "sessNEW.jsonl")
        engine.push(project_root=self.A)

        # B pulls — should localize ONLY the new file, not re-localize sessA.
        self._use("B", "mach-B")
        r = engine.pull(project_root=self.B)
        self.assertEqual(r["files"], 1)                    # incremental: just sessNEW
        self.assertIn("sessNEW.jsonl", self._names(self.B))

    def test_repeated_sync_over_git_is_a_noop(self):
        # The user's exact scenario: one machine, real remote. After a sync that
        # pushes work, the next sync must do nothing — pull must not re-localize
        # (and thus re-dirty) the files this machine just pushed.
        self._use("A", "mach-A")
        self._seed(self.A, "sessA.jsonl")
        engine.init(self.A, remote=self.remote)
        self._seed(self.A, "sessNEW.jsonl")
        r1 = engine.sync(project_root=self.A)
        self.assertEqual(r1["pushed"], 1)
        r2 = engine.sync(project_root=self.A)
        self.assertEqual(r2["pushed"], 0)
        self.assertEqual(r2["pulled"], 0)
        # And dry-run now agrees there's nothing to do.
        r3 = engine.sync_full(project_root=self.A, dry_run=True)
        self.assertEqual(r3["push_changed"], [])

    def test_full_convergence_loop_with_incremental(self):
        # The separate pull-baseline must be right, or B's sessions never reach A.
        self._use("A", "mach-A")
        self._seed(self.A, "sessA.jsonl")
        engine.init(self.A, remote=self.remote)
        self._use("B", "mach-B")
        engine.join(self.B, remote=self.remote)
        self._seed(self.B, "sessB.jsonl")
        engine.sync(project_root=self.B)                   # B publishes sessB

        self._use("A", "mach-A")
        engine.sync(project_root=self.A)                   # A must receive sessB
        self.assertIn("sessB.jsonl", self._names(self.A))


if __name__ == "__main__":
    unittest.main(verbosity=2)
