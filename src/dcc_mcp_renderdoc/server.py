"""Standalone RenderDoc MCP server lifecycle."""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

from dcc_mcp_core import DccServerOptions
from dcc_mcp_core.server_base import DccServerBase

from .__version__ import __version__
from .runtime import RenderDocError, get_version

DEFAULT_PORT = 8765
_server: Optional["RenderDocMcpServer"] = None


class RenderDocMcpServer(DccServerBase):
    """Headless DCC-MCP adapter backed by renderdoccmd."""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        os.environ.setdefault("DCC_MCP_PYTHON_EXECUTABLE", sys.executable)
        options = DccServerOptions.from_env(
            "renderdoc",
            Path(__file__).resolve().parent / "skills",
            port=port,
            server_name="dcc-mcp-renderdoc",
            server_version=__version__,
        )
        super().__init__(options=options)

    def _version_string(self) -> str:
        try:
            return get_version()["version_output"]
        except RenderDocError:
            return "RenderDoc CLI unavailable"


def start_server(port: Optional[int] = None) -> RenderDocMcpServer:
    global _server
    if _server is None or not _server.is_running:
        selected_port = (
            port
            if port is not None
            else int(os.environ.get("DCC_MCP_RENDERDOC_PORT", DEFAULT_PORT))
        )
        _server = RenderDocMcpServer(selected_port)
        _server.register_builtin_actions()
        _server.start()
    return _server


def stop_server() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def main() -> None:
    """Run until interrupted."""
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    start_server()
    try:
        stopped.wait()
    finally:
        stop_server()


if __name__ == "__main__":
    main()
