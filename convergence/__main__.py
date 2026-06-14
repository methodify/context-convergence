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

from . import engine, hooks
from .doctor import format_report, scan
from .pathmap import DEFAULT_SENTINEL, canonicalize_jsonl, localize_jsonl


def _cmd_init(args) -> int:
    r = engine.init(args.project_root, cluster_root=args.cluster,
                    project_id=args.project_id, remote=args.remote)
    print(f"init '{r['project_id']}' (machine {r['machine_id']}): "
          f"{r['files']} file(s), {r['substitutions']} path(s) canonicalized")
    print(f"  cluster: {r['cluster']}" + (f"  (remote {r['remote']})" if r["remote"] else ""))
    return 0


def _cmd_sync(args) -> int:
    r = engine.sync(args.project_root, project_id=args.project_id)
    print(f"sync '{r['project_id']}': pulled {r['pulled']}, pushed {r['pushed']} file(s)")
    if r["backup"]:
        print(f"  backup: {r['backup']}")
    return 0


def _cmd_join(args) -> int:
    r = engine.join(args.project_root, cluster_root=args.cluster,
                    project_id=args.project_id, remote=args.remote)
    print(f"join '{r['project_id']}' (machine {r['machine_id']}): "
          f"{r['files']} file(s), {r['substitutions']} path(s) localized -> {r['local_dir']}")
    print(f"  roster now has {r['participants']} participant(s)")
    if r["backup"]:
        print(f"  backup: {r['backup']}")
    return 0


def _print_secret_warnings(warnings) -> None:
    if not warnings:
        return
    n = sum(len(v) for v in warnings.values())
    print(f"  ⚠ {n} apparent secret(s) in pushed context:", file=sys.stderr)
    for name, findings in warnings.items():
        for f in findings:
            print(f"      {name}: {f}", file=sys.stderr)


def _cmd_push(args) -> int:
    r = engine.push(args.project_root, project_id=args.project_id,
                    scan_secrets=args.scan_secrets, strict_secrets=args.strict)
    print(f"push '{r['project_id']}': {r['files']} file(s), "
          f"{r['substitutions']} path(s) canonicalized -> {r['cluster']}")
    _print_secret_warnings(r.get("secret_warnings"))
    return 0


def _cmd_scan(args) -> int:
    warnings = engine.scan_local(args.project_root, project_id=args.project_id)
    if not warnings:
        print("scan: no apparent secrets found")
        return 0
    n = sum(len(v) for v in warnings.values())
    print(f"scan: {n} apparent secret(s) across {len(warnings)} file(s):")
    for name, findings in warnings.items():
        for f in findings:
            print(f"  {name}: {f}")
    return 1


def _cmd_hook_sync(args) -> int:
    return hooks.hook_sync(args.project_root)


def _cmd_hook(args) -> int:
    if args.action == "install":
        r = hooks.install(event=args.event, settings_path=args.settings)
        print(("installed" if r["changed"] else "already installed")
              + f" {r['event']} hook in {r['settings']}")
        print(f"  command: {r['command']}")
    elif args.action == "uninstall":
        r = hooks.uninstall(event=args.event, settings_path=args.settings)
        print(f"removed {r['removed']} {r['event']} hook entr(ies) from {r['settings']}")
    else:  # status
        r = hooks.status(settings_path=args.settings)
        print(f"hook settings: {r['settings']}")
        for ev, on in r["installed"].items():
            print(f"  {ev}: {'installed' if on else 'not installed'}")
        print(f"  command: {r['command']}")
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
    i.add_argument("--remote", default=None, help="git remote URL/path (omit for a local cluster dir)")
    i.add_argument("--project-id", default=None)
    i.set_defaults(func=_cmd_init)

    j = sub.add_parser("join", help="pull a project's context onto a new machine, localized")
    j.add_argument("project_root", nargs="?", default=None)
    j.add_argument("--cluster", required=True)
    j.add_argument("--remote", default=None, help="git remote URL/path (omit for a local cluster dir)")
    j.add_argument("--project-id", default=None)
    j.set_defaults(func=_cmd_join)

    pushp = sub.add_parser("push", help="localize->canonicalize local context into the cluster")
    pushp.add_argument("project_root", nargs="?", default=None)
    pushp.add_argument("--project-id", default=None)
    pushp.add_argument("--scan-secrets", action="store_true", help="scan for apparent secrets first")
    pushp.add_argument("--strict", action="store_true", help="with --scan-secrets, refuse on any finding")
    pushp.set_defaults(func=_cmd_push)

    for name, fn, help_ in (
        ("pull", _cmd_pull, "localize cluster context into ~/.claude/projects"),
        ("sync", _cmd_sync, "pull then push (the everyday verb)"),
        ("status", _cmd_status, "show dirty/behind state and roster"),
        ("scan", _cmd_scan, "scan local context for apparent secrets (no sync)"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("project_root", nargs="?", default=None)
        sp.add_argument("--project-id", default=None)
        sp.set_defaults(func=fn)

    hs = sub.add_parser("hook-sync", help="(internal) soft-failing sync for the Stop hook")
    hs.add_argument("project_root", nargs="?", default=None)
    hs.set_defaults(func=_cmd_hook_sync)

    hk = sub.add_parser("hook", help="install/uninstall the Stop-hook that runs sync at session end")
    hk.add_argument("action", choices=["install", "uninstall", "status"])
    hk.add_argument("--event", default=hooks.DEFAULT_EVENT, choices=["Stop", "SessionEnd"])
    hk.add_argument("--settings", default=None, help="settings.json path (default ~/.claude/settings.json)")
    hk.set_defaults(func=_cmd_hook)

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
