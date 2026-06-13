"""convergence CLI.

Sync verbs (Sprint 1 — local cluster dir, single machine):
    python -m convergence init <project_root> --cluster <dir> [--project-id ID]
    python -m convergence push   [project_root] [--project-id ID]
    python -m convergence pull   [project_root] [--project-id ID]
    python -m convergence status [project_root] [--project-id ID]

Low-level path-mapping (Sprint 0 — operate on a context dir directly):
    python -m convergence doctor <context_dir> [--root R] [--home H]
    python -m convergence canonicalize <context_dir> <out_dir> [--root R]
    python -m convergence localize <in_dir> <context_dir> --root R
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

from . import engine
from .doctor import format_report, scan
from .pathmap import DEFAULT_SENTINEL, canonicalize_jsonl, localize_jsonl


def _cmd_init(args) -> int:
    r = engine.init(args.project_root, cluster_root=args.cluster, project_id=args.project_id)
    print(f"init '{r['project_id']}' (machine {r['machine_id']}): "
          f"{r['files']} file(s), {r['substitutions']} path(s) canonicalized")
    print(f"  cluster: {r['cluster']}")
    return 0


def _cmd_push(args) -> int:
    r = engine.push(args.project_root, project_id=args.project_id)
    print(f"push '{r['project_id']}': {r['files']} file(s), "
          f"{r['substitutions']} path(s) canonicalized -> {r['cluster']}")
    return 0


def _cmd_pull(args) -> int:
    r = engine.pull(args.project_root, project_id=args.project_id)
    print(f"pull '{r['project_id']}': {r['files']} file(s), "
          f"{r['substitutions']} path(s) localized -> {r['local_dir']}")
    if r["backup"]:
        print(f"  backup: {r['backup']}")
    return 0


def _cmd_status(args) -> int:
    r = engine.status(args.project_root, project_id=args.project_id)
    print(f"project '{r['project_id']}'  (machine {r['machine_id']})")
    print(f"  root:    {r['project_root']}")
    print(f"  cluster: {r['cluster']}")
    print(f"  last converged: {r['last_converged']}")
    print(f"  local files: {r['local_count']}   cluster files: {r['cluster_count']}")
    print(f"  dirty (push needed):  {', '.join(r['dirty']) or 'none'}")
    print(f"  behind (pull avail.): {', '.join(r['behind']) or 'none'}")
    print(f"  roster ({len(r['participants'])} participant(s)):")
    for mid, osn, root, lc in r["participants"]:
        print(f"    - {mid}  {osn}  {root}  (converged {lc})")
    return 0


def _cmd_doctor(args) -> int:
    rep = scan(args.context_dir, root=args.root, home=args.home, sentinel=args.sentinel)
    print(format_report(rep))
    return 0 if rep.ok else 1


def _transform_dir(src, dst, fn, root, sentinel) -> int:
    os.makedirs(dst, exist_ok=True)
    total = 0
    for f in sorted(glob.glob(os.path.join(src, "*.jsonl"))):
        with open(f, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        out, n = fn(text, root, sentinel)
        total += n
        with open(os.path.join(dst, os.path.basename(f)), "w", encoding="utf-8") as fh:
            fh.write(out)
    return total


def _resolve_root(args) -> str | None:
    if args.root:
        return args.root
    rep = scan(args.context_dir if hasattr(args, "context_dir") else args.src)
    return rep.project_root


def _cmd_canonicalize(args) -> int:
    rep = scan(args.context_dir, root=args.root)
    root = args.root or rep.project_root
    if not root:
        print("error: could not infer project root; pass --root", file=sys.stderr)
        return 2
    n = _transform_dir(args.context_dir, args.out_dir, canonicalize_jsonl, root, args.sentinel)
    print(f"canonicalized {root} -> {args.sentinel}: {n} substitution(s) into {args.out_dir}")
    return 0


def _cmd_localize(args) -> int:
    if not args.root:
        print("error: localize requires --root (the target machine's project root)",
              file=sys.stderr)
        return 2
    n = _transform_dir(args.in_dir, args.context_dir, localize_jsonl, args.root, args.sentinel)
    print(f"localized {args.sentinel} -> {args.root}: {n} substitution(s) into {args.context_dir}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="convergence", description=__doc__)
    p.add_argument("--sentinel", default=DEFAULT_SENTINEL)
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="register a project in the cluster (first machine)")
    i.add_argument("project_root", nargs="?", default=None)
    i.add_argument("--cluster", required=True)
    i.add_argument("--project-id", default=None)
    i.set_defaults(func=_cmd_init)

    for name, fn, help_ in (
        ("push", _cmd_push, "localize->canonicalize local context into the cluster"),
        ("pull", _cmd_pull, "localize cluster context into ~/.claude/projects"),
        ("status", _cmd_status, "show dirty/behind state and roster"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("project_root", nargs="?", default=None)
        sp.add_argument("--project-id", default=None)
        sp.set_defaults(func=fn)

    d = sub.add_parser("doctor", help="scan a context dir and report safety")
    d.add_argument("context_dir")
    d.add_argument("--root", default=None)
    d.add_argument("--home", default=None)
    d.set_defaults(func=_cmd_doctor)

    c = sub.add_parser("canonicalize", help="local form -> canonical form")
    c.add_argument("context_dir")
    c.add_argument("out_dir")
    c.add_argument("--root", default=None)
    c.set_defaults(func=_cmd_canonicalize)

    l = sub.add_parser("localize", help="canonical form -> local form for --root")
    l.add_argument("in_dir")
    l.add_argument("context_dir")
    l.add_argument("--root", required=True)
    l.set_defaults(func=_cmd_localize)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except engine.ConvergenceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
