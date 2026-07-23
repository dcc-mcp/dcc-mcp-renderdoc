"""Small, typed wrapper around RenderDoc's official command-line client."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .downloader import download_latest


class RenderDocError(RuntimeError):
    """Raised when a RenderDoc operation cannot satisfy its contract."""


class _CaptureController:
    def __init__(self, process: Any, stdout: list[str], stderr: list[str], readers: list[Any]):
        self.process = process
        self.stdout = stdout
        self.stderr = stderr
        self.readers = readers
        self.launched_id: Optional[int] = None

    def output(self) -> tuple[str, str]:
        return "".join(self.stdout), "".join(self.stderr)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=1)


def _is_launched_id_returncode(returncode: Optional[int], launched_id: int) -> bool:
    if returncode is None or returncode in (0, launched_id):
        return True
    return sys.platform != "win32" and returncode == launched_id % 256


def _start_capture_controller(
    arguments: Sequence[str], *, timeout_secs: int, command: Optional[str]
) -> _CaptureController:
    executable = resolve_renderdoccmd(command)
    try:
        process = subprocess.Popen(
            [str(executable), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
    except OSError as exc:
        raise RenderDocError(f"Could not start RenderDoc: {exc}") from exc

    stdout: list[str] = []
    stderr: list[str] = []

    def read_stream(stream: Any, output: list[str]) -> None:
        for chunk in iter(lambda: stream.read(1), ""):
            output.append(chunk)

    readers = [
        threading.Thread(target=read_stream, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=read_stream, args=(process.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    controller = _CaptureController(process, stdout, stderr, readers)
    deadline = time.monotonic() + timeout_secs
    while True:
        returncode = process.poll()
        if returncode is not None:
            for reader in readers:
                reader.join(timeout=1)
        combined = "".join(stdout + stderr)
        launched = re.search(r"Launched as ID (\d+)", combined)
        if launched is not None:
            launched_id = int(launched.group(1))
            if _is_launched_id_returncode(returncode, launched_id):
                controller.launched_id = launched_id
                return controller
        if returncode is not None:
            controller.close()
            detail = "".join(controller.output()).strip()[-2000:] or "unknown error"
            raise RenderDocError(f"RenderDoc exited with code {returncode}: {detail}")
        if time.monotonic() >= deadline:
            controller.close()
            detail = "".join(controller.output()).strip()[-2000:]
            suffix = f": {detail}" if detail else ""
            raise RenderDocError(
                f"RenderDoc command timed out waiting for {arguments[0]} readiness after "
                f"{timeout_secs}s{suffix}"
            )
        time.sleep(0.01)


def resolve_renderdoccmd(explicit: Optional[str] = None) -> Path:
    """Resolve an executable RenderDoc CLI without invoking a shell."""
    candidate = explicit or os.environ.get("DCC_MCP_RENDERDOC_CMD")
    if candidate:
        path = Path(candidate).expanduser().resolve()
        if path.is_file():
            return path
        raise RenderDocError(f"RenderDoc command does not exist: {path}")

    for name in ("renderdoccmd.exe", "renderdoccmd"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    if os.environ.get("DCC_MCP_RENDERDOC_AUTO_DOWNLOAD", "1").lower() not in {
        "0",
        "false",
        "no",
    }:
        try:
            return download_latest()
        except (OSError, RuntimeError) as exc:
            raise RenderDocError(
                f"RenderDoc was not found and automatic download failed: {exc}"
            ) from exc
    raise RenderDocError(
        "renderdoccmd was not found; install RenderDoc or set DCC_MCP_RENDERDOC_CMD"
    )


def _resolve_qrenderdoc(command: Optional[str]) -> Path:
    renderdoccmd = resolve_renderdoccmd(command)
    name = "qrenderdoc.exe" if renderdoccmd.suffix.casefold() == ".exe" else "qrenderdoc"
    qrenderdoc = renderdoccmd.with_name(name)
    if not qrenderdoc.is_file():
        raise RenderDocError(
            "qrenderdoc was not found beside renderdoccmd; required for Target Control: "
            f"{qrenderdoc}"
        )
    return qrenderdoc


def _validate_target_control_status(status: Any) -> dict[str, Any]:
    fields = {
        "schema_version",
        "connected",
        "triggered",
        "shutdown",
        "timed_out",
        "target_pid",
        "capture_path",
        "error",
    }
    if not isinstance(status, dict) or set(status) != fields:
        raise RenderDocError("Target Control returned an invalid status schema")
    if type(status["schema_version"]) is not int or status["schema_version"] != 1:
        raise RenderDocError("Target Control returned an unsupported status version")
    boolean_fields = ("connected", "triggered", "shutdown", "timed_out")
    if any(type(status[name]) is not bool for name in boolean_fields):
        raise RenderDocError("Target Control returned invalid boolean status fields")
    if status["target_pid"] is not None and (
        type(status["target_pid"]) is not int or status["target_pid"] <= 0
    ):
        raise RenderDocError("Target Control returned an invalid target PID")
    if status["capture_path"] is not None and (
        not isinstance(status["capture_path"], str) or not status["capture_path"]
    ):
        raise RenderDocError("Target Control returned an invalid capture path")
    if status["error"] is not None and not isinstance(status["error"], str):
        raise RenderDocError("Target Control returned an invalid error field")
    if status["error"] is not None:
        raise RenderDocError(f"Target Control failed: {status['error'] or 'empty error'}")
    if (
        not status["connected"]
        or not status["triggered"]
        or not status["shutdown"]
        or status["timed_out"]
    ):
        raise RenderDocError(f"Target Control failed: {status['error'] or 'incomplete status'}")
    if type(status["target_pid"]) is not int:
        raise RenderDocError("Target Control success did not include a target PID")
    if not isinstance(status["capture_path"], str) or not status["capture_path"]:
        raise RenderDocError("Target Control success did not include a capture path")
    return status


def _configure_qrenderdoc_environment(root: Path, environment: dict[str, str]) -> None:
    if sys.platform == "linux":
        config_root = root / "xdg-data"
        environment["XDG_DATA_HOME"] = str(config_root)
    elif sys.platform == "win32":
        config_root = root / "appdata-roaming"
        environment["APPDATA"] = str(config_root)
        environment["LOCALAPPDATA"] = str(root / "appdata-local")
    else:
        return
    config_directory = config_root / "qrenderdoc"
    config_directory.mkdir(parents=True)
    (config_directory / "UI.config").write_text(
        json.dumps({"Analytics_TotalOptOut": True, "rdocConfigData": 1}),
        encoding="utf-8",
    )


def _read_diagnostic_tail(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[-2000:]
    except OSError:
        return ""


def _trigger_target_capture(
    ident: int,
    *,
    capture_wait_secs: int,
    command: Optional[str],
    target_name: Optional[str] = None,
    trigger_after_secs: float = 0.0,
    expected_pid: Optional[int] = None,
) -> dict[str, Any]:
    if type(ident) is not int or ident <= 0:
        raise RenderDocError("RenderDoc target ident must be a positive integer")
    if type(capture_wait_secs) is not int or capture_wait_secs <= 0:
        raise RenderDocError("Target Control timeout must be a positive integer")
    if target_name is not None and (not isinstance(target_name, str) or not target_name.strip()):
        raise RenderDocError("Target Control target name must be a non-empty string")
    if expected_pid is not None and (type(expected_pid) is not int or expected_pid <= 0):
        raise RenderDocError("Target Control expected PID must be a positive integer")
    if (
        isinstance(trigger_after_secs, bool)
        or not isinstance(trigger_after_secs, (int, float))
        or trigger_after_secs < 0
    ):
        raise RenderDocError("Target Control trigger delay must be a non-negative number")
    if sys.platform not in {"win32", "linux"}:
        raise RenderDocError(
            "RenderDoc Target Control runtime is supported only on Windows and Linux"
        )
    if (
        sys.platform == "linux"
        and not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
        and not os.environ.get("QT_QPA_PLATFORM")
    ):
        raise RenderDocError(
            "RenderDoc Target Control requires an X/Wayland display on Linux; "
            "run under Xvfb or configure QT_QPA_PLATFORM explicitly"
        )
    qrenderdoc = _resolve_qrenderdoc(command)
    script_path = Path(__file__).with_name("_target_control.py")
    if not script_path.is_file():
        raise RenderDocError(f"Bundled Target Control helper is missing: {script_path}")
    with tempfile.TemporaryDirectory(prefix="dcc-mcp-renderdoc-target-") as directory:
        root = Path(directory)
        status_path = root / "status.json"
        environment = os.environ.copy()
        environment.update(
            {
                "DCC_MCP_RENDERDOC_TARGET_IDENT": str(ident),
                "DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS": str(capture_wait_secs),
                "DCC_MCP_RENDERDOC_TARGET_STATUS": str(status_path),
                "DCC_MCP_RENDERDOC_TRIGGER_AFTER_SECS": str(trigger_after_secs),
            }
        )
        if target_name is not None:
            environment["DCC_MCP_RENDERDOC_TARGET_NAME"] = target_name
        else:
            environment.pop("DCC_MCP_RENDERDOC_TARGET_NAME", None)
        if expected_pid is not None:
            environment["DCC_MCP_RENDERDOC_EXPECTED_PID"] = str(expected_pid)
        else:
            environment.pop("DCC_MCP_RENDERDOC_EXPECTED_PID", None)
        _configure_qrenderdoc_environment(root, environment)
        stdout_path = root / "qrenderdoc.stdout.log"
        stderr_path = root / "qrenderdoc.stderr.log"
        try:
            with (
                stdout_path.open("w", encoding="utf-8") as stdout,
                stderr_path.open("w", encoding="utf-8") as stderr,
            ):
                result = subprocess.run(
                    [str(qrenderdoc), "--python", str(script_path)],
                    check=False,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    timeout=capture_wait_secs + int(trigger_after_secs) + 31,
                    shell=False,
                    env=environment,
                )
        except subprocess.TimeoutExpired as exc:
            diagnostics = {
                "target_ident": ident,
                "target_name": target_name,
                "expected_pid": expected_pid,
                "capture_wait_secs": capture_wait_secs,
                "trigger_after_secs": trigger_after_secs,
                "qt_qpa_platform": environment.get("QT_QPA_PLATFORM"),
                "display_configured": bool(
                    environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY")
                ),
                "status": _read_diagnostic_tail(status_path) or "missing",
                "stdout": _read_diagnostic_tail(stdout_path),
                "stderr": _read_diagnostic_tail(stderr_path),
            }
            raise RenderDocError(
                "Target Control host timed out after "
                f"{capture_wait_secs + int(trigger_after_secs) + 31}s; "
                f"diagnostics={json.dumps(diagnostics, sort_keys=True)}"
            ) from exc
        if result.returncode != 0:
            detail = (
                _read_diagnostic_tail(stderr_path)
                or _read_diagnostic_tail(stdout_path)
                or "no host output"
            )
            raise RenderDocError(
                f"Target Control host exited with code {result.returncode}: {detail}"
            )
        if not status_path.is_file():
            detail = (
                _read_diagnostic_tail(stderr_path)
                or _read_diagnostic_tail(stdout_path)
                or "no status output"
            )
            raise RenderDocError(f"Target Control did not write status: {detail}")
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RenderDocError("Target Control wrote malformed status JSON") from exc
        return _validate_target_control_status(status)


def _capture_from_target_status(
    status: dict[str, Any], directory: Path, before: set[Path]
) -> list[Path]:
    capture = Path(status["capture_path"]).expanduser().resolve()
    if capture.parent != directory.resolve() or capture.suffix.casefold() != ".rdc":
        raise RenderDocError("Target Control returned a capture outside the requested RDC output")
    if not capture.is_file() or capture in before:
        raise RenderDocError("Target Control capture is missing or was not created by this request")
    if capture.stat().st_size <= 0:
        raise RenderDocError("Target Control capture is empty")
    return [capture]


def _run(
    arguments: Sequence[str],
    *,
    timeout_secs: int,
    command: Optional[str] = None,
    accept_launched_id: bool = False,
) -> subprocess.CompletedProcess[str]:
    executable = resolve_renderdoccmd(command)
    try:
        result = subprocess.run(
            [str(executable), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderDocError(f"RenderDoc command timed out after {timeout_secs}s") from exc
    except OSError as exc:
        raise RenderDocError(f"Could not start RenderDoc: {exc}") from exc
    if result.returncode != 0:
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        launched = re.search(r"Launched as ID (\d+)", stdout + "\n" + stderr)
        if not (
            accept_launched_id
            and launched is not None
            and _is_launched_id_returncode(result.returncode, int(launched.group(1)))
        ):
            detail = (
                "\n".join(
                    part
                    for part in (
                        f"stdout: {stdout.strip()[-1000:]}" if stdout.strip() else "",
                        f"stderr: {stderr.strip()[-1000:]}" if stderr.strip() else "",
                    )
                    if part
                )
                or "unknown error"
            )
            raise RenderDocError(f"RenderDoc exited with code {result.returncode}: {detail}")
    return result


def get_version(*, command: Optional[str] = None) -> dict[str, Any]:
    result = _run(["version"], timeout_secs=30, command=command)
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return {"command": str(resolve_renderdoccmd(command)), "version_output": output}


def capture_program(
    executable: str,
    output_template: str,
    *,
    arguments: Optional[Sequence[str]] = None,
    working_directory: Optional[str] = None,
    wait_for_exit: bool = True,
    api_validation: bool = False,
    hook_children: bool = False,
    trigger_after_secs: Optional[float] = None,
    trigger_process_name: Optional[str] = None,
    capture_wait_secs: int = 30,
    timeout_secs: int = 300,
    command: Optional[str] = None,
) -> dict[str, Any]:
    """Launch one explicit executable under RenderDoc and return new captures."""
    target = Path(executable).expanduser().resolve()
    if not target.is_file():
        raise RenderDocError(f"Target executable does not exist: {target}")
    output = Path(output_template).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    cwd = Path(working_directory).expanduser().resolve() if working_directory else target.parent
    if not cwd.is_dir():
        raise RenderDocError(f"Working directory does not exist: {cwd}")
    if trigger_process_name is not None and not hook_children:
        raise RenderDocError("trigger_process_name requires hook_children=True")

    before = {path.resolve() for path in output.parent.glob("*.rdc")}
    cli_args = ["capture", "--working-dir", str(cwd), "--capture-file", str(output)]
    effective_wait_for_exit = wait_for_exit and trigger_after_secs is None
    if effective_wait_for_exit:
        cli_args.append("--wait-for-exit")
    if api_validation:
        cli_args.append("--opt-api-validation")
    if hook_children:
        cli_args.append("--opt-hook-children")
    cli_args.extend([str(target), *(str(value) for value in arguments or [])])
    target_pid: Optional[int] = None
    trigger_mode = "renderdoccmd"
    if trigger_after_secs is not None:
        controller = _start_capture_controller(
            cli_args,
            timeout_secs=timeout_secs,
            command=command,
        )
        try:
            status = _trigger_target_capture(
                controller.launched_id,
                capture_wait_secs=capture_wait_secs,
                command=command,
                target_name=trigger_process_name,
                trigger_after_secs=trigger_after_secs,
            )
            target_pid = status["target_pid"]
            captures = _capture_from_target_status(status, output.parent, before)
            trigger_mode = "target_control"
        finally:
            controller.close()
        stdout, stderr = controller.output()
    else:
        result = _run(
            cli_args,
            timeout_secs=timeout_secs,
            command=command,
            accept_launched_id=not effective_wait_for_exit,
        )
        stdout, stderr = result.stdout, result.stderr
        captures = _new_captures(output.parent, before)
    if not captures:
        output_detail = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())[-2000:]
        diagnostics = _capture_failure_diagnostics(
            process_id=target_pid,
            process_name=trigger_process_name or target.name,
            focused=False,
        )
        raise RenderDocError(
            "No new .rdc capture appeared; ensure the target presents a frame and was hooked "
            f"before creating its graphics device. {diagnostics}. "
            f"RenderDoc output: {output_detail}"
        )
    capture_validations = _validate_frame_captures(captures, command=command)
    return {
        "target": str(target),
        "captures": [str(path) for path in captures],
        "capture_validations": capture_validations,
        "focused_target_window": False,
        "trigger_mode": trigger_mode,
        "stdout": stdout.strip(),
    }


def capture_process(
    process_id: int,
    output_template: str,
    *,
    working_directory: Optional[str] = None,
    trigger_after_secs: float = 2.0,
    capture_wait_secs: int = 30,
    api_validation: bool = False,
    timeout_secs: int = 60,
    command: Optional[str] = None,
) -> dict[str, Any]:
    """Inject into one live process, trigger through Target Control, and return the capture."""
    if process_id <= 0:
        raise RenderDocError("Process ID must be a positive integer")
    output = Path(output_template).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    before = {path.resolve() for path in output.parent.glob("*.rdc")}
    cli_args = ["inject", f"--PID={process_id}", "--capture-file", str(output)]
    if working_directory:
        cwd = Path(working_directory).expanduser().resolve()
        if not cwd.is_dir():
            raise RenderDocError(f"Working directory does not exist: {cwd}")
        cli_args.extend(["--working-dir", str(cwd)])
    if api_validation:
        cli_args.append("--opt-api-validation")
    controller = _start_capture_controller(
        cli_args,
        timeout_secs=timeout_secs,
        command=command,
    )
    try:
        status = _trigger_target_capture(
            controller.launched_id,
            capture_wait_secs=capture_wait_secs,
            command=command,
            trigger_after_secs=trigger_after_secs,
            expected_pid=process_id,
        )
        if status["target_pid"] != process_id:
            raise RenderDocError(
                "Target Control connected to an unexpected process: "
                f"expected PID {process_id}, got {status['target_pid']}"
            )
        captures = _capture_from_target_status(status, output.parent, before)
    finally:
        controller.close()
    stdout, stderr = controller.output()
    if not captures:
        output_detail = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())[-2000:]
        diagnostics = _capture_failure_diagnostics(
            process_id=process_id,
            process_name=None,
            focused=False,
        )
        raise RenderDocError(
            "No .rdc capture appeared after injection and Target Control trigger. Inject before "
            "the target creates its graphics device; for an already initialized game, relaunch "
            "it with "
            f"capture_program. {diagnostics}. "
            f"RenderDoc output: {output_detail}"
        )
    capture_validations = _validate_frame_captures(captures, command=command)
    return {
        "process_id": process_id,
        "captures": [str(path) for path in captures],
        "capture_validations": capture_validations,
        "focused_target_window": False,
        "trigger_mode": "target_control",
        "stdout": stdout.strip(),
    }


def _new_captures(directory: Path, before: set[Path]) -> list[Path]:
    return sorted(
        (path.resolve() for path in directory.glob("*.rdc") if path.resolve() not in before),
        key=lambda path: path.stat().st_mtime_ns,
    )


def _visible_processes(limit: int = 64) -> list[dict[str, Any]]:
    if sys.platform != "win32":
        return []

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def find_window(window, _extra):
        if not user32.IsWindowVisible(window) or len(found) >= limit:
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if not owner.value or owner.value in seen:
            return True
        process = kernel32.OpenProcess(0x1000, False, owner.value)
        if not process:
            return True
        try:
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                seen.add(owner.value)
                found.append({"process_id": owner.value, "name": Path(path.value).name})
        finally:
            kernel32.CloseHandle(process)
        return True

    user32.EnumWindows(find_window, 0)
    return found


def _capture_failure_diagnostics(
    *, process_id: Optional[int], process_name: Optional[str], focused: bool
) -> str:
    visible = _visible_processes()
    if process_id is not None:
        match = next((process for process in visible if process["process_id"] == process_id), None)
        target = f"{match['name']}(pid={process_id})" if match else f"pid={process_id}:not-visible"
    else:
        target = f"{Path(process_name).name}:not-found" if process_name else "not-found"
    snapshot = ", ".join(
        f"{process['name']}(pid={process['process_id']})" for process in visible[:20]
    )
    return (
        f"target_process={target}; focused_target_window={focused}; "
        f"visible_processes=[{snapshot or 'none'}]"
    )


def _require_capture(capture_file: str) -> Path:
    path = Path(capture_file).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".rdc":
        raise RenderDocError(f"RenderDoc capture does not exist or is not .rdc: {path}")
    return path


def _child_text(parent: ET.Element, name: str) -> Optional[str]:
    child = parent.find(name)
    return child.text.strip() if child is not None and child.text else None


def _chunk_operation(name: str) -> str:
    if name.casefold().startswith("internal::"):
        return ""
    return name.rsplit("::", 1)[-1].casefold()


def _is_draw_or_dispatch_chunk(name: str) -> bool:
    operation = _chunk_operation(name)
    if operation.startswith("gldrawbuffer"):
        return False
    return operation.startswith(
        (
            "draw",
            "dispatch",
            "tracerays",
            "executeindirect",
            "vkcmddraw",
            "vkcmddispatch",
            "vkcmdtracerays",
            "gldraw",
            "glmultidraw",
            "gldispatch",
        )
    )


def _is_frame_work_chunk(name: str) -> bool:
    if _is_draw_or_dispatch_chunk(name):
        return True
    operation = _chunk_operation(name)
    if operation.startswith("gl"):
        clear = operation == "glclear" or operation.startswith(
            ("glclearbuffer", "glclearnamedframebuffer", "glcleartex")
        )
    else:
        clear = operation != "clearstate" and operation.startswith(("clear", "vkcmdclear"))
    return clear or operation.startswith(
        (
            "copy",
            "resolve",
            "blit",
            "vkcmdcopy",
            "vkcmdresolve",
            "vkcmdblit",
            "glcopy",
            "glblit",
            "vkcmdbeginrenderpass",
            "vkcmdbeginrendering",
            "vkcmdexecutecommands",
            "executecommandlist",
        )
    )


def _is_present_chunk(name: str) -> bool:
    operation = _chunk_operation(name)
    return operation.startswith(
        (
            "present",
            "vkqueuepresent",
            "swapbuffers",
            "glxswapbuffers",
            "eglswapbuffers",
            "wglswapbuffers",
            "wglswaplayerbuffers",
        )
    )


def parse_capture_xml(xml_file: str, *, representative_limit: int = 20) -> dict[str, Any]:
    """Parse the stable high-level fields emitted by RenderDoc's XML converter."""
    path = Path(xml_file)
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise RenderDocError(f"Could not parse RenderDoc XML: {exc}") from exc
    if root.tag != "rdc":
        raise RenderDocError(f"Unexpected RenderDoc XML root: {root.tag}")
    header = root.find("header")
    chunks = root.find("chunks")
    if header is None or chunks is None:
        raise RenderDocError("RenderDoc XML is missing header or chunks")

    driver = header.find("driver")
    thumbnail = header.find("thumbnail")
    names = [chunk.get("name") or "unnamed" for chunk in chunks.findall("chunk")]
    counts = Counter(names)
    draw_dispatch_count = sum(_is_draw_or_dispatch_chunk(name) for name in names)
    frame_work_count = sum(_is_frame_work_chunk(name) for name in names)
    present_count = sum(_is_present_chunk(name) for name in names)
    return {
        "driver": {
            "id": driver.get("id") if driver is not None else None,
            "name": driver.text.strip() if driver is not None and driver.text else None,
        },
        "machine_ident": _child_text(header, "machineIdent"),
        "thumbnail": {
            "width": int(thumbnail.get("width", "0")) if thumbnail is not None else 0,
            "height": int(thumbnail.get("height", "0")) if thumbnail is not None else 0,
        },
        "chunk_version": chunks.get("version"),
        "chunk_count": len(names),
        "draw_dispatch_count": draw_dispatch_count,
        "frame_work_count": frame_work_count,
        "present_count": present_count,
        "frame_content_status": (
            "rendering_commands_present" if frame_work_count else "no_rendering_work"
        ),
        "chunk_frequencies": [
            {"name": name, "count": count} for name, count in counts.most_common(20)
        ],
        "representative_chunks": names[:representative_limit],
    }


