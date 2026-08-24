import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from dcc_mcp_renderdoc import __version__


def _windows_adb_pids() -> set[int]:
    if sys.platform != "win32":
        return set()
    completed = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq adb.exe", "/FO", "CSV", "/NH"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    pids = set()
    for row in csv.reader(io.StringIO(completed.stdout)):
        if len(row) >= 2 and row[0].casefold() == "adb.exe":
            pids.add(int(row[1]))
    return pids


def test_server_constructs_with_headless_contract(tmp_path):
    registry = tmp_path / "child-registry"
    env = os.environ.copy()
    env["DCC_MCP_GATEWAY_PORT"] = "0"
    env["DCC_MCP_REGISTRY_DIR"] = str(registry)
    source_root = Path(__file__).parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(source_root), env.get("PYTHONPATH")) if value
    )
    script = """
import json
from dcc_mcp_renderdoc.server import RenderDocMcpServer
server = RenderDocMcpServer(port=0)
try:
    print(json.dumps({
        "server_name": server._options.server_name,
        "dcc_name": server._options.dcc_name,
    }))
finally:
    server.stop()
"""
    adb_before = _windows_adb_pids()

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload == {
        "server_name": "dcc-mcp-renderdoc",
        "dcc_name": "renderdoc",
    }
    assert env["DCC_MCP_GATEWAY_PORT"] == "0"
    assert env["DCC_MCP_REGISTRY_DIR"] == str(registry)

    if sys.platform == "win32":
        new_adb = _windows_adb_pids() - adb_before
        deadline = time.monotonic() + 2
        while new_adb and time.monotonic() < deadline:
            time.sleep(0.1)
            new_adb = _windows_adb_pids() - adb_before
        assert not new_adb, f"server probe leaked adb.exe process(es): {sorted(new_adb)}"


def test_bundled_skills_and_release_workflow_exist():
    root = Path(__file__).parents[1]
    assert (root / "install.md").is_file()
    assert (root / "src" / "dcc_mcp_renderdoc" / "_target_control.py").is_file()
    skills = root / "src" / "dcc_mcp_renderdoc" / "skills"
    assert {path.name for path in skills.iterdir() if path.is_dir()} == {
        "renderdoc-analysis",
        "renderdoc-capture",
    }
    assert (root / ".github" / "workflows" / "release.yml").is_file()


def test_capture_program_accepts_ten_minute_boss_trigger():
    root = Path(__file__).parents[1]
    tools = (
        root / "src" / "dcc_mcp_renderdoc" / "skills" / "renderdoc-capture" / "tools.yaml"
    ).read_text(encoding="utf-8")
    capture_program = re.search(
        r"  - name: capture_program(?P<body>.*?)(?=\n  - name:)", tools, re.DOTALL
    )
    assert capture_program is not None
    maximum = re.search(
        r"trigger_after_secs: \{[^}]*maximum: (?P<seconds>\d+)", capture_program.group("body")
    )
    assert maximum is not None
    assert int(maximum.group("seconds")) >= 612


def test_runtime_version_matches_distribution_metadata():
    root = Path(__file__).parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    project_version = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    assert project_version is not None
    assert __version__ == project_version.group(1)
    lock = (root / "uv.lock").read_text(encoding="utf-8")
    locked_project = re.search(
        r'\[\[package\]\]\s+name = "dcc-mcp-renderdoc"\s+version = "([^"]+)"', lock
    )
    assert locked_project is not None
    assert __version__ == locked_project.group(1)


def test_start_server_defers_port_resolution_to_core(monkeypatch):
    from types import SimpleNamespace

    from dcc_mcp_renderdoc import server as server_module

    ports = []
    stub = SimpleNamespace(
        is_running=False,
        register_builtin_actions=lambda: None,
        start=lambda: None,
        stop=lambda: None,
    )

    monkeypatch.setattr(server_module, "_server", None)
    monkeypatch.setattr(
        server_module, "RenderDocMcpServer", lambda port=None: ports.append(port) or stub
    )
    monkeypatch.setenv("DCC_MCP_RENDERDOC_PORT", "8765")

    server_module.start_server(0)
    server_module.stop_server()
    server_module.start_server()
    server_module.stop_server()

    assert ports == [0, None]
