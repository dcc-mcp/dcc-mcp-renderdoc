"""Shared Install SOP v1 contract imports from the published Core package."""

from __future__ import annotations

from dcc_mcp_core.deployment import INSTALL_EXIT_CODES as INSTALL_EXIT_CODES
from dcc_mcp_core.deployment import load_install_sop_schema as load_install_sop_schema

INSTALL_EXIT_OK = INSTALL_EXIT_CODES["ok"]
INSTALL_EXIT_PREFLIGHT = INSTALL_EXIT_CODES["preflight"]
INSTALL_EXIT_ACQUIRE = INSTALL_EXIT_CODES["acquire"]
INSTALL_EXIT_INSTALL = INSTALL_EXIT_CODES["install"]
INSTALL_EXIT_VERIFY = INSTALL_EXIT_CODES["verify"]
INSTALL_EXIT_REQUIRES_RESTART = INSTALL_EXIT_CODES["requires_restart"]
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
