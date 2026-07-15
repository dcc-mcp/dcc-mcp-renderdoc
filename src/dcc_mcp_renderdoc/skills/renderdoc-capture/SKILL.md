---
name: renderdoc-capture
description: >-
  Domain skill — Discover RenderDoc and launch an explicit game or graphics executable under
  capture. Use for repeatable local or CI frame capture. Not for reading an existing capture —
  use renderdoc-analysis.
license: MIT
compatibility: "RenderDoc 1.45+; dcc-mcp-core 0.19+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: renderdoc
    layer: domain
    version: "0.1.0"
    search-hint: "RenderDoc capture launch executable game graphics frame rdc"
    tags: "renderdoc,capture,graphics-debugging,game-development"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# RenderDoc Capture

Call `get_version` before capture. `capture_program` starts only the explicit executable and
arguments, never a shell. Set `trigger_after_secs` to trigger F12 automatically on Windows.
When using `hook_children`, set `trigger_process_name` to the child executable that should receive
focus before F12.

Use `capture_process` when Steam or another platform client must launch the target first. It
injects into the explicit PID, focuses that visible window, triggers F12 after the requested delay,
and waits for the resulting capture. Neither tool terminates the target process.
