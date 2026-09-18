---
name: renderdoc-replay
description: >-
  Domain skill — Replay an existing RenderDoc capture through RenderDoc's official replay API and
  expose its actions, resources, pipeline state, shaders, mesh and pixel data, GPU counters, debug
  messages, and a Python escape hatch. Use for deep graphics debugging of a .rdc you already have.
  Not for launching a capture — use renderdoc-capture.
license: MIT
compatibility: "RenderDoc 1.45+; dcc-mcp-core 0.20.14+; qrenderdoc beside renderdoccmd"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: renderdoc
    layer: domain
    version: "0.1.0"
    search-hint: "RenderDoc replay draw pipeline state shader pixel history counters mesh graphics debugging"
    tags: "renderdoc,replay,pipeline-state,shader,pixel-history,counters,graphics-debugging"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# RenderDoc Replay

These tools replay an existing `.rdc` through RenderDoc's own replay API, so they expose what the
RenderDoc UI can show rather than only what `renderdoccmd` can serialise. Every tool takes
`capture_file` and never modifies it; only `get_texture_data`, `get_buffer_data`, and
`run_python_script` write to disk, and only to a destination you name.

Start with `describe_capture` to learn the capture's size and which replay capabilities are
available, then `list_actions` to find an event ID. `get_pipeline_state`, `get_shader_info`, and
`get_mesh_data` answer what a draw was about to do; `get_pixel_history` and `debug_pixel` explain
what it produced.

Replay runs in the Python interpreter bundled with `qrenderdoc`, so `qrenderdoc` must sit beside
`renderdoccmd`. On headless Linux, run the adapter under Xvfb or provide another working X/Wayland
display; the official archive does not include Qt's `offscreen` platform plugin.

`pixel_history` and `shader_debugging` are replay capabilities, not adapter options. Call
`describe_capture` first on an unfamiliar capture — `get_pixel_history` and `debug_pixel` fail with
an explicit message when the current replay cannot support them.

Large results are paged with `offset` and `limit`; keep `limit` small enough to read. Prefer the
typed tools over `run_python_script`. `run_python_script` executes caller-supplied Python with the
adapter's privileges on the host: use it for capabilities this adapter does not expose as a tool,
and only with scripts you trust.
