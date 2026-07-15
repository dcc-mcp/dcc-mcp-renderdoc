from pathlib import Path
from subprocess import CompletedProcess

import pytest

from dcc_mcp_renderdoc import runtime


def test_parse_capture_xml_summarizes_stable_fields(tmp_path: Path):
    xml_file = tmp_path / "capture.xml"
    xml_file.write_text(
        """<rdc><header><driver id="2">Vulkan</driver><machineIdent>123</machineIdent>
        <thumbnail width="640" height="360" /></header><chunks version="17">
        <chunk name="vkCmdDraw"/><chunk name="vkCmdDraw"/><chunk name="Present"/>
        </chunks></rdc>""",
        encoding="utf-8",
    )

    result = runtime.parse_capture_xml(str(xml_file), representative_limit=2)

    assert result["driver"] == {"id": "2", "name": "Vulkan"}
    assert result["thumbnail"] == {"width": 640, "height": 360}
    assert result["chunk_count"] == 3
    assert result["chunk_frequencies"][0] == {"name": "vkCmdDraw", "count": 2}
    assert result["representative_chunks"] == ["vkCmdDraw", "vkCmdDraw"]


def test_resolve_renderdoccmd_prefers_explicit_file(tmp_path: Path):
    executable = tmp_path / "renderdoccmd"
    executable.touch()
    assert runtime.resolve_renderdoccmd(str(executable)) == executable.resolve()


def test_capture_program_uses_argument_vector_and_reports_new_capture(tmp_path, monkeypatch):
    target = tmp_path / "game.exe"
    target.touch()
    observed = {}

    def fake_run(arguments, **_kwargs):
        observed["arguments"] = arguments
        (tmp_path / "capture_frame1.rdc").touch()
        return CompletedProcess(arguments, 0, "captured", "")

    monkeypatch.setattr(runtime, "_run", fake_run)
    result = runtime.capture_program(
        str(target),
        str(tmp_path / "capture"),
        arguments=["--scene", "demo"],
    )

    assert observed["arguments"][-3:] == [str(target.resolve()), "--scene", "demo"]
    assert result["captures"] == [str((tmp_path / "capture_frame1.rdc").resolve())]


def test_invalid_capture_is_rejected(tmp_path: Path):
    with pytest.raises(runtime.RenderDocError, match="not .rdc"):
        runtime.inspect_capture(str(tmp_path / "missing.rdc"))
