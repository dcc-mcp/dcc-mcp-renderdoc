---
name: renderdoc-analysis
description: >-
  Domain skill — Inspect an existing RenderDoc capture and export its embedded thumbnail, Chrome
  trace, or drawcall texture resources, and analyse what the frame contains: sample a target region
  on a grid, scan it for NaN, Inf, negative, and out-of-band values, read a whole frame's structure
  and signals in one replay, snapshot everything one draw executed with, break the frame into passes
  and per-pass load, diff adjacent draws' pipeline state for redundant switches, and time each pass
  from the GPU duration counter. Use for offline graphics triage, automation artifacts, and finding
  where a frame starts producing garbage or spending its time. Not for launching a capture — use
  renderdoc-capture. Not for why one pixel or vertex has the value it has — use renderdoc-debug.
license: MIT
compatibility: "RenderDoc 1.45+; dcc-mcp-core 0.20.14+; qrenderdoc beside renderdoccmd for the analysis tools"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: renderdoc
    layer: domain
    version: "0.2.0"
    search-hint: "RenderDoc inspect rdc chunks thumbnail Chrome trace graphics analysis sample region NaN Inf pixel diagnosis frame overview draw call state pipeline shader bindings render passes state changes redundant switches batching pass timing"
    tags: "renderdoc,analysis,thumbnail,timeline,pixel-diagnosis,frame-overview,draw-state,render-passes,state-changes,pass-timing,graphics-debugging"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# RenderDoc Analysis

Inspect before exporting. These tools never modify the input `.rdc`; exports require an explicit
destination path and create its parent directory when needed.

Use `export_drawcall_resources` with an exact event ID to export only texture resources that the
pixel shader reports as used at that event. The result records binding, resource name, dimensions,
format, and output file for each PNG.

Inspection reports `draw_dispatch_count`, `frame_work_count`, `present_count`, and
`frame_content_status` so a structurally readable capture with no rendering work is not mistaken
for a usable frame.

## Two capability tiers

This skill spans both RenderDoc backends, so check which one a tool needs before you call it:

| Tool | Backend | Answers |
| --- | --- | --- |
| `inspect_capture` | `renderdoccmd` | whether the file is structurally readable at all |
| `export_thumbnail`, `export_timeline`, `convert_capture` | `renderdoccmd` | artifacts written to a path you name |
| `export_drawcall_resources` | `renderdoc.pyd` | the textures one event actually used |
| `sample_pixel_region`, `diagnose_pixel_values` | `renderdoc.pyd` | what the pixels in a region contain |
| `get_frame_overview` | `renderdoc.pyd` | what the frame contains, in one replay |
| `get_draw_call_state` | `renderdoc.pyd` | what one draw executed with |
| `analyze_render_passes` | `renderdoc.pyd` | the pass structure and each pass's load, no counter needed |
| `analyze_state_changes` | `renderdoc.pyd` | which state switches between adjacent draws were avoidable |
| `get_pass_timing` | `renderdoc.pyd` + a timing counter | how long each pass took |

The `renderdoccmd` baseline can convert and export, but it cannot read data back out of a capture.
Every tool that needs readback lives in the second tier and is gated on the **deep replay backend**
(`renderdoc.pyd`, reached through the Python runtime bundled with `qrenderdoc`). `get_pass_timing`
additionally needs a GPU duration counter, which is a property of the driver, not of the install.

When the backend is missing, those tools return a structured `unsupported_backend` result naming
the backend and how to enable it. When the backend is there but the driver exposes no duration
counter, `get_pass_timing` returns `unsupported_capture` with the counters that *are* available
instead of a frame of zero-duration passes. None of them raise, and none of them return empty data
silently. Call `renderdoc_inspect__replay_capabilities` first when you are unsure which tier you are
on, and `renderdoc_perf__perf_capabilities` before `get_pass_timing`.

## Working order

1. `inspect_capture` — confirm the file is readable before paying for a replay.
2. `get_frame_overview` — structure, resources, and the signals worth chasing, in one replay.
3. `analyze_render_passes` — where the frame's work clusters, and which pass owns a target.
4. `get_pass_timing` — how long each of those passes took, when the driver has a duration counter.
5. `analyze_state_changes` — which of the switches between adjacent draws were avoidable.
6. `get_draw_call_state` — everything one suspicious draw was running with.
7. `sample_pixel_region` — what the target actually contains at that event.
8. `diagnose_pixel_values` — where in the target the values stop making sense.
9. `renderdoc_debug__pixel_history` — why one pixel ended up that way.

## What is measured and what is estimated

Three of these tools report values that are derived rather than read, and each one says so in its
payload (`estimate`, `estimate_method`, or `estimate_fields`) as well as in its summary. Read that
before quoting a number:

- **`sample_pixel_region`** reads the texels its grid lands on. A grid coarser than the region makes
  the min, max, and mean a sample of the region, not a census of it. A grid that covers every texel
  reports `estimate: false`. Values are decoded as floats for every format, so a 64-bit integer
  channel comes back rounded.
- **`diagnose_pixel_values`** strides over a region larger than `max_texels` (two million by
  default), and the counts are then a sample of that region. The stride is reported as
  `region.step`.
- **`get_frame_overview`** and **`analyze_render_passes`** derive pass triangle counts from the API
  draw parameters (`numIndices / 3 * numInstances`), which is before culling, clipping, and vertex
  shading. Both list it in `estimate_fields`; the overview's signals are listed there too, because
  they are thresholds over the capture's structure.
- **`get_pass_timing`** reports the pass event's own counter sample when the driver sampled it, and
  the sum of the pass's timed actions when it did not. The method is reported per pass in
  `duration_method` and `derived`, so a summed number is never read as a measured one.

Two more limits are about the format, not the estimate: a block-compressed, packed, or
special-encoded texture cannot be sampled at all, and both pixel tools report that as an
unsupported result with the reason instead of sampled zeros. Likewise, NaN and Inf only exist in a
floating-point format, so `diagnose_pixel_values` reports those checks as not applicable for an
integer format, with the reason, rather than as zero anomalies.

`analyze_state_changes` reads one pipeline state per draw, so it is bounded by `max_events` (64 by
default): it looks at the first N draws of the range, not the whole frame, and reports
`event_count_truncated` when there were more.

Replay runs in the Python interpreter bundled with `qrenderdoc`. On headless Linux, run the adapter
under Xvfb or provide another working X/Wayland display; the official archive does not include Qt's
`offscreen` platform plugin.
