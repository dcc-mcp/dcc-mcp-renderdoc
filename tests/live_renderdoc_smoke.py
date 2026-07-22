"""Capture and inspect one real OpenGL frame through the MCP endpoint."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

from dcc_mcp_renderdoc.server import RenderDocMcpServer


def _post(url: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def _content(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("error") or response.get("result", {}).get("isError"):
        raise RuntimeError(json.dumps(response, indent=2))
    result = response.get("result", {})
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    if result.get("content"):
        return json.loads(result["content"][0]["text"])
    return result


def _call(url: str, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return _content(_post(url, "tools/call", {"name": name, "arguments": arguments or {}}))


def _wait(url: str, envelope: dict[str, Any]) -> dict[str, Any]:
    job_id = envelope.get("job_id")
    if not job_id:
        return envelope
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        status = _call(url, "jobs_get_status", {"job_id": job_id, "include_result": True})
        if status.get("status") == "completed":
            return status
        if status.get("status") in {"failed", "cancelled", "interrupted"}:
            raise RuntimeError(json.dumps(status, indent=2))
        time.sleep(0.1)
    raise TimeoutError(f"RenderDoc MCP job {job_id} did not finish")


def _tool(url: str, suffix: str) -> str:
    names: list[str] = []
    cursor = None
    for _ in range(20):
        response = _post(url, "tools/list", {"cursor": cursor} if cursor else None)
        result = response.get("result", {})
        names.extend(tool["name"] for tool in result.get("tools", []))
        cursor = result.get("nextCursor")
        if not cursor:
            break
    matches = [name for name in names if name == suffix or name.endswith(f"__{suffix}")]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one tool ending in {suffix!r}, found {matches!r}")
    return matches[0]


def _contains_target_control_trigger(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("trigger_mode") == "target_control":
            return True
        return any(_contains_target_control_trigger(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_target_control_trigger(item) for item in value)
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--renderdoc", required=True)
    parser.add_argument("--program", required=True)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    args = parser.parse_args()

    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    os.environ["DCC_MCP_RENDERDOC_CMD"] = str(Path(args.renderdoc).resolve())
    program = str(Path(args.program).resolve())

    server = RenderDocMcpServer(port=0)
    try:
        server.register_builtin_actions()
        server.start(install_atexit_hook=False)
        url = server.mcp_url
        _call(url, "load_skill", {"skill_name": "renderdoc-capture"})
        _call(url, "load_skill", {"skill_name": "renderdoc-analysis"})
        version = _wait(url, _call(url, _tool(url, "get_version")))
        capture_job = _call(
            url,
            _tool(url, "capture_program"),
            {
                "executable": program,
                "output_template": str(artifacts / "smoke"),
                "arguments": [str(artifacts / "smoke")],
                "working_directory": str(Path(program).parent),
                "trigger_after_secs": 0.2,
                "capture_wait_secs": 60,
            },
        )
        capture_result = _wait(url, capture_job)
        if not _contains_target_control_trigger(capture_result):
            raise RuntimeError("MCP capture did not report trigger_mode=target_control")
        captures = sorted(artifacts.glob("*.rdc"))
        if len(captures) != 1:
            raise RuntimeError(f"Expected exactly one Target Control capture, found {captures}")
        capture = str(captures[0])
        inspection = _wait(
            url,
            _call(url, _tool(url, "inspect_capture"), {"capture_file": capture}),
        )
        _wait(
            url,
            _call(
                url,
                _tool(url, "export_thumbnail"),
                {"capture_file": capture, "output_file": str(artifacts / "smoke.png")},
            ),
        )
        _wait(
            url,
            _call(
                url,
                _tool(url, "export_timeline"),
                {"capture_file": capture, "output_file": str(artifacts / "smoke.json")},
            ),
        )
        assert (artifacts / "smoke.png").stat().st_size > 0
        assert (artifacts / "smoke.json").stat().st_size > 0
        print(json.dumps({"version": version, "inspection_job": inspection}))
    finally:
        server.stop()


if __name__ == "__main__":
    main()
