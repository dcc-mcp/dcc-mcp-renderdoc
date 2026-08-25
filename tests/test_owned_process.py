from __future__ import annotations

import concurrent.futures
import ctypes
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

from dcc_mcp_renderdoc import _owned_process
from dcc_mcp_renderdoc._owned_process import (
    OwnedProcessCancelledError,
    OwnedProcessTimeoutError,
    run_owned_process,
)

PROCESS_TREE_HELPER = Path(__file__).with_name("process_tree_helper.py")


class _ProcessIdentity:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.handle: Optional[int] = None
        self.pidfd: Optional[int] = None
        self.start_token: Optional[str] = None
        if os.name == "nt":
            synchronize_and_terminate = 0x00100001
            handle = ctypes.windll.kernel32.OpenProcess(synchronize_and_terminate, False, pid)
            if not handle:
                raise OSError("failed to bind process identity")
            self.handle = int(handle)
        elif hasattr(os, "pidfd_open"):
            self.pidfd = os.pidfd_open(pid)
        else:
            self.start_token = self._posix_start_token()
            if not self.start_token:
                raise OSError("failed to bind process identity")

    def _posix_start_token(self) -> Optional[str]:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(self.pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        token = completed.stdout.strip()
        return token or None

    def wait_dead(self, timeout_secs: float = 5.0) -> bool:
        if self.handle is not None:
            return (
                ctypes.windll.kernel32.WaitForSingleObject(self.handle, int(timeout_secs * 1000))
                == 0
            )
        if self.pidfd is not None:
            readable, _, _ = select.select([self.pidfd], [], [], timeout_secs)
            return bool(readable)
        deadline = time.monotonic() + timeout_secs
        while time.monotonic() < deadline:
            if self._posix_start_token() != self.start_token:
                return True
            time.sleep(0.05)
        return False

    def force_kill(self) -> None:
        if self.handle is not None:
            ctypes.windll.kernel32.TerminateProcess(self.handle, 91)
            return
        if self.pidfd is not None and hasattr(signal, "pidfd_send_signal"):
            signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
            return
        if self._posix_start_token() == self.start_token:
            os.kill(self.pid, signal.SIGKILL)

    def close(self) -> None:
        if self.handle is not None:
            ctypes.windll.kernel32.CloseHandle(self.handle)
        if self.pidfd is not None:
            os.close(self.pidfd)


def _wait_for_ready(future, ready_path: Path, timeout_secs: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if ready_path.is_file():
            return
        if future.done():
            future.result()
        time.sleep(0.01)
    raise AssertionError("process-tree helper did not become ready")


def _bind_tree(root_pid: Path, descendant_pid: Path) -> list[_ProcessIdentity]:
    return [
        _ProcessIdentity(int(root_pid.read_text(encoding="ascii"))),
        _ProcessIdentity(int(descendant_pid.read_text(encoding="ascii"))),
    ]


def _assert_tree_dead(identities: list[_ProcessIdentity]) -> None:
    dead = [False] * len(identities)
    try:
        for index, identity in enumerate(identities):
            dead[index] = identity.wait_dead()
        assert all(dead)
    finally:
        for identity, is_dead in zip(identities, dead):
            if not is_dead:
                identity.force_kill()
            identity.close()


def test_timeout_terminates_ready_descendant_tree_with_inherited_pipes(tmp_path: Path) -> None:
    root_pid = tmp_path / "root.pid"
    descendant_pid = tmp_path / "descendant.pid"
    ready = tmp_path / "descendant.ready"
    command = [
        sys.executable,
        str(PROCESS_TREE_HELPER),
        str(root_pid),
        str(descendant_pid),
        str(ready),
    ]
    started = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_owned_process, command, timeout_secs=2.0)
        _wait_for_ready(future, ready)
        identities = _bind_tree(root_pid, descendant_pid)
        with pytest.raises(OwnedProcessTimeoutError, match="probe process timed out"):
            future.result(timeout=6)

    assert time.monotonic() - started < 7
    _assert_tree_dead(identities)


def test_cancellation_terminates_ready_descendant_tree_without_orphans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cancelled = threading.Event()

    def check_cancelled() -> None:
        if cancelled.is_set():
            raise RuntimeError("private cancellation detail")

    monkeypatch.setattr(_owned_process, "check_dcc_cancelled", check_cancelled)
    root_pid = tmp_path / "root.pid"
    descendant_pid = tmp_path / "descendant.pid"
    ready = tmp_path / "descendant.ready"
    command = [
        sys.executable,
        str(PROCESS_TREE_HELPER),
        str(root_pid),
        str(descendant_pid),
        str(ready),
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_owned_process, command, timeout_secs=10.0)
        _wait_for_ready(future, ready)
        identities = _bind_tree(root_pid, descendant_pid)
        cancelled.set()
        with pytest.raises(OwnedProcessCancelledError, match="probe process was cancelled"):
            future.result(timeout=6)

    _assert_tree_dead(identities)


def test_successful_probe_capture_is_bounded_and_drained() -> None:
    payload_size = _owned_process._MAX_CAPTURE_BYTES + 4096
    result = run_owned_process(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.buffer.write(b'x' * {}); sys.stdout.flush(); "
                "sys.stderr.buffer.write(b'y' * {}); sys.stderr.flush()"
            ).format(payload_size, payload_size),
        ],
        timeout_secs=5.0,
    )

    assert result.returncode == 0
    assert len(result.stdout.encode("utf-8")) == _owned_process._MAX_CAPTURE_BYTES
    assert len(result.stderr.encode("utf-8")) == _owned_process._MAX_CAPTURE_BYTES
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_unstartable_probe_fails_closed_without_exposing_argv(tmp_path: Path) -> None:
    missing = tmp_path / "private-missing-probe"

    with pytest.raises(
        _owned_process.OwnedProcessError,
        match="^probe process could not be started safely$",
    ) as error:
        run_owned_process([str(missing), "private-argument"], timeout_secs=2.0)

    assert str(missing) not in str(error.value)
    assert "private-argument" not in str(error.value)


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "waitid"),
    reason="waitid identity oracle is unavailable",
)
def test_posix_never_signals_a_reaped_session_leader_numeric_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_killpg = os.killpg
    safe_signals = []
    unsafe_signals = []

    def identity_checked_killpg(process_group: int, sig: int) -> None:
        try:
            identity = os.waitid(
                os.P_PID,
                process_group,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            unsafe_signals.append((process_group, sig))
            return
        safe_signals.append((process_group, sig, identity is not None))
        real_killpg(process_group, sig)

    monkeypatch.setattr(os, "killpg", identity_checked_killpg)

    result = run_owned_process([sys.executable, "-c", "pass"], timeout_secs=5.0)

    assert result.returncode == 0
    assert unsafe_signals == []
    assert safe_signals
    assert all(not leader_exited for _, _, leader_exited in safe_signals)


@pytest.mark.skipif(os.name == "nt", reason="POSIX root-first process-tree regression")
def test_posix_root_first_exit_still_kills_inherited_pipe_descendant(
    tmp_path: Path,
) -> None:
    root_pid = tmp_path / "root.pid"
    descendant_pid = tmp_path / "descendant.pid"
    ready = tmp_path / "descendant.ready"
    command = [
        sys.executable,
        str(PROCESS_TREE_HELPER),
        str(root_pid),
        str(descendant_pid),
        str(ready),
        "root-exit",
    ]
    started = time.monotonic()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_owned_process, command, timeout_secs=5.0)
        _wait_for_ready(future, ready)
        descendant_identity = _ProcessIdentity(int(descendant_pid.read_text(encoding="ascii")))
        result = future.result(timeout=6)

    assert result.returncode == 0
    assert time.monotonic() - started < 6
    _assert_tree_dead([descendant_identity])
