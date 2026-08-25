"""Bounded process-tree ownership for short RenderDoc runtime probes."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, BinaryIO, Mapping, Optional, Sequence

from dcc_mcp_core.skills_helper import check_dcc_cancelled

_MAX_CAPTURE_BYTES = 128 * 1024
_PROCESS_CLEANUP_SECS = 3.0
_POSIX_SUPERVISOR = r"""
import os
import signal
import subprocess
import sys

status_fd = int(sys.argv[1])
command = sys.argv[2:]
signal.signal(signal.SIGTERM, signal.SIG_IGN)

def restore_sigterm():
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

try:
    child = subprocess.Popen(command, preexec_fn=restore_sigterm)
except BaseException:
    payload = b"E\n"
else:
    payload = ("R%d\n" % child.wait()).encode("ascii")

try:
    os.write(status_fd, payload)
finally:
    os.close(status_fd)

while True:
    signal.pause()
"""


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

    def __init__(self, process: subprocess.Popen[bytes], status_fd: int) -> None:
        self.process = process
        self.process_group = process.pid
        self.status_fd: Optional[int] = status_fd
        self.status_buffer = bytearray()
        self.command_returncode: Optional[int] = None
        self._exited = False
        self._kqueue: Any = None
        if not hasattr(os, "waitid"):
            if not all(
                hasattr(select, name)
                for name in (
                    "kqueue",
                    "kevent",
                    "KQ_FILTER_PROC",
                    "KQ_EV_ADD",
                    "KQ_EV_ERROR",
                    "KQ_EV_ONESHOT",
                    "KQ_NOTE_EXIT",
                )
            ):
                raise OwnedProcessError("probe process identity watch is unavailable")
            queue = select.kqueue()
            try:
                event = select.kevent(
                    process.pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT,
                )
                queue.control([event], 0, 0)
            except BaseException:
                queue.close()
                raise
            self._kqueue = queue

    def read_command_returncode(self) -> Optional[int]:
        if self.command_returncode is not None:
            return self.command_returncode
        if self.status_fd is None:
            raise OwnedProcessError("probe process supervisor status was closed")
        try:
            chunk = os.read(self.status_fd, 64)
        except BlockingIOError:
            return None
        if chunk:
            self.status_buffer.extend(chunk)
        if b"\n" not in self.status_buffer:
            if not chunk:
                raise OwnedProcessError("probe process supervisor returned no status")
            return None
        line, remainder = bytes(self.status_buffer).split(b"\n", 1)
        if remainder or len(line) > 16:
            raise OwnedProcessError("probe process supervisor returned an invalid status")
        if line == b"E":
            raise OwnedProcessError("probe process could not be started safely")
        if not line.startswith(b"R"):
            raise OwnedProcessError("probe process supervisor returned an invalid status")
        try:
            returncode = int(line[1:].decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise OwnedProcessError("probe process supervisor returned an invalid status") from exc
        self.command_returncode = returncode
        return returncode

    def leader_exited(self) -> bool:
        """Observe exit without reaping so the numeric group cannot be recycled."""
        if self._exited:
            return True
        if self.process.returncode is not None:
            raise OwnedProcessError("probe process identity was reaped before tree cleanup")
        if self._kqueue is not None:
            events = self._kqueue.control(None, 1, 0)
            if not events:
                return False
            event = events[0]
            if event.ident != self.process.pid or not (event.fflags & select.KQ_NOTE_EXIT):
                raise OwnedProcessError("probe process identity watch returned an invalid event")
            if event.flags & select.KQ_EV_ERROR:
                raise OwnedProcessError("probe process identity watch failed")
            self._exited = True
            return True
        try:
            result = os.waitid(
                os.P_PID,
                self.process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError as exc:
            raise OwnedProcessError("probe process identity was lost before tree cleanup") from exc
        self._exited = result is not None
        return self._exited

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
        if self.status_fd is not None:
            os.close(self.status_fd)
            self.status_fd = None
        if self._kqueue is not None:
            self._kqueue.close()
            self._kqueue = None


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
    command_returncode: Optional[int] = None
    status_read: Optional[int] = None
    status_write: Optional[int] = None
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
            launch_command = [str(item) for item in command]
        else:
            popen_kwargs["start_new_session"] = True
            status_read, status_write = os.pipe()
            os.set_blocking(status_read, False)
            popen_kwargs["pass_fds"] = (status_write,)
            launch_command = [
                sys.executable,
                "-c",
                _POSIX_SUPERVISOR,
                str(status_write),
                *(str(item) for item in command),
            ]
        try:
            process = subprocess.Popen(launch_command, **popen_kwargs)
            if status_write is not None:
                os.close(status_write)
                status_write = None
            if os.name == "nt":
                owner.assign_and_resume(process)
            else:
                assert status_read is not None
                owner = _PosixProcessGroup(process, status_read)
                status_read = None
        except BaseException as exc:
            if status_write is not None:
                os.close(status_write)
                status_write = None
            if status_read is not None:
                os.close(status_read)
                status_read = None
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
            if os.name == "nt":
                exited = process.poll() is not None
            else:
                command_returncode = owner.read_command_returncode()
                exited = command_returncode is not None
                if not exited and owner.leader_exited():
                    raise OwnedProcessError("probe process supervisor exited unexpectedly")
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

    if status_write is not None:
        os.close(status_write)
        status_write = None
    if status_read is not None:
        os.close(status_read)
        status_read = None

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
    returncode = process.returncode if os.name == "nt" else command_returncode
    assert returncode is not None
    return OwnedProcessResult(
        returncode=int(returncode),
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )
