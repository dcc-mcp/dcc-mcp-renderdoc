"""Small, typed wrapper around RenderDoc's official command-line client."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence


class RenderDocError(RuntimeError):
    """Raised when a RenderDoc operation cannot satisfy its contract."""


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
    raise RenderDocError(
        "renderdoccmd was not found; install RenderDoc or set DCC_MCP_RENDERDOC_CMD"
    )


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
        detail = (result.stderr or result.stdout or "unknown error").strip()[-2000:]
        launched = re.search(r"Launched as ID (\d+)", detail)
        if not (
            accept_launched_id
            and launched is not None
            and int(launched.group(1)) == result.returncode
        ):
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
    result = _run(
        cli_args,
        timeout_secs=timeout_secs,
        command=command,
        accept_launched_id=not effective_wait_for_exit,
    )
    target_pid: Optional[int] = None
    if trigger_after_secs is not None:
        time.sleep(max(0.0, trigger_after_secs))
        target_pid = _wait_for_visible_process(
            trigger_process_name or target.name,
            min(capture_wait_secs, 30),
        )
        focused = _trigger_capture_hotkey(target_pid)
        captures = _wait_for_captures(output.parent, before, capture_wait_secs)
    else:
        focused = False
        captures = _new_captures(output.parent, before)
    if not captures:
        output_detail = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )[-2000:]
        diagnostics = _capture_failure_diagnostics(
            process_id=target_pid,
            process_name=trigger_process_name or target.name,
            focused=focused,
        )
        raise RenderDocError(
            "No new .rdc capture appeared; ensure the target presents a frame and was hooked "
            f"before creating its graphics device. {diagnostics}. "
            f"RenderDoc output: {output_detail}"
        )
    return {
        "target": str(target),
        "captures": [str(path) for path in captures],
        "focused_target_window": focused,
        "stdout": result.stdout.strip(),
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
    """Inject into one live process, trigger F12, and return the new capture."""
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
    result = _run(
        cli_args,
        timeout_secs=timeout_secs,
        command=command,
        accept_launched_id=True,
    )
    time.sleep(max(0.0, trigger_after_secs))
    focused = _trigger_capture_hotkey(process_id)
    captures = _wait_for_captures(output.parent, before, capture_wait_secs)
    if not captures:
        output_detail = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )[-2000:]
        diagnostics = _capture_failure_diagnostics(
            process_id=process_id,
            process_name=None,
            focused=focused,
        )
        raise RenderDocError(
            "No .rdc capture appeared after injection and F12; ensure the target window is visible "
            f"and uses a graphics API supported by RenderDoc. {diagnostics}. "
            f"RenderDoc output: {output_detail}"
        )
    return {
        "process_id": process_id,
        "captures": [str(path) for path in captures],
        "focused_target_window": focused,
        "stdout": result.stdout.strip(),
    }


def _new_captures(directory: Path, before: set[Path]) -> list[Path]:
    return sorted(
        (path.resolve() for path in directory.glob("*.rdc") if path.resolve() not in before),
        key=lambda path: path.stat().st_mtime_ns,
    )


def _wait_for_captures(directory: Path, before: set[Path], timeout_secs: int) -> list[Path]:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        captures = _new_captures(directory, before)
        if captures:
            return captures
        time.sleep(0.25)
    return []


def _trigger_capture_hotkey(process_id: Optional[int] = None) -> bool:
    if sys.platform != "win32":
        raise RenderDocError("Automatic F12 capture triggering is currently supported on Windows")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    target = wintypes.HWND()
    if process_id is not None:
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def find_window(window, _extra):
            nonlocal target
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
            if owner.value == process_id and user32.IsWindowVisible(window):
                target = window
                return False
            return True

        user32.EnumWindows(find_window, 0)
        if target:
            user32.SetForegroundWindow(target)
            time.sleep(0.15)
    user32.keybd_event(0x7B, 0, 0, 0)
    user32.keybd_event(0x7B, 0, 2, 0)
    return bool(target)


def _wait_for_visible_process(process_name: str, timeout_secs: int) -> Optional[int]:
    if sys.platform != "win32":
        return None
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        process_id = _visible_process_id(process_name)
        if process_id is not None:
            return process_id
        time.sleep(0.25)
    return None


def _visible_process_id(process_name: str) -> Optional[int]:
    expected = Path(process_name).name.casefold()
    for process in _visible_processes():
        if process["name"].casefold() == expected:
            return int(process["process_id"])
    return None


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
