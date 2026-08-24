from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Test modules import the adapter (and therefore dcc-mcp-core) during collection.
# Isolate that import from any developer or runner gateway before collection starts.
_SESSION_REGISTRY = tempfile.TemporaryDirectory(prefix="dcc-mcp-renderdoc-pytest-")
os.environ["DCC_MCP_GATEWAY_PORT"] = "0"
os.environ["DCC_MCP_REGISTRY_DIR"] = _SESSION_REGISTRY.name


@pytest.fixture(autouse=True)
def _isolated_core_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give every ordinary test its own ephemeral Core registry and port."""
    registry = tmp_path / "dcc-mcp-registry"
    registry.mkdir()
    monkeypatch.setenv("DCC_MCP_GATEWAY_PORT", "0")
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(registry))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del session, exitstatus
    _SESSION_REGISTRY.cleanup()
