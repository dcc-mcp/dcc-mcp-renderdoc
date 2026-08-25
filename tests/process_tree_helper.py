"""Deterministic native-process tree for owned probe cleanup tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def _write(path: str, value: str) -> None:
    Path(path).write_text(value, encoding="ascii")


def _descendant(pid_path: str, ready_path: str) -> None:
    _write(pid_path, str(os.getpid()))
    sys.stdout.write("descendant stdout ready\n")
    sys.stdout.flush()
    sys.stderr.write("descendant stderr ready\n")
    sys.stderr.flush()
    _write(ready_path, "ready")
    while True:
        time.sleep(0.05)


def _root(
    root_pid_path: str,
    descendant_pid_path: str,
    ready_path: str,
    *,
    exit_after_ready: bool,
) -> None:
    _write(root_pid_path, str(os.getpid()))
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "descendant",
            descendant_pid_path,
            ready_path,
        ],
        stdin=subprocess.DEVNULL,
    )
    if exit_after_ready:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if Path(ready_path).is_file():
                return
            time.sleep(0.01)
        raise RuntimeError("descendant did not become ready")
    while True:
        time.sleep(0.05)


def main() -> None:
    if sys.argv[1] == "descendant":
        _descendant(sys.argv[2], sys.argv[3])
        return
    _root(
        sys.argv[1],
        sys.argv[2],
        sys.argv[3],
        exit_after_ready=len(sys.argv) > 4 and sys.argv[4] == "root-exit",
    )


if __name__ == "__main__":
    main()
