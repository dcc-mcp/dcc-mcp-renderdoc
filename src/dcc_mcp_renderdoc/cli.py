"""Command-line entry point for the server and install diagnostics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from .diagnostics import build_report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dcc-mcp-renderdoc")
    subparsers = parser.add_subparsers(dest="operation")
    for operation in ("doctor", "install", "status", "verify", "uninstall", "upgrade"):
        diagnostic = subparsers.add_parser(operation)
        diagnostic.add_argument("--json", action="store_true", dest="as_json")
        diagnostic.add_argument("--command")
        diagnostic.add_argument("--yes", action="store_true")
        diagnostic.add_argument("--dry-run", action="store_true")
        diagnostic.add_argument("--dcc-path", type=Path)
        diagnostic.add_argument("--python", type=Path)
        diagnostic.add_argument("--receipt-path", type=Path)
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.operation is None:
        from .server import main as run_server

        run_server()
        return 0

    if args.operation in {"install", "status", "verify", "uninstall", "upgrade"}:
        from .lifecycle import handle

        report, exit_code = handle(args)
    else:
        report = build_report(args.operation, command=args.command)
        exit_code = int(report["exit_code"])
    if args.as_json:
        report["exit_code"] = exit_code
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        directly_usable = bool(
            report.get("directly_usable", report.get("verify", {}).get("directly_usable"))
        )
        state = "ready" if directly_usable else "not ready"
        print(f"RenderDoc adapter is {state}.")
        for step in report["next_steps"]:
            print(json.dumps(step, sort_keys=True))
    return exit_code


def main() -> int:
    return run()
