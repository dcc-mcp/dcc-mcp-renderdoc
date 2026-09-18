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

from .capabilities import group_for, probe, require
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
    "get_pixel_history",
    "debug_pixel",
    "get_counters",
    "get_debug_messages",
    "run_python_script",
)


def describe_capabilities(command: Optional[str] = None) -> dict[str, Any]:
    """Report the reachable RenderDoc backends and the capabilities they unlock."""
    return probe(command)


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
