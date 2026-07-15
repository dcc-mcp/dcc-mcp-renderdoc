from pathlib import Path

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
