"""Host-side driver for the bundled RenderDoc replay bridge.

The deep replay tools (resources, pipeline state, shaders, pixel history,
counters, and script execution) need RenderDoc's replay API, which is only
available inside the Python runtime bundled with ``qrenderdoc``. This module
launches :mod:`dcc_mcp_renderdoc._replay_bridge` in that runtime, passes one
operation and one JSON parameter document through the environment, and reads
the resulting status file back.

The contract mirrors the Target Control and resource export bridges: the child
process is the only component allowed to touch RenderDoc state, and it reports
through a single schema-versioned JSON document.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from .capabilities import (
    DEBUG_FLAGS,
    DEBUG_TOOLS,
    DEEP_BACKEND,
    ENABLE_HINT,
    group_for,
    probe,
    require,
    unsupported_message,
)
from .runtime import (
    RenderDocError,
    _configure_qrenderdoc_environment,
    _read_diagnostic_tail,
    _require_capture,
    _resolve_qrenderdoc,
)

REPLAY_OPERATIONS = (
    "describe_capture",
    "list_actions",
    "get_action",
    "list_resources",
    "get_resource_usage",
    "get_pipeline_state",
    "get_shader_info",
    "get_texture_data",
    "get_buffer_data",
    "get_mesh_data",
    "export_mesh",
    "pick_pixel",
    "get_pixel_history",
    "debug_pixel",
    "debug_vertex",
    "debug_thread",
    "get_counters",
    "get_debug_messages",
    "run_python_script",
)


def describe_capabilities(command: Optional[str] = None) -> dict[str, Any]:
    """Report the reachable RenderDoc backends and the capabilities they unlock."""
    return probe(command)


def unsupported_backend(group: str, command: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Report a capability group that needs a backend this host cannot reach.

    Returns ``None`` when the group is available, so callers can gate on the
    result without probing twice.
    """
    status = probe(command)
    if status["capabilities"].get(group):
        return None
    return {
        "supported": False,
        "capability_group": group,
        "backend": DEEP_BACKEND,
        "reason": status["deep"]["reason"],
        "hint": ENABLE_HINT,
        "error_message": unsupported_message(group, status),
    }


def debug_capabilities(
    capture_file: Optional[str] = None, command: Optional[str] = None
) -> dict[str, Any]:
    """Report the debug backend, its per-capture flags, and what each tool needs.

    Pass ``capture_file`` to also open the capture and read RenderDoc's own
    per-capture replay flags (``pixel_history``, ``shader_debugging``,
    ``post_vs_data``). Without it the report covers the backend only and leaves
    each flag state ``None``.
    """
    status = probe(command)
    flags = None
    capture = None
    if capture_file and status["capabilities"].get("debug"):
        described = run_replay_operation(capture_file, "describe_capture", command=command)
        flags = described["result"].get("capabilities") or {}
        capture = {"capture_file": described["capture_file"], "flags": flags}
    tools = {}
    for tool, operation in DEBUG_TOOLS.items():
        flag = DEBUG_FLAGS.get(tool)
        backend_available = bool(status["capabilities"].get("debug"))
        flag_state = None if (flag is None or flags is None) else bool(flags.get(flag))
        tools[tool] = {
            "operation": operation,
            "requires_flag": flag,
            "backend_available": backend_available,
            "flag_state": flag_state,
            "supported": backend_available and (flag_state is None or flag_state),
        }
    return {
        "baseline": status["baseline"],
        "deep": status["deep"],
        "capabilities": status["capabilities"],
        "capture": capture,
        "capture_checked": capture is not None,
        "tools": tools,
        "hint": status["hint"],
    }


def run_debug_operation(
    capture_file: str,
    operation: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    timeout_secs: int = 300,
    command: Optional[str] = None,
) -> dict[str, Any]:
    """Run one debug-group replay operation, or report the backend as unreachable.

    The debug tools must never crash when ``renderdoc.pyd`` is absent, so an
    unreachable backend comes back as a structured report for the caller to turn
    into an explicit "unsupported, here is how to enable it" result instead of an
    exception.
    """
    unsupported = unsupported_backend("debug", command=command)
    if unsupported is not None:
        return unsupported
    return run_replay_operation(
        capture_file, operation, params, timeout_secs=timeout_secs, command=command
    )