def inspect_capture(
    capture_file: str,
    *,
    representative_limit: int = 20,
    command: Optional[str] = None,
) -> dict[str, Any]:
    capture = _require_capture(capture_file)
    with tempfile.TemporaryDirectory(prefix="dcc-mcp-renderdoc-") as directory:
        xml_file = Path(directory) / "capture.xml"
        _run(
            [
                "convert",
                "--filename",
                str(capture),
                "--output",
                str(xml_file),
                "--convert-format",
                "xml",
            ],
            timeout_secs=180,
            command=command,
        )
        details = parse_capture_xml(str(xml_file), representative_limit=representative_limit)
    return {"capture_file": str(capture), "size_bytes": capture.stat().st_size, **details}


def _validate_frame_captures(
    captures: Sequence[Path], *, command: Optional[str]
) -> list[dict[str, Any]]:
    validations = []
    for capture in captures:
        details = inspect_capture(str(capture), command=command)
        if details["frame_work_count"] == 0:
            diagnostics = {
                "capture_file": details["capture_file"],
                "driver": details["driver"],
                "chunk_count": details["chunk_count"],
                "draw_dispatch_count": details["draw_dispatch_count"],
                "present_count": details["present_count"],
                "representative_chunks": details["representative_chunks"],
            }
            raise RenderDocError(
                "RenderDoc capture contains no rendering work (Draw, Dispatch, Clear, Copy, "
                "Resolve, Blit, render-pass work, or command-list execution) and is not a usable "
                "frame capture. The .rdc was preserved; retry while the target is actively "
                "rendering and verify the selected process or child target. "
                f"diagnostics={json.dumps(diagnostics, sort_keys=True)}"
            )
        validations.append(
            {
                key: details[key]
                for key in (
                    "capture_file",
                    "size_bytes",
                    "driver",
                    "thumbnail",
                    "chunk_count",
                    "draw_dispatch_count",
                    "frame_work_count",
                    "present_count",
                    "frame_content_status",
                )
            }
        )
    return validations


