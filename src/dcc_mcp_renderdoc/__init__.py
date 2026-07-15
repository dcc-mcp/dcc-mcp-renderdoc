"""RenderDoc adapter for DCC-MCP."""

from .__version__ import __version__
from .server import RenderDocMcpServer

__all__ = ["RenderDocMcpServer", "__version__"]
