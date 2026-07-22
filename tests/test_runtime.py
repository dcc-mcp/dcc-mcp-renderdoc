import ast
import io
import json
import runpy
import sys
import time
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from types import SimpleNamespace

import pytest

from dcc_mcp_renderdoc import runtime


class StubController:
    def __init__(self, stdout="Launched as ID 123", stderr="", launched_id=123):
        self.stdout = stdout
        self.stderr = stderr
        self.launched_id = launched_id

    def output(self):
        return self.stdout, self.stderr

    def close(self):
        return None


@pytest.fixture(autouse=True)
def _default_target_control_delay(monkeypatch):
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TRIGGER_AFTER_SECS", "0")
    monkeypatch.setattr(runtime.sys, "platform", "win32")


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
        (tmp_path / "capture_frame1.rdc").write_bytes(b"rdc")
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


def test_non_waiting_launch_accepts_posix_truncated_target_id(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd"
    command.touch()
    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 8, "Launched as ID 38920", ""),
    )

    result = runtime._run(
        ["capture", "game"],
        timeout_secs=10,
        command=str(command),
        accept_launched_id=True,
    )

    assert result.returncode == 8


def test_capture_controller_exposes_launched_target_ident(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd.exe"
    command.touch()

    class Process:
        def __init__(self, *_args, **_kwargs):
            self.stdout = io.StringIO("Launched as ID 4321\n")
            self.stderr = io.StringIO("")
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(runtime.subprocess, "Popen", Process)

    controller = runtime._start_capture_controller(
        ["capture", "game.exe"], timeout_secs=1, command=str(command)
    )
    try:
        assert controller.launched_id == 4321
    finally:
        controller.close()


def test_capture_controller_accepts_posix_truncated_id_after_draining_output(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd"
    command.touch()

    class Process:
        def __init__(self, *_args, **_kwargs):
            self.stdout = io.StringIO("Launched as ID 38920\n")
            self.stderr = io.StringIO("")
            self.returncode = 8

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.subprocess, "Popen", Process)

    controller = runtime._start_capture_controller(
        ["capture", "game"], timeout_secs=1, command=str(command)
    )

    assert controller.launched_id == 38920


def test_target_control_trigger_uses_bundled_qrenderdoc_status(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd.exe"
    qrenderdoc = tmp_path / "qrenderdoc.exe"
    command.touch()
    qrenderdoc.touch()
    observed = {}

    def fake_run(arguments, **kwargs):
        observed.update(arguments=arguments, kwargs=kwargs)
        script = Path(arguments[-1])
        observed["script"] = script.read_text(encoding="utf-8")
        Path(kwargs["env"]["DCC_MCP_RENDERDOC_TARGET_STATUS"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "connected": True,
                    "triggered": True,
                    "shutdown": True,
                    "timed_out": False,
                    "target_pid": 42,
                    "capture_path": str(tmp_path / "capture_frame1.rdc"),
                    "error": None,
                }
            ),
            encoding="utf-8",
        )
        return CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    result = runtime._trigger_target_capture(
        4321,
        capture_wait_secs=30,
        command=str(command),
        target_name="child.exe",
        trigger_after_secs=4,
    )

    assert observed["arguments"][:2] == [str(qrenderdoc), "--python"]
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["env"]["DCC_MCP_RENDERDOC_TARGET_IDENT"] == "4321"
    assert observed["kwargs"]["env"]["DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS"] == "30"
    assert observed["kwargs"]["env"]["DCC_MCP_RENDERDOC_TRIGGER_AFTER_SECS"] == "4"
    assert observed["kwargs"]["env"]["DCC_MCP_RENDERDOC_TARGET_NAME"] == "child.exe"
    assert 'CreateTargetControl("", ident, "dcc-mcp-renderdoc", False)' in observed["script"]
    assert "TriggerCapture(1)" in observed["script"]
    assert "finally:" in observed["script"]
    assert "Shutdown()" in observed["script"]
    assert result["target_pid"] == 42


def test_target_control_requires_qrenderdoc_sibling(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("qrenderdoc must be validated before launch"),
    )

    with pytest.raises(runtime.RenderDocError, match="qrenderdoc was not found beside"):
        runtime._trigger_target_capture(12, capture_wait_secs=1, command=str(command))


def test_target_control_reports_connection_failure_from_status(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    command.with_name("qrenderdoc.exe").touch()

    def fake_run(arguments, **_kwargs):
        Path(_kwargs["env"]["DCC_MCP_RENDERDOC_TARGET_STATUS"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "connected": False,
                    "triggered": False,
                    "shutdown": False,
                    "timed_out": False,
                    "target_pid": None,
                    "capture_path": None,
                    "error": "connection refused",
                }
            ),
            encoding="utf-8",
        )
        return CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    with pytest.raises(runtime.RenderDocError, match="connection refused"):
        runtime._trigger_target_capture(12, capture_wait_secs=1, command=str(command))


def test_target_control_rejects_malformed_status_json(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    command.with_name("qrenderdoc.exe").touch()

    def fake_run(arguments, **_kwargs):
        Path(_kwargs["env"]["DCC_MCP_RENDERDOC_TARGET_STATUS"]).write_text("{", encoding="utf-8")
        return CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    with pytest.raises(runtime.RenderDocError, match="malformed status JSON"):
        runtime._trigger_target_capture(12, capture_wait_secs=1, command=str(command))


def test_target_control_rejects_invalid_status_schema(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    command.with_name("qrenderdoc.exe").touch()

    def fake_run(arguments, **kwargs):
        Path(kwargs["env"]["DCC_MCP_RENDERDOC_TARGET_STATUS"]).write_text(
            json.dumps({"schema_version": 1}), encoding="utf-8"
        )
        return CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    with pytest.raises(runtime.RenderDocError, match="invalid status schema"):
        runtime._trigger_target_capture(12, capture_wait_secs=1, command=str(command))


def test_target_control_status_requires_integer_version_and_null_error():
    status = {
        "schema_version": 1,
        "connected": True,
        "triggered": True,
        "shutdown": True,
        "timed_out": False,
        "target_pid": 42,
        "capture_path": "capture.rdc",
        "error": None,
    }

    runtime._validate_target_control_status(status)
    with pytest.raises(runtime.RenderDocError, match="unsupported status version"):
        runtime._validate_target_control_status({**status, "schema_version": True})
    with pytest.raises(runtime.RenderDocError, match="Target Control failed"):
        runtime._validate_target_control_status({**status, "error": ""})


def test_target_control_capture_must_be_new_rdc_in_requested_directory(tmp_path):
    capture = tmp_path / "capture_frame1.rdc"
    capture.touch()
    status = {"capture_path": str(capture)}

    with pytest.raises(runtime.RenderDocError, match="not created by this request"):
        runtime._capture_from_target_status(status, tmp_path, {capture.resolve()})

    with pytest.raises(runtime.RenderDocError, match="empty"):
        runtime._capture_from_target_status(status, tmp_path, set())

    outside = tmp_path.parent / f"{tmp_path.name}-outside.rdc"
    outside.touch()
    try:
        with pytest.raises(runtime.RenderDocError, match="outside the requested RDC output"):
            runtime._capture_from_target_status({"capture_path": str(outside)}, tmp_path, set())
    finally:
        outside.unlink()


def test_target_control_host_timeout_cleans_temporary_script(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    command.with_name("qrenderdoc.exe").touch()
    observed = {}

    def fake_run(arguments, **kwargs):
        observed["directory"] = Path(kwargs["env"]["DCC_MCP_RENDERDOC_TARGET_STATUS"]).parent
        raise TimeoutExpired(arguments, 31)

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    with pytest.raises(runtime.RenderDocError, match="host timed out after 32s"):
        runtime._trigger_target_capture(12, capture_wait_secs=1, command=str(command))

    assert not observed["directory"].exists()


def test_bundled_target_control_script_triggers_and_shuts_down(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    capture_path = tmp_path / "capture_frame1.rdc"
    calls = []

    class Target:
        def Connected(self):
            return True

        def GetPID(self):
            return 42

        def TriggerCapture(self, frames):
            calls.append(("trigger", frames))

        def ReceiveMessage(self, progress):
            calls.append(("receive", progress))
            return SimpleNamespace(
                type="new-capture",
                newCapture=SimpleNamespace(path=str(capture_path)),
            )

        def Shutdown(self):
            calls.append(("shutdown",))

    def create_target(url, ident, client, force):
        calls.append(("connect", url, ident, client, force))
        return Target()

    monkeypatch.setitem(
        sys.modules,
        "renderdoc",
        SimpleNamespace(
            CreateTargetControl=create_target,
            TargetControlMessageType=SimpleNamespace(NewCapture="new-capture"),
        ),
    )
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "4321")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "30")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))

    with pytest.raises(SystemExit) as error:
        runpy.run_path(
            str(Path(runtime.__file__).with_name("_target_control.py")),
            run_name="__main__",
        )

    assert error.value.code == 0
    assert calls == [
        ("connect", "", 4321, "dcc-mcp-renderdoc", False),
        ("trigger", 1),
        ("receive", None),
        ("shutdown",),
    ]
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["capture_path"] == str(capture_path)
    assert status["shutdown"] is True


def test_target_control_requires_linux_display_instead_of_assuming_qt_plugin(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd"
    command.touch()
    command.with_name("qrenderdoc").touch()

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("qrenderdoc must not start without a display"),
    )

    with pytest.raises(runtime.RenderDocError, match="requires an X/Wayland display"):
        runtime._trigger_target_capture(12, capture_wait_secs=1, command=str(command))


def test_target_control_preserves_explicit_linux_qt_platform(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd"
    command.touch()
    command.with_name("qrenderdoc").touch()
    observed = {}

    def fake_run(arguments, **kwargs):
        observed["environment"] = kwargs["env"]
        Path(kwargs["env"]["DCC_MCP_RENDERDOC_TARGET_STATUS"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "connected": True,
                    "triggered": True,
                    "shutdown": True,
                    "timed_out": False,
                    "target_pid": 42,
                    "capture_path": str(tmp_path / "capture.rdc"),
                    "error": None,
                }
            ),
            encoding="utf-8",
        )
        return CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    runtime._trigger_target_capture(12, capture_wait_secs=1, command=str(command))

    assert observed["environment"]["QT_QPA_PLATFORM"] == "xcb"


def test_target_control_rejects_unsupported_macos_runtime(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd"
    command.touch()
    command.with_name("qrenderdoc").touch()
    monkeypatch.setattr(runtime.sys, "platform", "darwin")

    with pytest.raises(runtime.RenderDocError, match="supported only on Windows and Linux"):
        runtime._trigger_target_capture(12, capture_wait_secs=1, command=str(command))


def test_bundled_target_control_timeout_still_shuts_down(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    calls = []

    class Target:
        def Connected(self):
            return True

        def GetPID(self):
            return 42

        def TriggerCapture(self, frames):
            calls.append(("trigger", frames))

        def ReceiveMessage(self, progress):
            pytest.fail("deadline should expire before message polling")

        def Shutdown(self):
            calls.append(("shutdown",))

    monkeypatch.setitem(
        sys.modules,
        "renderdoc",
        SimpleNamespace(
            CreateTargetControl=lambda *_args: Target(),
            TargetControlMessageType=SimpleNamespace(NewCapture="new-capture"),
        ),
    )
    clock = iter([0.0, 2.0, 2.0, 4.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock, 2.0))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "4321")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "1")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))

    with pytest.raises(SystemExit):
        runpy.run_path(
            str(Path(runtime.__file__).with_name("_target_control.py")),
            run_name="__main__",
        )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert calls == [("trigger", 1), ("shutdown",)]
    assert status["timed_out"] is True
    assert status["shutdown"] is True
    assert "timed out waiting" in status["error"]


