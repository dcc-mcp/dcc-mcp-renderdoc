---
name: renderdoc-analysis
description: >-
  Domain skill — Inspect an existing RenderDoc capture and export its embedded thumbnail or Chrome
  trace. Use for offline graphics triage and automation artifacts. Not for launching a capture —
  use renderdoc-capture.
license: MIT
compatibility: "RenderDoc 1.45+; dcc-mcp-core 0.19+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: renderdoc
    layer: domain
    version: "0.1.0"
    search-hint: "RenderDoc inspect rdc chunks thumbnail Chrome trace graphics analysis"
    tags: "renderdoc,analysis,thumbnail,timeline,graphics-debugging"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# RenderDoc Analysis

Inspect before exporting. These tools never modify the input `.rdc`; exports require an explicit
destination path and create its parent directory when needed.

Inspection reports `draw_dispatch_count`, `frame_work_count`, `present_count`, and
`frame_content_status` so a structurally readable capture with no rendering work is not mistaken
for a usable frame.
