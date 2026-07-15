import re
from pathlib import Path

from dcc_mcp_renderdoc import __version__
from dcc_mcp_renderdoc.server import RenderDocMcpServer


def test_server_constructs_with_headless_contract():
    server = RenderDocMcpServer(port=0)
    try:
        assert server._options.server_name == "dcc-mcp-renderdoc"
        assert server._options.dcc_name == "renderdoc"
    finally:
        server.stop()


def test_bundled_skills_and_release_workflow_exist():
    root = Path(__file__).parents[1]
    skills = root / "src" / "dcc_mcp_renderdoc" / "skills"
    assert {path.name for path in skills.iterdir() if path.is_dir()} == {
        "renderdoc-analysis",
        "renderdoc-capture",
    }
    assert (root / ".github" / "workflows" / "release.yml").is_file()


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