def test_bundled_target_control_import_failure_exits_before_ui(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    monkeypatch.delitem(sys.modules, "renderdoc", raising=False)
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "4321")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "1")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))

    with pytest.raises(SystemExit) as error:
        runpy.run_path(
            str(Path(runtime.__file__).with_name("_target_control.py")),
            run_name="__main__",
        )

    assert error.value.code == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert "renderdoc" in status["error"]


def test_bundled_target_control_has_an_import_safe_main_guard(tmp_path):
    status_path = tmp_path / "status.json"
    namespace = runpy.run_path(
        str(Path(runtime.__file__).with_name("_target_control.py")), run_name="not_main"
    )

    assert callable(namespace["main"])
    assert not status_path.exists()


def test_bundled_target_control_fails_immediately_on_disconnect(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    calls = []

    class Target:
        def Connected(self):
            return True

        def GetPID(self):
            return 42

        def TriggerCapture(self, frames):
            calls.append(("trigger", frames))

        def ReceiveMessage(self, progress):
            return SimpleNamespace(type="disconnected")

        def Shutdown(self):
            calls.append(("shutdown",))

    monkeypatch.setitem(
        sys.modules,
        "renderdoc",
        SimpleNamespace(
            CreateTargetControl=lambda *_args: Target(),
            TargetControlMessageType=SimpleNamespace(
                NewCapture="new-capture", Disconnected="disconnected"
            ),
        ),
    )
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "4321")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "30")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))
    monkeypatch.delenv("DCC_MCP_RENDERDOC_TARGET_NAME", raising=False)

    with pytest.raises(SystemExit):
        runpy.run_path(
            str(Path(runtime.__file__).with_name("_target_control.py")),
            run_name="__main__",
        )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert calls == [("trigger", 1), ("shutdown",)]
    assert "disconnected before capture" in status["error"]