def clean_params(**values: Any) -> dict[str, Any]:
    """Drop unset optional arguments before they reach the replay bridge."""
    return {key: value for key, value in values.items() if value is not None}


def _validate_replay_status(status: Any, operation: str) -> dict[str, Any]:
    fields = {"schema_version", "operation", "result", "error"}
    if not isinstance(status, dict) or set(status) != fields:
        raise RenderDocError("RenderDoc replay returned an invalid status schema")
    if type(status["schema_version"]) is not int or status["schema_version"] != 1:
        raise RenderDocError("RenderDoc replay returned an unsupported status version")
    if status["operation"] != operation:
        raise RenderDocError(
            f"RenderDoc replay returned status for {status['operation']!r} instead of {operation!r}"
        )
    if status["error"] is not None and not isinstance(status["error"], str):
        raise RenderDocError("RenderDoc replay returned an invalid error field")
    if status["error"] is not None:
        raise RenderDocError(f"RenderDoc replay failed: {status['error'] or 'empty error'}")
    if not isinstance(status["result"], dict):
        raise RenderDocError("RenderDoc replay did not return an object result")
    return status


def run_replay_operation(
    capture_file: str,
    operation: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    timeout_secs: int = 300,
    command: Optional[str] = None,
) -> dict[str, Any]:
    """Run one replay operation against a capture and return its result."""
    if operation not in REPLAY_OPERATIONS:
        raise RenderDocError(f"unknown RenderDoc replay operation: {operation}")
    require(group_for(operation), command=command)
    capture = _require_capture(capture_file)
    payload = dict(params or {})
    qrenderdoc = _resolve_qrenderdoc(command)
    script_path = Path(__file__).with_name("_replay_bridge.py")
    if not script_path.is_file():
        raise RenderDocError(f"Bundled replay bridge is missing: {script_path}")

    with tempfile.TemporaryDirectory(prefix="dcc-mcp-renderdoc-replay-") as directory:
        root = Path(directory)
        status_path = root / "status.json"
        params_path = root / "params.json"
        stdout_path = root / "qrenderdoc.stdout.log"
        stderr_path = root / "qrenderdoc.stderr.log"
        try:
            params_path.write_text(json.dumps(payload), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            raise RenderDocError(
                f"RenderDoc replay parameters must be JSON-serializable: {exc}"
            ) from exc
        environment = os.environ.copy()
        environment.update(
            {
                "DCC_MCP_RENDERDOC_CAPTURE": str(capture),
                "DCC_MCP_RENDERDOC_OPERATION": operation,
                "DCC_MCP_RENDERDOC_REPLAY_PARAMS": str(params_path),
                "DCC_MCP_RENDERDOC_REPLAY_STATUS": str(status_path),
            }
        )
        _configure_qrenderdoc_environment(root, environment)
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
                    timeout=timeout_secs,
                    shell=False,
                    env=environment,
                )
        except subprocess.TimeoutExpired as exc:
            raise RenderDocError(
                f"RenderDoc replay timed out after {timeout_secs}s: "
                f"{_read_diagnostic_tail(stderr_path) or _read_diagnostic_tail(stdout_path)}"
            ) from exc
        if result.returncode != 0:
            detail = (
                _read_diagnostic_tail(stderr_path)
                or _read_diagnostic_tail(stdout_path)
                or "no host output"
            )
            raise RenderDocError(
                f"RenderDoc replay host exited with code {result.returncode}: {detail}"
            )
        if not status_path.is_file():
            raise RenderDocError(
                "RenderDoc replay did not write status: "
                f"{_read_diagnostic_tail(stderr_path) or _read_diagnostic_tail(stdout_path)}"
            )
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RenderDocError("RenderDoc replay wrote malformed status JSON") from exc

    status = _validate_replay_status(status, operation)
    return {
        "capture_file": str(capture),
        "operation": operation,
        "result": status["result"],
    }
