"""Replay backend detection and capability gating.

RenderDoc exposes its capabilities through two different entry points, and only
some of them can be driven headlessly:

* ``renderdoccmd`` is the headless baseline. It covers capture injection,
  thumbnails, format conversion, and the remote server. It can only *replay* a
  capture, never read data back out of one.
* ``renderdoc.pyd`` is the replay library that carries the deep capabilities
  (resources, pipeline state, shaders, pixel history, counters). It is not
  importable by the adapter's own interpreter, so the adapter reaches it through
  the Python runtime bundled with ``qrenderdoc``.

This module decides which of those backends is reachable and reports the result
as capability flags. Callers gate deep operations on the flags instead of
crashing when a full RenderDoc build is absent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, Optional

from .runtime import RenderDocError, resolve_renderdoccmd

BASELINE_BACKEND = "renderdoccmd"
DEEP_BACKEND = "renderdoc.pyd"
DEEP_BACKEND_DRIVER = "qrenderdoc-bridge"

#: Deep replay operations, grouped by the capability they need.
CAPABILITY_GROUPS: Dict[str, tuple] = {
    "inspect": (
        "describe_capture",
        "list_actions",
        "get_action",
        "list_resources",
        "get_resource_usage",
        "get_pipeline_state",
        "get_shader_info",
        "get_texture_data",
        "get_buffer_data",
    ),
    "debug": (
        "pick_pixel",
        "get_pixel_history",
        "debug_pixel",
        "debug_vertex",
        "debug_thread",
        "export_mesh",
        "get_mesh_data",
    ),
    "perf": (
        "describe_perf",
        "get_counters",
        "get_debug_messages",
        "get_action_timing",
        "get_overdraw",
    ),
    "ext": ("run_python_script",),
}

#: Feature-level capability flags, reported alongside the coarse groups above.
#: Every feature is gated on the same deep backend, but the finer keys let an
#: agent ask "can I read counters here?" instead of "is the perf group up?" —
#: which is the question a caller actually has before it pays for a replay.
FEATURE_GROUPS: Dict[str, tuple] = {
    "counters": ("get_counters",),
    "messages": ("get_debug_messages",),
    "timing": ("get_action_timing",),
    "overdraw": ("get_overdraw",),
}

#: The renderdoc-debug skill tools and the deep replay operation each drives.
DEBUG_TOOLS: Dict[str, str] = {
    "pick_pixel": "pick_pixel",
    "pixel_history": "get_pixel_history",
    "debug_pixel": "debug_pixel",
    "debug_vertex": "debug_vertex",
    "debug_thread": "debug_thread",
    "export_mesh": "export_mesh",
}

#: Per-capture replay flag each debug tool needs on top of the deep backend.
#: ``None`` means the backend alone is enough for that tool.
DEBUG_FLAGS: Dict[str, Optional[str]] = {
    # pick_pixel attributes a pixel to a draw through the pixel history, so it
    # needs the same per-capture flag as pixel_history.
    "pick_pixel": "pixel_history",
    "pixel_history": "pixel_history",
    "debug_pixel": "shader_debugging",
    "debug_vertex": "shader_debugging",
    "debug_thread": "shader_debugging",
    "export_mesh": "post_vs_data",
}

#: The renderdoc-perf skill tools and the deep replay operation each drives.
#: ``list_counters`` and ``fetch_counters`` share one operation: the catalogue
#: and its samples are the same query, taken with and without ``fetch``.
PERF_TOOLS: Dict[str, str] = {
    "list_counters": "get_counters",
    "fetch_counters": "get_counters",
    "get_action_timing": "get_action_timing",
    "get_debug_messages": "get_debug_messages",
    "analyze_overdraw": "get_overdraw",
}

#: Per-capture flag each perf tool needs on top of the deep backend. ``None``
#: means the backend alone is enough for that tool.
PERF_FLAGS: Dict[str, Optional[str]] = {
    # Listing counters is answered by the driver's catalogue, which exists even
    # when it is empty; sampling one is not, so only fetching needs the flag.
    "list_counters": None,
    "fetch_counters": "counters",
    "get_action_timing": "timing",
    # Debug messages come from the replay itself, not from a counter or a mesh.
    "get_debug_messages": None,
    "analyze_overdraw": "post_vs_data",
}

ENABLE_HINT = (
    "install the full RenderDoc build that ships qrenderdoc beside renderdoccmd, "
    "or point DCC_MCP_RENDERDOC_CMD at an existing renderdoccmd"
)


def _resolve_deep_host(command: Optional[str]) -> Optional[Path]:
    """Locate the qrenderdoc host that can drive renderdoc.pyd, if present."""
    try:
        renderdoccmd = resolve_renderdoccmd(command)
    except RenderDocError:
        return None
    name = "qrenderdoc.exe" if renderdoccmd.suffix.casefold() == ".exe" else "qrenderdoc"
    qrenderdoc = renderdoccmd.with_name(name)
    return qrenderdoc if qrenderdoc.is_file() else None


def _native_module_importable() -> bool:
    """True when the host interpreter itself can import ``renderdoc``."""
    try:
        return importlib.util.find_spec("renderdoc") is not None
    except (ImportError, ValueError):
        return False


def probe(command: Optional[str] = None) -> Dict[str, Any]:
    """Describe the reachable backends and the capabilities they unlock."""
    baseline_host = None
    baseline_reason = None
    try:
        baseline_host = str(resolve_renderdoccmd(command))
    except RenderDocError as exc:
        baseline_reason = str(exc)

    deep_host = _resolve_deep_host(command)
    native_module = _native_module_importable()
    if deep_host is not None:
        deep_reason = None
    elif native_module:
        deep_reason = (
            "renderdoc.pyd is importable in this interpreter but the adapter reaches deep "
            "replay through qrenderdoc, which was not found"
        )
    else:
        deep_reason = "qrenderdoc was not found beside renderdoccmd"

    deep_available = deep_host is not None
    capabilities = {group: deep_available for group in CAPABILITY_GROUPS}
    capabilities.update({feature: deep_available for feature in FEATURE_GROUPS})
    return {
        "baseline": {
            "backend": BASELINE_BACKEND,
            "available": baseline_host is not None,
            "host": baseline_host,
            "reason": baseline_reason,
        },
        "deep": {
            "backend": DEEP_BACKEND,
            "driver": DEEP_BACKEND_DRIVER if deep_available else None,
            "available": deep_available,
            "host": str(deep_host) if deep_host is not None else None,
            "native_module_importable": native_module,
            "reason": deep_reason,
            "operations": sorted(
                {operation for operations in CAPABILITY_GROUPS.values() for operation in operations}
            ),
        },
        "capabilities": capabilities,
        "hint": ENABLE_HINT,
    }


def group_for(operation: str) -> Optional[str]:
    """Return the capability group that owns ``operation``."""
    for group, operations in CAPABILITY_GROUPS.items():
        if operation in operations:
            return group
    return None


def unsupported_message(group: str, status: Optional[Dict[str, Any]] = None) -> str:
    status = status or {}
    deep = status.get("deep", {}) if isinstance(status.get("deep"), dict) else {}
    reason = deep.get("reason") or "no deep replay backend is reachable"
    return (
        f"RenderDoc capability group {group!r} needs the {DEEP_BACKEND} replay backend, "
        f"which is unavailable: {reason}. Enable it by: {ENABLE_HINT}."
    )


def require(group: Optional[str], command: Optional[str] = None) -> Dict[str, Any]:
    """Return the probe result, or raise an actionable error for a missing backend."""
    status = probe(command)
    if group is None or status["capabilities"].get(group):
        return status
    raise RenderDocError(unsupported_message(group, status))
