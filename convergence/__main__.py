"""Minimal CLI for the Sprint 0 spike: doctor / canonicalize / localize.

    python -m convergence doctor <context_dir> [--root R] [--home H]
    python -m convergence canonicalize <context_dir> <out_dir> [--root R]
    python -m convergence localize <in_dir> <context_dir> --root R

No sync, no roster persistence, no git — just the path-mapping core exercised
against a real (or copied) context directory.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

from .doctor import format_report, scan
from .pathmap import DEFAULT_SENTINEL, canonicalize_jsonl, localize_jsonl


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
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
