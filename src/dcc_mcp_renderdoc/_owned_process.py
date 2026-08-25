"""Bounded process-tree ownership for short RenderDoc runtime probes."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, BinaryIO, Mapping, Optional, Sequence

from dcc_mcp_core.skills_helper import check_dcc_cancelled

_MAX_CAPTURE_BYTES = 128 * 1024
_PROCESS_CLEANUP_SECS = 3.0


class OwnedProcessError(RuntimeError):
    """A bounded probe process could not be started or cleaned up safely."""


class OwnedProcessTimeoutError(OwnedProcessError):
    """A bounded probe process exceeded its deadline."""


class OwnedProcessCancelledError(OwnedProcessError):
    """A bounded probe process was cancelled by its caller."""


@dataclass(frozen=True)
class OwnedProcessResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


class _PipeCollector:
    """Drain one inherited child pipe while bounding retained output and joins."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.buffer = bytearray()
        self.truncated = False
        self.failed = False
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.stream.read(64 * 1024)
                if not chunk:
                    return
                remaining = (_MAX_CAPTURE_BYTES + 1) - len(self.buffer)
                if remaining > 0:
                    self.buffer.extend(chunk[:remaining])
                if len(chunk) > remaining or len(self.buffer) > _MAX_CAPTURE_BYTES:
                    self.truncated = True
        except OSError:
            self.failed = True

    def finish(self, deadline: float) -> tuple[str, bool]:
        self.thread.join(max(0.0, deadline - time.monotonic()))
        if self.thread.is_alive() and os.name == "nt" and self.thread.native_id is not None:
            _cancel_windows_thread_io(self.thread.native_id)
            self.thread.join(max(0.0, deadline - time.monotonic()))
        if self.thread.is_alive():
            raise OwnedProcessError("probe process output cleanup exceeded its bound")
        self.stream.close()
        if self.failed:
            raise OwnedProcessError("probe process output could not be drained")
        raw = bytes(self.buffer[:_MAX_CAPTURE_BYTES])
        return raw.decode("utf-8", errors="replace"), self.truncated


