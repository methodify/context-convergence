"""Localize a local cluster-shaped dir (roster.json + context/) straight into
~/.claude/projects, bypassing git/GitHub entirely. For sneakernet: copy machine
A's ~/.convergence/clones/<id>/ contents to this machine and point this at them.

    python3 converge_import.py <source_dir>

Reuses the tested localizer (multi-tier v2 + skip-identical) and backs up the
local context dir first."""
import os, sys, types
from convergence import engine, env
from convergence.cluster import Cluster
from convergence.pathmap import encode_project_dir

src = sys.argv[1]
cl = Cluster(src)
roster = cl.load_roster()
p = roster.get(env.machine_id())
if not p:
    sys.exit(f"this machine ({env.machine_id()}) is not in {src}/roster.json; "
             f"participants present: {[x.machine_id for x in roster.participants]}")
encoded = encode_project_dir(p.project_root)
bk = engine._backup_local_context(encoded)
print("backup:", bk)
tp = types.SimpleNamespace(cluster=cl)
n, subs = engine._localize_into_local(tp, p, encoded, roster.rewrite_home)
print(f"localized {n} file(s) ({subs} path substitutions) -> {engine._local_context_dir(encoded)}")