def test_bundled_target_control_uses_launched_target_when_name_matches(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    capture_path = tmp_path / "capture_frame1.rdc"
    calls = []

    class Target:
        def Connected(self):
            return True

        def GetPID(self):
            return 42

        def GetTarget(self):
            return r"C:\games\DccMcpGame"

        def TriggerCapture(self, frames):
            calls.append(("trigger", frames))

        def ReceiveMessage(self, progress):
            calls.append(("receive", progress))
            return SimpleNamespace(
                type="new-capture",
                newCapture=SimpleNamespace(path=str(capture_path)),
            )

        def Shutdown(self):
            calls.append(("shutdown",))

    monkeypatch.setitem(
        sys.modules,
        "renderdoc",
        SimpleNamespace(
            CreateTargetControl=lambda *_args: Target(),
            TargetControlMessageType=SimpleNamespace(
                NewChild="new-child",
                NewCapture="new-capture",
                Disconnected="disconnected",
            ),
        ),
    )
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "4321")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "30")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_NAME", "DccMcpGame.exe")

    with pytest.raises(SystemExit):
        runpy.run_path(
            str(Path(runtime.__file__).with_name("_target_control.py")),
            run_name="__main__",
        )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["capture_path"] == str(capture_path)
    assert status["target_pid"] == 42
    assert status["error"] is None
    assert calls == [("trigger", 1), ("receive", None), ("shutdown",)]


