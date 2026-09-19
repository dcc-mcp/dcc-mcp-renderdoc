---
name: renderdoc-perf
description: >-
  Domain skill — Measure how a RenderDoc capture spends its frame: enumerate the GPU counters a
  driver exposes, sample them over one event range, break the frame into per-action GPU durations
  and per-pass totals, read the debug, warning, and error messages a replay produced, and quantify
  overdraw from post-VS geometry. Use when a capture replays and the question is how expensive it
  is or why it is slow. Not for capture or conversion — use renderdoc-capture. Not for why one
  pixel or vertex has the value it has — use renderdoc-debug.
license: MIT
compatibility: "RenderDoc 1.45+; dcc-mcp-core 0.20.14+; qrenderdoc beside renderdoccmd"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: renderdoc
    layer: domain
    version: "0.1.0"
    search-hint: "RenderDoc performance GPU counters fetch counters action timing duration debug messages overdraw heatmap profile"
    tags: "renderdoc,performance,gpu-counters,action-timing,overdraw,debug-messages,profiling"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# RenderDoc Perf

These tools answer *how expensive* a frame is. They replay an existing `.rdc` through RenderDoc's
replay API and never modify it; only `analyze_overdraw` writes to disk, and only to a destination
you name.

Every tool in this skill needs the **deep replay backend** (`renderdoc.pyd`, reached through the
Python runtime bundled with `qrenderdoc`). The headless `renderdoccmd` baseline cannot read
anything back out of a capture, so it cannot substitute here.

## Capability gating

Call `perf_capabilities` first. It reports:

- whether the deep backend is reachable, and how to enable it when it is not;
- when given `capture_file`, the per-capture facts only a replay can answer — whether this driver
  exposes counters at all (`counters`), whether one of them carries GPU duration (`timing`), and
  whether post-VS geometry can be read back (`post_vs_data`);
- per tool, whether that tool is usable right now and which flag it depends on.

When the deep backend is missing, every tool returns a structured
`unsupported_backend` result naming the missing backend and how to enable it. None of them raise,
and none of them return empty data silently.

| Tool | Needs | Answers |
| --- | --- | --- |
| `list_counters` | — | which GPU counters this driver exposes, with unit and category |
| `fetch_counters` | `counters` | sampled counter values over one event range |
| `get_action_timing` | `timing` | per-action GPU duration, slowest draws, per-pass totals |
| `get_debug_messages` | — | debug, warning, and error messages a replay produced |
| `analyze_overdraw` | `post_vs_data` | how many times the frame shades each pixel, with a heatmap |

## Working order

1. `perf_capabilities` — confirm the backend and the per-capture flags.
2. `list_counters` — see what this driver offers before asking for samples.
3. `fetch_counters` — sample the counters you picked over the event range you care about.
4. `get_action_timing` — turn the timing counter into per-action durations and per-pass totals.
5. `get_debug_messages` — read the warnings and errors the API produced during replay.
6. `analyze_overdraw` — check whether the frame is fill-rate bound.

Two things vary by machine and neither is a bug:

- **Counter catalogues are driver-specific.** NVIDIA, AMD, and Intel expose different counters, and
  so do different APIs. Always `list_counters` before `fetch_counters`; an ID from one machine
  means nothing on another.
- **`get_action_timing` needs a duration counter.** If the driver exposes none, it returns
  `supported: false` with the counters that *are* available rather than an empty timing table, and
  `perf_capabilities` reports `timing: false` up front so you can find that out first.

## Overdraw is an estimate

RenderDoc's own quad-overdraw overlay is a GPU pass driven through a `ReplayOutput`, which needs a
window and therefore cannot run headless. `analyze_overdraw` computes the same quantity — how many
times the frame shades each pixel — by projecting each draw's post-VS triangles onto a pixel grid
and counting coverage. That makes it available headless, and makes it an estimate: it counts
triangle coverage rather than GPU quads, and it shades both windings because the draw's cull mode
is not consulted. `depth_test` adds an approximate depth-only pass.

The default grid is 256x144, not the capture's viewport, because a CPU rasteriser costs one pass
per covered triangle. Raise `width` and `height` for a finer estimate; the result reports
`average_overdraw`, `max_overdraw`, and `fill_ratio` plus a per-draw breakdown so you can find the
draws that cost the most either way.

Replay runs in the Python interpreter bundled with `qrenderdoc`. On headless Linux, run the adapter
under Xvfb or provide another working X/Wayland display; the official archive does not include Qt's
`offscreen` platform plugin.
