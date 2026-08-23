"""Command-line entry point for the server and install diagnostics."""

from __future__ import annotations

import argparse
import json
from typing import Optional, Sequence

from .diagnostics import build_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dcc-mcp-renderdoc")
    subparsers = parser.add_subparsers(dest="operation")
    for operation in ("doctor", "verify"):
        diagnostic = subparsers.add_parser(operation)
        diagnostic.add_argument("--json", action="store_true", dest="as_json")
        diagnostic.add_argument("--command")
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.operation is None:
        from .server import main as run_server

        run_server()
        return 0

    report = build_report(args.operation, command=args.command)
    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        state = "ready" if report["directly_usable"] else "not ready"
        print(f"RenderDoc adapter is {state}.")
        for step in report["next_steps"]:
            print(json.dumps(step, sort_keys=True))
    return int(report["exit_code"])


def main() -> int:
    return run()