def test_bundled_target_control_pumps_messages_through_long_trigger_delay(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    capture_path = tmp_path / "capture_frame1.rdc"
    calls = []
    clock = [0.0]
    triggered = [False]

    class Target:
        def Connected(self):
            return True

        def GetPID(self):
            return 42

        def TriggerCapture(self, frames):
            calls.append(("trigger", frames))
            triggered[0] = True

        def ReceiveMessage(self, progress):
            calls.append(("receive", progress, clock[0]))
            if not triggered[0]:
                clock[0] = min(clock[0] + 100.0, 612.0)
                return SimpleNamespace(type="noop")
            return SimpleNamespace(
                type="new-capture",
                newCapture=SimpleNamespace(path=str(capture_path)),
            )

        def Shutdown(self):
            calls.append(("shutdown",))

    monkeypatch.setitem(
        sys.modules,
        "renderdoc",
        SimpleNamespace(
            CreateTargetControl=lambda *_args: Target(),
            TargetControlMessageType=SimpleNamespace(
                NewCapture="new-capture", Disconnected="disconnected"
            ),
        ),
    )
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "4321")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "10")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TRIGGER_AFTER_SECS", "612")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))
    monkeypatch.delenv("DCC_MCP_RENDERDOC_TARGET_NAME", raising=False)

    with pytest.raises(SystemExit):
        runpy.run_path(
            str(Path(runtime.__file__).with_name("_target_control.py")),
            run_name="__main__",
        )

    trigger_index = calls.index(("trigger", 1))
    assert len([call for call in calls[:trigger_index] if call[0] == "receive"]) == 7
    assert calls[-2:] == [("receive", None, 612.0), ("shutdown",)]
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["capture_path"] == str(capture_path)
    assert status["error"] is None


@pytest.mark.parametrize("actual_target", ["C:\\\\games\\\\child", "/games/child"])
def test_bundled_target_control_follows_new_child_from_launched_parent(
    monkeypatch, tmp_path, actual_target
):
    status_path = tmp_path / "status.json"
    capture_path = tmp_path / "capture_frame1.rdc"
    calls = []

    class Parent:
        def __init__(self):
            self.sent_child = False

        def Connected(self):
            return True

        def GetPID(self):
            return 100

        def GetTarget(self):
            return "launcher.exe"

        def ReceiveMessage(self, progress):
            calls.append(("parent-receive", progress))
            if not self.sent_child:
                self.sent_child = True
                return SimpleNamespace(
                    type="new-child",
                    newChild=SimpleNamespace(ident=38927, processId=200),
                )
            return SimpleNamespace(type="noop")

        def Shutdown(self):
            calls.append(("parent-shutdown",))

    class Child:
        def Connected(self):
            return True

        def GetPID(self):
            return 200

        def GetTarget(self):
            return actual_target

        def TriggerCapture(self, frames):
            calls.append(("child-trigger", frames))

        def ReceiveMessage(self, progress):
            calls.append(("child-receive", progress))
            return SimpleNamespace(
                type="new-capture",
                newCapture=SimpleNamespace(path=str(capture_path)),
            )

        def Shutdown(self):
            calls.append(("child-shutdown",))

    targets = {38920: Parent(), 38927: Child()}
    monkeypatch.setitem(
        sys.modules,
        "renderdoc",
        SimpleNamespace(
            CreateTargetControl=lambda _url, ident, _client, _force: targets[ident],
            TargetControlMessageType=SimpleNamespace(
                NewChild="new-child",
                NewCapture="new-capture",
                Disconnected="disconnected",
            ),
        ),
    )
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "38920")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "1")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_NAME", "child.exe")
    clock = iter([0.0, 0.0, 0.0, 0.1, 0.1, 0.3, 0.3, 0.3, 0.3])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock, 0.3))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(SystemExit):
        runpy.run_path(
            str(Path(runtime.__file__).with_name("_target_control.py")),
            run_name="__main__",
        )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["target_pid"] == 200
    assert calls == [
        ("parent-receive", None),
        ("parent-receive", None),
        ("parent-shutdown",),
        ("child-trigger", 1),
        ("child-receive", None),
        ("child-shutdown",),
    ]


@pytest.mark.parametrize(
    ("child_name", "child_pid", "message_pid", "expected_error"),
    [
        ("other.exe", 200, 200, "no child target matched child.exe"),
        ("child.exe", 201, 200, "PID did not match NewChild"),
    ],
)
def test_bundled_target_control_child_selection_fails_safe(
    monkeypatch,
    tmp_path,
    child_name,
    child_pid,
    message_pid,
    expected_error,
):
    status_path = tmp_path / "status.json"
    shutdowns = []

    class Parent:
        def Connected(self):
            return True

        def GetTarget(self):
            return "launcher.exe"

        def ReceiveMessage(self, _progress):
            return SimpleNamespace(
                type="new-child",
                newChild=SimpleNamespace(ident=38927, processId=message_pid),
            )

        def Shutdown(self):
            shutdowns.append("parent")

    class Child:
        def Connected(self):
            return True

        def GetPID(self):
            return child_pid

        def GetTarget(self):
            return child_name

        def Shutdown(self):
            shutdowns.append("child")

    targets = {38920: Parent(), 38927: Child()}
    monkeypatch.setitem(
        sys.modules,
        "renderdoc",
        SimpleNamespace(
            CreateTargetControl=lambda _url, ident, _client, _force: targets[ident],
            TargetControlMessageType=SimpleNamespace(
                NewChild="new-child",
                NewCapture="new-capture",
                Disconnected="disconnected",
            ),
        ),
    )
    if child_name == "other.exe":
        clock = iter([0.0, 0.0, 2.0])
        monkeypatch.setattr(time, "monotonic", lambda: next(clock, 2.0))
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "38920")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "1")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_NAME", "child.exe")

    with pytest.raises(SystemExit):
        runpy.run_path(
            str(Path(runtime.__file__).with_name("_target_control.py")),
            run_name="__main__",
        )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert expected_error in status["error"]
    assert shutdowns.count("child") >= 1
    assert shutdowns[-1] == "parent"


def test_bundled_target_control_rejects_multiple_matching_siblings(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    clock = [0.0]
    messages = iter(
        [
            (
                0.0,
                SimpleNamespace(
                    type="new-child",
                    newChild=SimpleNamespace(ident=38927, processId=200),
                ),
            ),
            (
                0.2,
                SimpleNamespace(
                    type="new-child",
                    newChild=SimpleNamespace(ident=38928, processId=202),
                ),
            ),
            (
                0.3,
                SimpleNamespace(
                    type="new-child",
                    newChild=SimpleNamespace(ident=38929, processId=201),
                ),
            ),
            (
                0.56,
                SimpleNamespace(type="noop"),
            ),
        ]
    )
    fallback = (
        0.56,
        SimpleNamespace(type="noop"),
    )
    shutdowns = []

    class Parent:
        def Connected(self):
            return True

        def GetTarget(self):
            return "launcher.exe"

        def ReceiveMessage(self, _progress):
            timestamp, message = next(messages, fallback)
            clock[0] = timestamp
            return message

        def Shutdown(self):
            shutdowns.append("parent")

    class Child:
        def __init__(self, pid, target):
            self.pid = pid
            self.target = target

        def Connected(self):
            return True

        def GetPID(self):
            return self.pid

        def GetTarget(self):
            return self.target

        def Shutdown(self):
            shutdowns.append(self.pid)

    targets = {
        38920: Parent(),
        38927: Child(200, "child.exe"),
        38928: Child(202, "other.exe"),
        38929: Child(201, "child.exe"),
    }
    monkeypatch.setitem(
        sys.modules,
        "renderdoc",
        SimpleNamespace(
            CreateTargetControl=lambda _url, ident, _client, _force: targets[ident],
            TargetControlMessageType=SimpleNamespace(
                NewChild="new-child",
                NewCapture="new-capture",
                Disconnected="disconnected",
            ),
        ),
    )
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "38920")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "1")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_NAME", "child.exe")

    with pytest.raises(SystemExit):
        runpy.run_path(
            str(Path(runtime.__file__).with_name("_target_control.py")),
            run_name="__main__",
        )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert "multiple child targets matched child.exe" in status["error"]
    assert sorted(value for value in shutdowns if isinstance(value, int)) == [200, 201, 202]
    assert shutdowns[-1] == "parent"


def test_bundled_target_control_uses_one_shared_capture_deadline(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    calls = []
    clock = [0.0]

    class Target:
        def Connected(self):
            return True

        def GetPID(self):
            return 42

        def TriggerCapture(self, frames):
            calls.append(("trigger", frames))

        def ReceiveMessage(self, _progress):
            calls.append(("receive", clock[0]))
            clock[0] += 1.0
            return SimpleNamespace(type="noop")

        def Shutdown(self):
            calls.append(("shutdown",))

    monkeypatch.setitem(
        sys.modules,
        "renderdoc",
        SimpleNamespace(
            CreateTargetControl=lambda *_args: Target(),
            TargetControlMessageType=SimpleNamespace(
                NewChild="new-child",
                NewCapture="new-capture",
                Disconnected="disconnected",
            ),
        ),
    )
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "38920")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "3")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TRIGGER_AFTER_SECS", "2")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))
    monkeypatch.delenv("DCC_MCP_RENDERDOC_TARGET_NAME", raising=False)

    with pytest.raises(SystemExit):
        runpy.run_path(
            str(Path(runtime.__file__).with_name("_target_control.py")),
            run_name="__main__",
        )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["timed_out"] is True
    assert calls == [
        ("receive", 0.0),
        ("receive", 1.0),
        ("trigger", 1),
        ("receive", 2.0),
        ("receive", 3.0),
        ("receive", 4.0),
        ("shutdown",),
    ]


def test_bundled_target_control_is_python36_compatible():
    source = Path(runtime.__file__).with_name("_target_control.py").read_text(encoding="utf-8")

    ast.parse(source, feature_version=(3, 6))


def test_capture_program_reuses_target_control_trigger(tmp_path, monkeypatch):
    target = tmp_path / "game.exe"
    target.touch()
    capture = tmp_path / "capture_frame1.rdc"
    observed = {}
    monkeypatch.setattr(
        runtime,
        "_start_capture_controller",
        lambda *_args, **_kwargs: StubController(launched_id=77),
    )
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    def trigger(ident, **kwargs):
        observed.update(ident=ident, kwargs=kwargs)
        capture.write_bytes(b"rdc")
        return {
            "target_pid": 88,
            "capture_path": str(capture),
        }

    monkeypatch.setattr(runtime, "_trigger_target_capture", trigger)

    result = runtime.capture_program(
        str(target),
        str(tmp_path / "capture"),
        trigger_after_secs=0,
        hook_children=True,
        trigger_process_name="child.exe",
        capture_wait_secs=9,
    )

    assert observed == {
        "ident": 77,
        "kwargs": {
            "capture_wait_secs": 9,
            "command": None,
            "target_name": "child.exe",
            "trigger_after_secs": 0,
        },
    }
    assert result["captures"] == [str(capture.resolve())]
    assert result["focused_target_window"] is False
    assert result["trigger_mode"] == "target_control"


def test_capture_program_rejects_child_name_without_child_hook(tmp_path):
    target = tmp_path / "game.exe"
    target.touch()

    with pytest.raises(runtime.RenderDocError, match="requires hook_children=True"):
        runtime.capture_program(
            str(target),
            str(tmp_path / "capture"),
            trigger_after_secs=0,
            trigger_process_name="child.exe",
        )


def test_capture_process_reuses_target_control_trigger(tmp_path, monkeypatch):
    capture = tmp_path / "capture_frame1.rdc"
    observed = {}
    monkeypatch.setattr(
        runtime,
        "_start_capture_controller",
        lambda *_args, **_kwargs: StubController(launched_id=91),
    )
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    def trigger(ident, **kwargs):
        observed.update(ident=ident, kwargs=kwargs)
        capture.write_bytes(b"rdc")
        return {
            "target_pid": 42,
            "capture_path": str(capture),
        }

    monkeypatch.setattr(runtime, "_trigger_target_capture", trigger)

    result = runtime.capture_process(
        42,
        str(tmp_path / "capture"),
        trigger_after_secs=0,
        capture_wait_secs=7,
    )

    assert observed == {
        "ident": 91,
        "kwargs": {"capture_wait_secs": 7, "command": None, "trigger_after_secs": 0},
    }
    assert result["captures"] == [str(capture.resolve())]
    assert result["focused_target_window"] is False
    assert result["trigger_mode"] == "target_control"


def test_capture_process_injects_triggers_and_reports_capture(tmp_path, monkeypatch):
    observed = {}
    capture = tmp_path / "capture_frame1.rdc"

    def fake_run(arguments, **kwargs):
        observed["arguments"] = arguments
        return StubController("Launched as ID 456", launched_id=456)

    monkeypatch.setattr(runtime, "_start_capture_controller", fake_run)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    def trigger(ident, **_kwargs):
        capture.write_bytes(b"rdc")
        return {
            "target_pid": 42,
            "capture_path": str(capture),
        }

    monkeypatch.setattr(runtime, "_trigger_target_capture", trigger)

    result = runtime.capture_process(42, str(tmp_path / "capture"))

    assert observed["arguments"][:2] == ["inject", "--PID=42"]
    assert result["focused_target_window"] is False
    assert result["trigger_mode"] == "target_control"


def test_capture_process_triggers_while_injector_is_running_and_only_stops_injector(
    tmp_path, monkeypatch
):
    started = {}
    target = {"running": True}
    command = tmp_path / "renderdoccmd.exe"
    command.touch()

    class Injector:
        def __init__(self, arguments, **kwargs):
            started.update(arguments=arguments, kwargs=kwargs, process=self)
            self.stdout = io.StringIO("Launched as ID 42\n")
            self.stderr = io.StringIO("")
            self.returncode = None
            self.terminated = False
            self.killed = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                raise TimeoutExpired(self.args, timeout)
            return self.returncode

    monkeypatch.setattr(runtime.subprocess, "Popen", Injector)

    def trigger(ident, **_kwargs):
        assert ident == 42
        assert started["process"].returncode is None
        (tmp_path / "capture_frame1.rdc").write_bytes(b"rdc")
        return {
            "target_pid": 42,
            "capture_path": str(tmp_path / "capture_frame1.rdc"),
        }

    monkeypatch.setattr(runtime, "_trigger_target_capture", trigger)

    result = runtime.capture_process(
        42,
        str(tmp_path / "capture;still-one-argument"),
        trigger_after_secs=0,
        timeout_secs=1,
        command=str(command),
    )

    assert result["focused_target_window"] is False
    assert started["arguments"][1:3] == ["inject", "--PID=42"]
    assert started["arguments"][4] == str((tmp_path / "capture;still-one-argument").resolve())
    assert started["kwargs"]["shell"] is False
    assert started["process"].terminated is True
    assert started["process"].killed is False
    assert target["running"] is True


def test_capture_process_readiness_timeout_force_stops_only_injector(tmp_path, monkeypatch):
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    target = {"running": True}
    started = {}

    class StuckInjector:
        def __init__(self, arguments, **kwargs):
            started["process"] = self
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")
            self.killed = False
            self.terminated = False

        def poll(self):
            return -9 if self.killed else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            if not self.killed:
                raise TimeoutExpired("renderdoccmd", timeout)
            return -9

    monkeypatch.setattr(runtime.subprocess, "Popen", StuckInjector)

    with pytest.raises(runtime.RenderDocError, match="waiting for inject readiness"):
        runtime.capture_process(
            42,
            str(tmp_path / "capture"),
            trigger_after_secs=0,
            timeout_secs=0,
            command=str(command),
        )

    assert started["process"].terminated is True
    assert started["process"].killed is True
    assert target["running"] is True


def test_triggered_program_capture_keeps_controller_alive_until_capture(tmp_path, monkeypatch):
    target = tmp_path / "game.exe"
    target.touch()
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    events = []

    class Controller:
        def __init__(self, arguments, **kwargs):
            events.append(("started", arguments, kwargs))
            self.stdout = io.StringIO("Launched as ID 77\n")
            self.stderr = io.StringIO("")
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append(("stopped",))
            self.returncode = 0

        def kill(self):
            raise AssertionError("responsive controller should not be killed")

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(runtime.subprocess, "Popen", Controller)

    def captured_while_running(ident, **_kwargs):
        assert [event[0] for event in events] == ["started"]
        assert ident == 77
        (tmp_path / "capture_frame1.rdc").write_bytes(b"rdc")
        return {
            "target_pid": 77,
            "capture_path": str(tmp_path / "capture_frame1.rdc"),
        }

    monkeypatch.setattr(runtime, "_trigger_target_capture", captured_while_running)

    result = runtime.capture_program(
        str(target),
        str(tmp_path / "capture"),
        trigger_after_secs=0,
        command=str(command),
    )

    assert result["captures"] == [str((tmp_path / "capture_frame1.rdc").resolve())]
    assert [event[0] for event in events] == ["started", "stopped"]
    assert events[0][1][-1] == str(target.resolve())
    assert events[0][2]["shell"] is False


def test_invalid_capture_is_rejected(tmp_path: Path):
    with pytest.raises(runtime.RenderDocError, match="not .rdc"):
        runtime.inspect_capture(str(tmp_path / "missing.rdc"))
