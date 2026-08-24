"""Machine-readable preflight and verification for the RenderDoc adapter."""

from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from .__version__ import __version__
from .downloader import PINNED_VERSION, _configured_bundle, probe_runtime

MIN_CORE_VERSION = "0.19.45"
MIN_RENDERDOC_VERSION = "1.20"
PREFLIGHT_EXIT = 10
VERIFY_EXIT = 40


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.search(r"(?<![0-9])v?([0-9]+(?:\.[0-9]+)+)", value, re.IGNORECASE)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _meets_floor(value: Optional[str], minimum: str) -> bool:
    installed = _version_tuple(value or "")
    floor = _version_tuple(minimum)
    if not installed:
        return False
    width = max(len(installed), len(floor))
    return installed + (0,) * (width - len(installed)) >= floor + (0,) * (width - len(floor))


def _find_command(explicit: Optional[str]) -> tuple[Optional[Path], Optional[str]]:
    configured = explicit or os.environ.get("DCC_MCP_RENDERDOC_CMD")
    if configured:
        path = Path(configured).expanduser().resolve()
        return (path, "explicit" if explicit else "environment") if path.is_file() else (None, None)
    for name in ("renderdoccmd.exe", "renderdoccmd"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve(), "path"
    return None, None


def _core_version() -> Optional[str]:
    try:
        return importlib.metadata.version("dcc-mcp-core")
    except importlib.metadata.PackageNotFoundError:
        return None


def _prerequisite(identifier: str, ok: bool, **details: Any) -> dict[str, Any]:
    return {"id": identifier, "required": True, "ok": ok, **details}


def _next_step(
    name: str,
    description: str,
    *,
    url: Optional[str] = None,
    command: Optional[list[str]] = None,
    why: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "id": name,
        "description": description,
        "why": why or "The reported prerequisite is required before RenderDoc is usable.",
        "url": url,
        "command": command or ["dcc-mcp-renderdoc", "install", "--json"],
        "requires_live_instance": False,
    }


def build_report(operation: str, *, command: Optional[str] = None) -> dict[str, Any]:
    """Return the Install SOP v1 report without downloading or starting a server."""
    if operation not in {"doctor", "verify"}:
        raise ValueError("operation must be doctor or verify")

    executable, command_source = _find_command(command)
    runtime_probe: Optional[dict[str, str]] = None
    version_error: Optional[str] = None
    if executable is not None:
        try:
            runtime_probe = probe_runtime(executable)
        except RuntimeError:
            version_error = "runtime_probe_failed"
    renderdoc_version = runtime_probe.get("renderdoccmd_version") if runtime_probe else None
    core_version = _core_version()
    core_ok = _meets_floor(core_version, MIN_CORE_VERSION)
    renderdoc_ok = _meets_floor(renderdoc_version, MIN_RENDERDOC_VERSION)
    platform_ok = sys.platform == "win32" or sys.platform.startswith("linux")

    qrenderdoc_name = (
        "qrenderdoc.exe"
        if executable is not None and executable.name.casefold().endswith(".exe")
        else "qrenderdoc"
    )
    qrenderdoc = executable.with_name(qrenderdoc_name) if executable is not None else None
    qrenderdoc_ok = qrenderdoc is not None and qrenderdoc.is_file() and runtime_probe is not None
    display_configured = sys.platform == "win32" or (
        sys.platform.startswith("linux")
        and bool(
            os.environ.get("DISPLAY")
            or os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("QT_QPA_PLATFORM")
            or shutil.which("xvfb-run")
        )
    )

    configured_port = os.environ.get("DCC_MCP_RENDERDOC_PORT")
    port_ok = configured_port is None or (
        configured_port.isdigit() and 0 <= int(configured_port) <= 65535
    )
    try:
        _configured_bundle()
        pin_configuration_ok = True
    except RuntimeError:
        pin_configuration_ok = False
    prerequisites = [
        _prerequisite("supported_platform", platform_ok, value=sys.platform),
        _prerequisite("core", core_ok, found=core_version is not None),
        _prerequisite(
            "renderdoccmd",
            executable is not None and renderdoc_ok,
            found=executable is not None,
            source=command_source,
            version_error=version_error,
        ),
        _prerequisite("qrenderdoc", qrenderdoc_ok, found=qrenderdoc_ok),
        _prerequisite("display", display_configured, configured=display_configured),
        _prerequisite("endpoint_configuration", port_ok, configured_port=configured_port),
        _prerequisite("download_integrity_configuration", pin_configuration_ok),
    ]
    directly_usable = all(item["ok"] for item in prerequisites)
    exit_code = 0 if directly_usable else (PREFLIGHT_EXIT if operation == "doctor" else VERIFY_EXIT)

    next_steps: list[dict[str, Any]] = []
    if not platform_ok:
        next_steps.append(
            _next_step(
                "use-supported-platform",
                "Use a Windows or Linux host; RenderDoc has no macOS desktop runtime.",
            )
        )
    if not core_ok:
        next_steps.append(
            _next_step(
                "upgrade-core",
                f"Install dcc-mcp-core {MIN_CORE_VERSION} or newer in this Python environment.",
                command=[
                    "python",
                    "-m",
                    "pip",
                    "install",
                    f"dcc-mcp-core>={MIN_CORE_VERSION},<1.0.0",
                ],
            )
        )
    if executable is None:
        next_steps.append(
            _next_step(
                "install-renderdoc",
                f"Install RenderDoc {MIN_RENDERDOC_VERSION} or newer from a trusted source.",
                url="https://renderdoc.org/",
            )
        )
        next_steps.append(
            _next_step(
                "configure-renderdoc-command",
                "Put renderdoccmd on PATH or set DCC_MCP_RENDERDOC_CMD to its exact path.",
            )
        )
    elif not renderdoc_ok:
        next_steps.append(
            _next_step(
                "upgrade-renderdoc",
                f"Upgrade RenderDoc to {MIN_RENDERDOC_VERSION} or newer.",
                url="https://renderdoc.org/",
            )
        )
    if not qrenderdoc_ok:
        next_steps.append(
            _next_step(
                "install-qrenderdoc",
                "Install qrenderdoc from the same distribution beside renderdoccmd.",
                url="https://renderdoc.org/",
            )
        )
    if platform_ok and sys.platform.startswith("linux") and not display_configured:
        next_steps.append(
            _next_step(
                "configure-display",
                "Configure DISPLAY, WAYLAND_DISPLAY, or a working QT_QPA_PLATFORM.",
            )
        )
    if not port_ok:
        next_steps.append(
            _next_step(
                "configure-port",
                "Set DCC_MCP_RENDERDOC_PORT to an integer from 0 through 65535.",
            )
        )
    if not pin_configuration_ok:
        next_steps.append(
            _next_step(
                "configure-integrity-pin",
                "Set version, exact official stable URL, and SHA-256 together, or clear all "
                "three override variables to use the built-in pin.",
            )
        )
    if not directly_usable:
        next_steps.append(
            _next_step(
                "verify-renderdoc",
                "Re-run machine-readable verification after applying the remediation steps.",
                command=["dcc-mcp-renderdoc", "verify", "--json"],
            )
        )

    pin_names = (
        "DCC_MCP_RENDERDOC_VERSION",
        "DCC_MCP_RENDERDOC_URL",
        "DCC_MCP_RENDERDOC_SHA256",
    )
    operator_pin_configured = all(os.environ.get(name) for name in pin_names)
    return {
        "schema_version": 1,
        "dcc_type": "renderdoc",
        "adapter_version": __version__,
        "core_version": core_version or "unknown",
        "steps": [
            {
                "id": item["id"],
                "status": "ok" if item["ok"] else "failed",
            }
            for item in prerequisites
        ],
        "receipt_path": None,
        "verify": {
            "directly_usable": directly_usable,
            "failure_stage": None if directly_usable else "preflight",
            "failure_reason": None if directly_usable else "prerequisite_failed",
        },
        "adapter": "renderdoc",
        "operation": operation,
        "status": "ok" if directly_usable else "failed",
        "directly_usable": directly_usable,
        "exit_code": exit_code,
        "versions": {
            "adapter": {"installed": __version__, "ok": True},
            "core": {
                "installed": core_version,
                "minimum": MIN_CORE_VERSION,
                "ok": core_ok,
            },
            "renderdoc": {
                "installed": renderdoc_version,
                "minimum": MIN_RENDERDOC_VERSION,
                "ok": renderdoc_ok,
            },
        },
        "config": {
            "command_source": command_source,
            "auto_download": os.environ.get("DCC_MCP_RENDERDOC_AUTO_DOWNLOAD", "1").lower()
            not in {"0", "false", "no"},
            "pinned_download_configured": pin_configuration_ok,
            "download_pin_source": "operator" if operator_pin_configured else "built_in",
            "download_version": (
                os.environ.get("DCC_MCP_RENDERDOC_VERSION")
                if operator_pin_configured
                else PINNED_VERSION
            ),
            "api_endpoint": "http://127.0.0.1:9765/mcp",
            "authentication": "not_required_for_loopback",
            "runtime_probe": runtime_probe,
        },
        "prerequisites": prerequisites,
        "next_steps": next_steps,
    }
