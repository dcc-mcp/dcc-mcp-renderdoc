"""Small, typed wrapper around RenderDoc's official command-line client."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
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
    if wait_for_exit:
        cli_args.append("--wait-for-exit")
    if api_validation:
        cli_args.append("--opt-api-validation")
    if hook_children:
        cli_args.append("--opt-hook-children")
    cli_args.extend([str(target), *(str(value) for value in arguments or [])])
    result = _run(cli_args, timeout_secs=timeout_secs, command=command)

    captures = sorted(
        (path.resolve() for path in output.parent.glob("*.rdc") if path.resolve() not in before),
        key=lambda path: path.stat().st_mtime_ns,
    )
    if not captures:
        output_detail = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )[-2000:]
        raise RenderDocError(
            "The target exited without creating a new .rdc capture; ensure it presents a frame "
            f"or uses RenderDoc's in-application capture API. RenderDoc output: {output_detail}"
        )
    return {
        "target": str(target),
        "captures": [str(path) for path in captures],
        "stdout": result.stdout.strip(),
    }


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