class _PosixProcessGroup:
    """Own one new session so exact descendants cannot outlive the probe."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.process_group = process.pid

    def leader_exited(self) -> bool:
        """Observe exit without reaping so the numeric group cannot be recycled."""
        if self.process.returncode is not None:
            raise OwnedProcessError("probe process identity was reaped before tree cleanup")
        try:
            result = os.waitid(
                os.P_PID,
                self.process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError as exc:
            raise OwnedProcessError("probe process identity was lost before tree cleanup") from exc
        return result is not None

    def terminate(self, *, force: bool = False) -> None:
        if self.process.returncode is not None:
            raise OwnedProcessError("refusing to signal a reaped probe process group")
        try:
            os.killpg(self.process_group, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass

    def wait_without_reaping(self, deadline: float) -> bool:
        while time.monotonic() < deadline:
            if self.leader_exited():
                return True
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        return self.leader_exited()

    def reap(self, deadline: float) -> None:
        _wait_for_exit(self.process, deadline)

    def close(self) -> None:
        return


class _WindowsJob:
    """Own a Windows child tree before its first instruction executes."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._thread_entry_type = ThreadEntry
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            self._handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        process_handle = self._ctypes.c_void_p(int(process._handle))  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        snapshot = self._kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
        if snapshot == self._ctypes.c_void_p(-1).value:
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        resumed = False
        try:
            entry = self._thread_entry_type()
            entry.dwSize = self._ctypes.sizeof(entry)
            found = bool(self._kernel32.Thread32First(snapshot, self._ctypes.byref(entry)))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread = self._kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                    if not thread:
                        raise self._ctypes.WinError(self._ctypes.get_last_error())
                    try:
                        if self._kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise self._ctypes.WinError(self._ctypes.get_last_error())
                        resumed = True
                    finally:
                        self._kernel32.CloseHandle(thread)
                found = bool(self._kernel32.Thread32Next(snapshot, self._ctypes.byref(entry)))
        finally:
            self._kernel32.CloseHandle(snapshot)
        if not resumed:
            raise OSError("suspended process has no resumable thread")

    def terminate(self, *, force: bool = False) -> None:
        del force
        if self._handle and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _cancel_windows_thread_io(thread_id: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    kernel32.CancelSynchronousIo.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    thread = kernel32.OpenThread(0x0001, False, thread_id)
    if not thread:
        return
    try:
        kernel32.CancelSynchronousIo(thread)
    finally:
        kernel32.CloseHandle(thread)


def _wait_for_exit(process: subprocess.Popen[bytes], deadline: float) -> None:
    remaining = max(0.0, deadline - time.monotonic())
    if remaining <= 0:
        raise OwnedProcessError("probe process cleanup exceeded its bound")
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        raise OwnedProcessError("probe process cleanup exceeded its bound") from exc


def run_owned_process(
    command: Sequence[str],
    *,
    timeout_secs: float,
    env: Optional[Mapping[str, str]] = None,
) -> OwnedProcessResult:
    """Run fixed argv under a tree owner with bounded capture and cleanup."""
    timeout = float(timeout_secs)
    if not command or timeout <= 0:
        raise OwnedProcessError("probe process requires a positive timeout and fixed argv")

    started = time.monotonic()
    process: Optional[subprocess.Popen[bytes]] = None
    owner: Any = None
    stdout_collector: Optional[_PipeCollector] = None
    stderr_collector: Optional[_PipeCollector] = None
    pending: Optional[BaseException] = None
    try:
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": False,
            "bufsize": 0,
            "shell": False,
        }
        if env is not None:
            popen_kwargs["env"] = dict(env)
        if os.name == "nt":
            owner = _WindowsJob()
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | 0x00000004
        else:
            popen_kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen([str(item) for item in command], **popen_kwargs)
            if os.name == "nt":
                owner.assign_and_resume(process)
            else:
                owner = _PosixProcessGroup(process)
        except BaseException as exc:
            if process is not None:
                process.kill()
                try:
                    process.wait(timeout=_PROCESS_CLEANUP_SECS)
                except subprocess.TimeoutExpired:
                    pass
            raise OwnedProcessError("probe process could not be started safely") from exc

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_collector = _PipeCollector(process.stdout)
        stderr_collector = _PipeCollector(process.stderr)
        stdout_collector.start()
        stderr_collector.start()
        deadline = started + timeout
        while True:
            exited = process.poll() is not None if os.name == "nt" else owner.leader_exited()
            if exited:
                break
            try:
                check_dcc_cancelled()
            except BaseException as exc:
                raise OwnedProcessCancelledError("probe process was cancelled") from exc
            if time.monotonic() >= deadline:
                raise OwnedProcessTimeoutError("probe process timed out")
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    except BaseException as exc:
        pending = exc

    cleanup_deadline = time.monotonic() + _PROCESS_CLEANUP_SECS
    cleanup_error: Optional[BaseException] = None
    stdout = ""
    stderr = ""
    stdout_truncated = False
    stderr_truncated = False
    try:
        if os.name != "nt" and isinstance(owner, _PosixProcessGroup):
            owner.terminate()
            grace_deadline = min(cleanup_deadline, time.monotonic() + 1.0)
            owner.wait_without_reaping(grace_deadline)
            owner.terminate(force=True)
            owner.wait_without_reaping(cleanup_deadline)
            owner.reap(cleanup_deadline)
        else:
            if owner is not None:
                owner.terminate()
            if process is not None and process.poll() is None:
                try:
                    process.wait(timeout=min(1.0, max(0.0, cleanup_deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    if owner is not None:
                        owner.terminate(force=True)
                    process.kill()
                    _wait_for_exit(process, cleanup_deadline)
            if owner is not None:
                owner.terminate(force=True)
        if stdout_collector is not None:
            stdout, stdout_truncated = stdout_collector.finish(cleanup_deadline)
        if stderr_collector is not None:
            stderr, stderr_truncated = stderr_collector.finish(cleanup_deadline)
    except BaseException as exc:
        cleanup_error = exc
    finally:
        if owner is not None:
            try:
                owner.close()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc

    if cleanup_error is not None:
        raise OwnedProcessError("probe process tree cleanup failed") from cleanup_error
    if pending is not None:
        raise pending.with_traceback(pending.__traceback__)
    assert process is not None and process.returncode is not None
    return OwnedProcessResult(
        returncode=int(process.returncode),
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )
