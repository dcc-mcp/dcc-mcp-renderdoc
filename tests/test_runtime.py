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


def test_non_waiting_launch_accepts_renderdoc_target_id(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 12345, "Launched as ID 12345", ""),
    )

    result = runtime._run(
        ["capture", "game.exe"],
        timeout_secs=10,
        command=str(command),
        accept_launched_id=True,
    )

    assert result.returncode == 12345


def test_capture_process_injects_triggers_and_reports_capture(tmp_path, monkeypatch):
    observed = {}

    def fake_run(arguments, **kwargs):
        observed["arguments"] = arguments
        observed["accept_launched_id"] = kwargs["accept_launched_id"]
        return CompletedProcess(arguments, 456, "Launched as ID 456", "")

    monkeypatch.setattr(runtime, "_run", fake_run)
    monkeypatch.setattr(runtime, "_trigger_capture_hotkey", lambda pid: pid == 42)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        runtime,
        "_wait_for_captures",
        lambda directory, before, timeout: [tmp_path / "capture_frame1.rdc"],
    )

    result = runtime.capture_process(42, str(tmp_path / "capture"))

    assert observed["arguments"][:2] == ["inject", "--PID=42"]
    assert observed["accept_launched_id"] is True
    assert result["focused_target_window"] is True


def test_capture_program_focuses_requested_child_before_trigger(tmp_path, monkeypatch):
    target = tmp_path / "launcher.exe"
    target.touch()
    observed = {}
    monkeypatch.setattr(
        runtime,
        "_run",
        lambda arguments, **kwargs: CompletedProcess(arguments, 123, "Launched as ID 123", ""),
    )
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime, "_wait_for_visible_process", lambda name, timeout: 77)
    monkeypatch.setattr(
        runtime,
        "_trigger_capture_hotkey",
        lambda pid: observed.setdefault("pid", pid) == 77,
    )
    monkeypatch.setattr(
        runtime,
        "_wait_for_captures",
        lambda directory, before, timeout: [tmp_path / "capture_frame1.rdc"],
    )

    result = runtime.capture_program(
        str(target),
        str(tmp_path / "capture"),
        trigger_after_secs=1,
        trigger_process_name="game.exe",
    )

    assert observed["pid"] == 77
    assert result["focused_target_window"] is True


def test_capture_program_failure_reports_missing_child_diagnostics(tmp_path, monkeypatch):
    target = tmp_path / "launcher.exe"
    target.touch()
    monkeypatch.setattr(
        runtime,
        "_run",
        lambda arguments, **kwargs: CompletedProcess(arguments, 123, "Launched as ID 123", ""),
    )
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime, "_wait_for_visible_process", lambda name, timeout: None)
    monkeypatch.setattr(runtime, "_trigger_capture_hotkey", lambda pid: False)
    monkeypatch.setattr(runtime, "_wait_for_captures", lambda directory, before, timeout: [])
    monkeypatch.setattr(
        runtime,
        "_visible_processes",
        lambda: [{"process_id": 12, "name": "launcher.exe"}],
    )

    with pytest.raises(runtime.RenderDocError) as error:
        runtime.capture_program(
            str(target),
            str(tmp_path / "capture"),
            trigger_after_secs=1,
            trigger_process_name="game.exe",
        )

    message = str(error.value)
    assert "target_process=game.exe:not-found" in message
    assert "focused_target_window=False" in message
    assert "visible_processes=[launcher.exe(pid=12)]" in message
    assert "RenderDoc output: Launched as ID 123" in message


def test_capture_process_failure_reports_injection_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runtime,
        "_run",
        lambda arguments, **kwargs: CompletedProcess(
            arguments, 42, "Launched as ID 42", "inject warning"
        ),
    )
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(runtime, "_trigger_capture_hotkey", lambda pid: False)
    monkeypatch.setattr(runtime, "_wait_for_captures", lambda directory, before, timeout: [])
    monkeypatch.setattr(
        runtime,
        "_visible_processes",
        lambda: [{"process_id": 42, "name": "game.exe"}],
    )

    with pytest.raises(runtime.RenderDocError) as error:
        runtime.capture_process(42, str(tmp_path / "capture"))

    message = str(error.value)
    assert "target_process=game.exe(pid=42)" in message
    assert "focused_target_window=False" in message
    assert "RenderDoc output: Launched as ID 42\ninject warning" in message


def test_invalid_capture_is_rejected(tmp_path: Path):
    with pytest.raises(runtime.RenderDocError, match="not .rdc"):
        runtime.inspect_capture(str(tmp_path / "missing.rdc"))
