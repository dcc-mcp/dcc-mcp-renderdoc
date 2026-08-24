"""Bounded qrenderdoc embedded-Python load probe for managed acquisition."""

import json
import os
import re
import time

import renderdoc  # noqa: F401

json.dumps({"cwd": os.getcwd(), "pattern": re.escape("renderdoc"), "time": time.time()})
with open(os.environ["DCC_MCP_RENDERDOC_PROBE_STATUS"], "x", encoding="utf-8") as status:
    status.write("dcc-mcp-renderdoc-python-probe-ok")
raise SystemExit(0)
