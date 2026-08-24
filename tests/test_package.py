import csv
import io
import os
import re
import subprocess
import sys
from pathlib import Path

from dcc_mcp_renderdoc import __version__


def _windows_process_pids(image_name: str) -> set[int]:
    if sys.platform != "win32":
        return set()
    completed = subprocess.run(
        ["tasklist.exe", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    pids = set()
    for row in csv.reader(io.StringIO(completed.stdout)):
        if len(row) >= 2 and row[0].casefold() == image_name.casefold():
            pids.add(int(row[1]))
    return pids


def test_server_constructs_with_headless_contract(monkeypatch, tmp_path):
    from dcc_mcp_core.server_base import DccServerBase

    from dcc_mcp_renderdoc.server import RenderDocMcpServer

    observed = {}

    def capture_options(server, *, options):
        observed["options"] = options
        server._options = options

    monkeypatch.setattr(DccServerBase, "__init__", capture_options)
    watched = ("adb.exe", "qrenderdoc.exe", "renderdoccmd.exe")
    processes_before = {name: _windows_process_pids(name) for name in watched}

    server = RenderDocMcpServer(port=0)

    assert server._options is observed["options"]
    assert server._options.server_name == "dcc-mcp-renderdoc"
    assert server._options.dcc_name == "renderdoc"
    assert os.environ["DCC_MCP_GATEWAY_PORT"] == "0"
    assert Path(os.environ["DCC_MCP_REGISTRY_DIR"]).parent == tmp_path
    for name in watched:
        leaked = _windows_process_pids(name) - processes_before[name]
        assert not leaked, f"headless options probe leaked {name}: {sorted(leaked)}"


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
