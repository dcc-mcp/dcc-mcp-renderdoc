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
    )

    assert observed["arguments"][:2] == [str(qrenderdoc), "--python"]
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["env"]["DCC_MCP_RENDERDOC_TARGET_IDENT"] == "4321"
    assert observed["kwargs"]["env"]["DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS"] == "30"
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


def test_target_control_capture_must_be_new_rdc_in_requested_directory(tmp_path):
    capture = tmp_path / "capture_frame1.rdc"
    capture.touch()
    status = {"capture_path": str(capture)}

    with pytest.raises(runtime.RenderDocError, match="not created by this request"):
        runtime._capture_from_target_status(status, tmp_path, {capture.resolve()})

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

    with pytest.raises(runtime.RenderDocError, match="host timed out after 31s"):
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
        runpy.run_path(str(Path(runtime.__file__).with_name("_target_control.py")))

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
    clock = iter([0.0, 2.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock, 2.0))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "4321")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "1")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))

    with pytest.raises(SystemExit):
        runpy.run_path(str(Path(runtime.__file__).with_name("_target_control.py")))

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
        runpy.run_path(str(Path(runtime.__file__).with_name("_target_control.py")))

    assert error.value.code == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert "renderdoc" in status["error"]


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
        runpy.run_path(str(Path(runtime.__file__).with_name("_target_control.py")))

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert calls == [("trigger", 1), ("shutdown",)]
    assert "disconnected before capture" in status["error"]


def test_bundled_target_control_selects_named_target_across_idents(monkeypatch, tmp_path):
    status_path = tmp_path / "status.json"
    capture_path = tmp_path / "capture_frame1.rdc"
    calls = []

    class Target:
        def __init__(self, name, pid):
            self.name = name
            self.pid = pid

        def Connected(self):
            return True

        def GetTarget(self):
            return self.name

        def GetPID(self):
            return self.pid

        def TriggerCapture(self, frames):
            calls.append(("trigger", self.pid, frames))

        def ReceiveMessage(self, progress):
            return SimpleNamespace(
                type="new-capture",
                newCapture=SimpleNamespace(path=str(capture_path)),
            )

        def Shutdown(self):
            calls.append(("shutdown", self.pid))

    targets = {
        38920: Target("launcher.exe", 100),
        38927: Target("C:\\games\\child", 200),
    }
    next_ident = {0: 38920, 38920: 38927, 38927: 0}
    monkeypatch.setitem(
        sys.modules,
        "renderdoc",
        SimpleNamespace(
            EnumerateRemoteTargets=lambda _url, cursor: next_ident[cursor],
            CreateTargetControl=lambda _url, ident, _client, _force: targets[ident],
            TargetControlMessageType=SimpleNamespace(
                NewCapture="new-capture", Disconnected="disconnected"
            ),
        ),
    )
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "38920")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "1")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_NAME", "child.exe")
    clock = iter([0.0, 0.0, 0.0, 2.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock, 2.0))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(SystemExit):
        runpy.run_path(str(Path(runtime.__file__).with_name("_target_control.py")))

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["target_pid"] == 200
    assert calls == [("shutdown", 100), ("trigger", 200, 1), ("shutdown", 200)]


@pytest.mark.parametrize(
    ("names", "expected_error"),
    [
        (["launcher.exe"], "no RenderDoc target matched child.exe"),
        (["child.exe", "child.exe"], "multiple RenderDoc targets matched child.exe"),
    ],
)
def test_bundled_target_control_named_target_fails_safe(
    monkeypatch, tmp_path, names, expected_error
):
    status_path = tmp_path / "status.json"

    class Target:
        def __init__(self, name):
            self.name = name

        def Connected(self):
            return True

        def GetTarget(self):
            return self.name

        def Shutdown(self):
            return None

    idents = [38920 + index for index in range(len(names))]
    targets = dict(zip(idents, (Target(name) for name in names)))
    next_ident = {0: idents[0]}
    next_ident.update({ident: idents[index + 1] for index, ident in enumerate(idents[:-1])})
    next_ident[idents[-1]] = 0
    monkeypatch.setitem(
        sys.modules,
        "renderdoc",
        SimpleNamespace(
            EnumerateRemoteTargets=lambda _url, cursor: next_ident[cursor],
            CreateTargetControl=lambda _url, ident, _client, _force: targets[ident],
            TargetControlMessageType=SimpleNamespace(
                NewCapture="new-capture", Disconnected="disconnected"
            ),
        ),
    )
    if len(names) == 1:
        clock = iter([0.0, 0.0, 2.0])
        monkeypatch.setattr(time, "monotonic", lambda: next(clock, 2.0))
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_IDENT", "38920")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS", "1")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_STATUS", str(status_path))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_TARGET_NAME", "child.exe")

    with pytest.raises(SystemExit):
        runpy.run_path(str(Path(runtime.__file__).with_name("_target_control.py")))

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert expected_error in status["error"]


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
        capture.touch()
        return {
            "target_pid": 88,
            "capture_path": str(capture),
        }

    monkeypatch.setattr(runtime, "_trigger_target_capture", trigger)

    result = runtime.capture_program(
        str(target),
        str(tmp_path / "capture"),
        trigger_after_secs=0,
        trigger_process_name="child.exe",
        capture_wait_secs=9,
    )

    assert observed == {
        "ident": 77,
        "kwargs": {"capture_wait_secs": 9, "command": None, "target_name": "child.exe"},
    }
    assert result["captures"] == [str(capture.resolve())]
    assert result["focused_target_window"] is False
    assert result["trigger_mode"] == "target_control"


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
        capture.touch()
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
        "kwargs": {"capture_wait_secs": 7, "command": None},
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
        capture.touch()
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
        (tmp_path / "capture_frame1.rdc").touch()
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
        (tmp_path / "capture_frame1.rdc").touch()
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