def _prepare_output(output_file: str, expected_suffixes: Iterable[str]) -> Path:
    path = Path(output_file).expanduser().resolve()
    if path.suffix.lower() not in set(expected_suffixes):
        choices = ", ".join(sorted(expected_suffixes))
        raise RenderDocError(f"Output must use one of these extensions: {choices}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def export_thumbnail(
    capture_file: str,
    output_file: str,
    *,
    max_size: int = 0,
    command: Optional[str] = None,
) -> dict[str, Any]:
    capture = _require_capture(capture_file)
    output = _prepare_output(output_file, {".bmp", ".jpg", ".png", ".tga"})
    _run(
        ["thumb", "--out", str(output), "--max-size", str(max_size), str(capture)],
        timeout_secs=120,
        command=command,
    )
    if not output.is_file():
        raise RenderDocError("RenderDoc reported success but did not create the thumbnail")
    return {
        "capture_file": str(capture),
        "output_file": str(output),
        "size_bytes": output.stat().st_size,
    }


def export_timeline(
    capture_file: str,
    output_file: str,
    *,
    command: Optional[str] = None,
) -> dict[str, Any]:
    capture = _require_capture(capture_file)
    output = _prepare_output(output_file, {".json"})
    _run(
        [
            "convert",
            "--filename",
            str(capture),
            "--output",
            str(output),
            "--convert-format",
            "chrome.json",
        ],
        timeout_secs=180,
        command=command,
    )
    if not output.is_file():
        raise RenderDocError("RenderDoc reported success but did not create the timeline")
    return {
        "capture_file": str(capture),
        "output_file": str(output),
        "size_bytes": output.stat().st_size,
    }
