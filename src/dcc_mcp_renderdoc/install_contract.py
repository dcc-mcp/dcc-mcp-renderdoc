"""Shared Install SOP v1 contract imports with a bounded compatibility copy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from dcc_mcp_core.deployment import INSTALL_EXIT_CODES as INSTALL_EXIT_CODES
    from dcc_mcp_core.deployment import load_install_sop_schema as _load_shared_schema
except (ImportError, ModuleNotFoundError):
    INSTALL_EXIT_CODES = {
        "ok": 0,
        "preflight": 10,
        "acquire": 20,
        "install": 30,
        "verify": 40,
        "requires_restart": 50,
    }
    _load_shared_schema = None

INSTALL_EXIT_OK = INSTALL_EXIT_CODES["ok"]
INSTALL_EXIT_PREFLIGHT = INSTALL_EXIT_CODES["preflight"]
INSTALL_EXIT_ACQUIRE = INSTALL_EXIT_CODES["acquire"]
INSTALL_EXIT_INSTALL = INSTALL_EXIT_CODES["install"]
INSTALL_EXIT_VERIFY = INSTALL_EXIT_CODES["verify"]
INSTALL_EXIT_REQUIRES_RESTART = INSTALL_EXIT_CODES["requires_restart"]


def load_install_sop_schema() -> dict[str, Any]:
    """Load Core's canonical schema or its exact compatibility copy."""
    if _load_shared_schema is not None:
        return _load_shared_schema()
    path = Path(__file__).resolve().parent / "schemas" / "adapter-install-sop-v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "INSTALL_EXIT_ACQUIRE",
    "INSTALL_EXIT_CODES",
    "INSTALL_EXIT_INSTALL",
    "INSTALL_EXIT_OK",
    "INSTALL_EXIT_PREFLIGHT",
    "INSTALL_EXIT_REQUIRES_RESTART",
    "INSTALL_EXIT_VERIFY",
    "load_install_sop_schema",
]
