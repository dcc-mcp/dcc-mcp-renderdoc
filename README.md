# dcc-mcp-renderdoc

RenderDoc capture and replay automation for the DCC Model Context Protocol ecosystem.

The adapter is headless-first: it reuses the official `renderdoccmd` executable for capture and
conversion, so agents can automate graphics regression triage without keeping the RenderDoc GUI
open or installing a second bridge.

## Install

```bash
pip install dcc-mcp-renderdoc
```

Install RenderDoc separately, then expose its command line tool with either `PATH` or:

```bash
export DCC_MCP_RENDERDOC_CMD=/opt/renderdoc/bin/renderdoccmd
dcc-mcp-renderdoc
```

On Windows, set the variable to `renderdoccmd.exe`.

## Agent workflows

- Launch a game or test executable under RenderDoc and wait for a typed `.rdc` capture.
- Inspect capture driver, machine identity, chunk version, API-call counts, and representative calls.
- Export a capture thumbnail for visual review.
- Export Chrome trace JSON for timeline tooling.

The capture tool launches only the explicit executable and arguments supplied by the caller. It
never invokes a shell. Analysis tools are read-only with respect to the `.rdc` input.

## Real CI

CI discovers the current stable RenderDoc build from the official downloads page. It compiles a
small OpenGL program, captures a real frame under Xvfb, calls the MCP analysis tool against the
resulting `.rdc`, and verifies thumbnail and timeline exports.

## Development

```bash
uv sync --extra dev
uv run python -m pytest
uv run ruff check src tests tools
uv run python tools/lint_skills.py
```

RenderDoc is an MIT-licensed graphics debugger maintained independently at
[renderdoc.org](https://renderdoc.org/). This adapter is not affiliated with the RenderDoc project.

