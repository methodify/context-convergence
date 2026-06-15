"""Sprint 4: secret hygiene scan (design §6.5)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from convergence import engine, env, secrets
from convergence.pathmap import encode_project_dir

ROOT = "/Users/tester/src/demo"


class ScanTextTest(unittest.TestCase):
    def test_detects_common_secrets(self):
        samples = {
            "AWS access key id": "key = AKIAIOSFODNN7EXAMPLE here",
            "GitHub token": "ghp_" + "a" * 36,
            "OpenAI / Anthropic key": "sk-" + "A" * 24,
            "private key block": "-----BEGIN RSA PRIVATE KEY-----",
            "assigned secret": 'password: "hunter2hunter2hunter2"',
        }
        for kind, text in samples.items():
            kinds = {f.kind for f in secrets.scan_text(text)}
            self.assertIn(kind, kinds, f"missed {kind} in {text!r}")

    def test_low_false_positives_on_benign_text(self):
        benign = ('{"cwd":"/Users/tester/src/demo","msg":"refactored the parser, '
                  'all tests pass, see {{CC_PROJECT_ROOT}}/a.py"}')
        self.assertEqual(secrets.scan_text(benign), [])

    def test_redaction_hides_the_value(self):
        f = secrets.scan_text("ghp_" + "b" * 36)[0]
        self.assertNotIn("b" * 36, str(f))
        self.assertIn("…", f.redacted)

    def test_dedupes_repeats(self):
        tok = "ghp_" + "c" * 36
        self.assertEqual(len(secrets.scan_text(f"{tok} ... {tok}")), 1)


class ScanIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = dict(os.environ)
        os.environ.update(
            CLAUDE_PROJECTS_DIR=os.path.join(self.tmp, "claude", "projects"),
            CONVERGENCE_HOME=os.path.join(self.tmp, "conv"),
            CONVERGENCE_MACHINE_ID="machine-A",
            CONVERGENCE_NOW="2026-06-14T00:00:00Z",
        )
        self.cluster = os.path.join(self.tmp, "cluster")
        d = os.path.join(env.claude_projects_dir(), encode_project_dir(ROOT))
        os.makedirs(d)
        rec = {"cwd": ROOT, "secret": "ghp_" + "d" * 36}
        with open(os.path.join(d, "sess.jsonl"), "w") as fh:
            fh.write(json.dumps(rec) + "\n")
        engine.init(ROOT, cluster=self.cluster)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_local_finds_it(self):
        w = engine.scan_local(project_id="demo")
        self.assertIn("sess.jsonl", w)
        self.assertEqual(w["sess.jsonl"][0].kind, "GitHub token")

    def test_push_strict_refuses(self):
        with self.assertRaises(engine.ConvergenceError):
            engine.push(project_id="demo", scan_secrets=True, strict_secrets=True)

    def test_push_warns_but_proceeds_without_strict(self):
        # The scan runs regardless of incremental skipping; nothing here changed
        # since init, so the push proceeds (no exception) and surfaces the warning.
        r = engine.push(project_id="demo", scan_secrets=True)
        self.assertIn("sess.jsonl", r["secret_warnings"])  # surfaced
        self.assertEqual(r["files"] + r["skipped"], 1)     # proceeded, not refused

    def test_push_without_scan_is_silent(self):
        r = engine.push(project_id="demo")
        self.assertEqual(r["secret_warnings"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
