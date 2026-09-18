from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dcc_mcp_renderdoc import capabilities, replay, runtime
from dcc_mcp_renderdoc.replay import clean_params

BRIDGE = Path(replay.__file__).with_name("_replay_bridge.py")


class _Enum(int):
    """Mimic RenderDoc's SWIG enums: int-valued, but rendered symbolically."""

    def __new__(cls, text: str, value: int):
        return super().__new__(cls, value)

    def __init__(self, text: str, value: int) -> None:
        super().__init__()
        self._text = text

    def __str__(self) -> str:
        return self._text


def _resource_id(value: int) -> _Enum:
    return _Enum("ResourceId::{}".format(value), value)


class _ResourceId:
    """Stand-in for RenderDoc's ``ResourceId``, which cannot wrap an integer."""

    def __init__(self, *args, **kwargs) -> None:
        raise TypeError("in method 'new_ResourceId', argument 1 of type 'ResourceId const &'")

    @staticmethod
    def Null():
        return _resource_id(0)


def _action(event_id, name, children=(), flags=0):
    action = SimpleNamespace(
        eventId=event_id,
        actionId=event_id,
        flags=flags,
        children=list(children),
        numIndices=3,
        numInstances=1,
        customName=name,
        indexOffset=0,
        baseVertex=0,
        vertexOffset=0,
        instanceOffset=0,
        drawIndex=0,
        dispatchDimension=SimpleNamespace(x=1, y=1, z=1),
        dispatchThreadsDimension=SimpleNamespace(x=1, y=1, z=1),
        dispatchBase=SimpleNamespace(x=0, y=0, z=0),
        outputs=[_resource_id(7)],
        depthOut=_resource_id(0),
        copySource=_resource_id(0),
        copyDestination=_resource_id(0),
    )
    action.GetName = lambda _structured: name
    return action


def _texture(resource_id, name="tex", width=4, height=4):
    return SimpleNamespace(
        resourceId=resource_id,
        width=width,
        height=height,
        depth=1,
        arraysize=1,
        mips=1,
        format=SimpleNamespace(Name=lambda: "R8G8B8A8_UNORM"),
        dimension=_Enum("TextureDim.Texture2D", 2),
        name=name,
    )


def _pixel_modification():
    """Stand-in for one RenderDoc ``PixelModification``."""
    return SimpleNamespace(
        eventId=2,
        primitiveID=1,
        fragIndex=0,
        unboundPS=False,
        shaderDiscarded=False,
        depthTestFailed=False,
        stencilTestFailed=False,
        scissorClipped=False,
        backfaceCulled=False,
        preMod=SimpleNamespace(col=SimpleNamespace(floatValue=[0.0, 0.0, 0.0, 1.0])),
        postMod=SimpleNamespace(col=SimpleNamespace(floatValue=[1.0, 0.0, 0.0, 1.0])),
        shaderOut=SimpleNamespace(col=SimpleNamespace(floatValue=[1.0, 0.0, 0.0, 1.0])),
        Passed=lambda: True,
    )


def _debug_trace():
    """Stand-in for one RenderDoc ``ShaderDebugTrace``."""
    return SimpleNamespace(
        stage=_Enum("ShaderStage.Pixel", 5),
        states=[
            SimpleNamespace(
                stepIndex=0,
                nextInstruction=1,
                flags=_Enum("ShaderEvents.NoEvent", 0),
                changes=None,
            )
        ],
        inputs=None,
        sourceVars=None,
    )


class FakeController:
    """Minimal stand-in for RenderDoc's ReplayController."""

    def __init__(self, **overrides):
        self.calls: list[tuple] = []
        self.pixel_history = True
        self.shader_debugging = True
        self.post_vs_data = True
        self.actions = [
            _action(1, "Frame", [_action(2, "Draw A", flags=1), _action(3, "Clear B", flags=8)]),
            _action(4, "Draw C", flags=1),
        ]
        self.textures = [_texture(11, "colour"), _texture(12, "depth")]
        self.buffers = [SimpleNamespace(resourceId=21, length=64)]
        self.resources = [
            SimpleNamespace(resourceId=11, name="colour", type=_Enum("ResourceType.Texture", 2)),
            SimpleNamespace(resourceId=21, name="verts", type=_Enum("ResourceType.Buffer", 1)),
        ]
        self.shader_bound = True
        self.buffer_bytes = b"\x01\x02\x03\x04"
        for key, value in overrides.items():
            setattr(self, key, value)

    def SetFrameEvent(self, event_id, force):
        self.calls.append(("set-event", event_id, force))

    def GetAPIProperties(self):
        return SimpleNamespace(
            pipelineType=_Enum("GraphicsAPI.D3D11", 2),
            localRenderer="renderer",
            vendor="vendor",
            degraded=False,
            remoteReplay=False,
            pixelHistory=self.pixel_history,
            shaderDebugging=self.shader_debugging,
            postVSData=self.post_vs_data,
            rgpCapture=False,
        )

    def GetFrameInfo(self):
        return SimpleNamespace(frameNumber=1, debugMessages=[], captureTime=0)

    def GetRootActions(self):
        return self.actions

    def GetStructuredFile(self):
        return SimpleNamespace(version=1)

    def GetResources(self):
        return self.resources

    def GetTextures(self):
        return self.textures

    def GetBuffers(self):
        return self.buffers

    def GetUsage(self, resource_id):
        self.calls.append(("usage", int(resource_id)))
        return [SimpleNamespace(eventId=2, usage=_Enum("ResourceUsage.ColorTarget", 16))]

    def GetPostVSData(self, instance, view, stage):
        self.calls.append(("post-vs", instance, view, stage))
        return SimpleNamespace(
            status=_Enum("MeshDataStatus.Succeeded", 1),
            numIndices=3,
            topology=_Enum("Topology.TriangleList", 3),
            baseVertex=0,
            vertexResourceId=21,
            vertexByteOffset=0,
            vertexByteStride=12,
            vertexByteSize=24,
            indexResourceId=0,
            indexByteOffset=0,
            indexByteStride=0,
            instanced=False,
            unproject=True,
            nearPlane=0.0,
            farPlane=1.0,
            format=SimpleNamespace(
                Name=lambda: "R32G32B32_FLOAT",
                compType=_Enum("CompType.Float", 1),
                compCount=3,
            ),
        )

    def GetBufferData(self, buffer, offset, length):
        self.calls.append(("buffer-data", int(buffer), offset, length))
        return self.buffer_bytes

    def GetTextureData(self, texture, sub):
        return b"\x00"

    def SaveTexture(self, save, path):
        self.calls.append(("save-texture", int(save.resourceId), path))
        Path(path).write_bytes(b"PNG")

    def PickPixel(self, texture, x, y, sub, type_cast):
        self.calls.append(("pick-pixel", int(texture), x, y))
        return _pixel_modification()

    def PixelHistory(self, texture, x, y, sub, type_cast):
        self.calls.append(("pixel-history", int(texture), x, y))
        return [_pixel_modification()]

    def DebugPixel(self, x, y, inputs):
        self.calls.append(("debug-pixel", x, y))
        return _debug_trace()

    def DebugVertex(self, vertex_index, instance, index, instance_offset, vertex_offset):
        self.calls.append(
            ("debug-vertex", vertex_index, instance, index, instance_offset, vertex_offset)
        )
        return _debug_trace()

    def DebugThread(self, group_id, thread_id):
        self.calls.append(("debug-thread", list(group_id), list(thread_id)))
        return _debug_trace()

    def FreeTrace(self, trace):
        self.calls.append(("free-trace",))

    def EnumerateCounters(self):
        return [_Enum("GPUCounter.EventGPUDuration", 1), _Enum("GPUCounter.PSInvocations", 2)]

    def DescribeCounter(self, counter):
        return SimpleNamespace(
            name="duration",
            category="gpu",
            description="event duration",
            unit=_Enum("CounterUnit.Seconds", 4),
            resultType=_Enum("CompType.Float", 1),
        )

    def FetchCounters(self, counters):
        self.calls.append(("fetch-counters", [int(item) for item in counters]))
        return [SimpleNamespace(counter=1, eventId=2, value=SimpleNamespace(f=0.5))]

    def GetDebugMessages(self):
        return [
            SimpleNamespace(
                eventId=3,
                category=_Enum("MessageCategory.Performance", 4),
                severity=_Enum("MessageSeverity.Medium", 2),
                source=_Enum("MessageSource.API", 0),
                messageID=42,
                description="slow",
            )
        ]

    def GetPipelineState(self):
        return FakePipeState(self)


class FakePipeState:
    def __init__(self, controller):
        self.controller = controller

    def GetShader(self, stage):
        return _resource_id(31) if str(stage) == "ShaderStage.Pixel" else _resource_id(0)

    def GetShaderEntryPoint(self, stage):
        return "PSMain" if str(stage) == "ShaderStage.Pixel" else ""

    def GetShaderReflection(self, stage):
        if not self.controller.shader_bound or str(stage) != "ShaderStage.Pixel":
            return None
        return SimpleNamespace(
            resourceId=_resource_id(31),
            entryPoint="PSMain",
            stage=stage,
            encoding=_Enum("ShaderEncoding.DXBC", 0),
            dispatchThreadsDimension=SimpleNamespace(x=0, y=0, z=0),
            inputSignature=[
                SimpleNamespace(
                    varName="colour",
                    semanticName="COLOR",
                    semanticIndex=0,
                    regIndex=0,
                    compCount=4,
                    systemValue=_Enum("SystemValue.None", 0),
                )
            ],
            outputSignature=[],
            constantBlocks=[
                SimpleNamespace(
                    name="cb0",
                    fixedBindNumber=0,
                    byteSize=64,
                    bufferBacked=True,
                    variables=[
                        SimpleNamespace(
                            name="tint",
                            byteOffset=0,
                            type=SimpleNamespace(
                                compType=_Enum("CompType.Float", 1),
                                rows=1,
                                columns=4,
                                elements=1,
                            ),
                        ),
                        SimpleNamespace(
                            name="count",
                            byteOffset=16,
                            type=SimpleNamespace(
                                compType=_Enum("CompType.UInt", 2), rows=1, columns=1, elements=1
                            ),
                        ),
                    ],
                )
            ],
            readOnlyResources=[
                SimpleNamespace(
                    name="albedo",
                    fixedBindNumber=1,
                    descriptorType=_Enum("DescriptorType.Texture", 1),
                    textureType=_Enum("TextureType.Texture2D", 2),
                    isTexture=True,
                    isReadOnly=True,
                )
            ],
            readWriteResources=[],
            samplers=[],
            debugInfo=None,
        )

    def GetPrimitiveTopology(self):
        return _Enum("Topology.TriangleList", 3)

    def GetIBuffer(self):
        return SimpleNamespace(resource=_resource_id(0))

    def GetVBuffers(self):
        return [SimpleNamespace(resource=_resource_id(21))]

    def GetVertexInputs(self):
        return [SimpleNamespace(name="POSITION")]

    def GetOutputTargets(self):
        return [SimpleNamespace(resource=_resource_id(11))]

    def GetDepthTarget(self):
        return SimpleNamespace(resource=_resource_id(12))

    def GetViewport(self, index):
        return SimpleNamespace(
            x=0, y=0, width=4, height=4, minDepth=0.0, maxDepth=1.0, enabled=True
        )

    def GetScissor(self, index):
        return SimpleNamespace(x=0, y=0, width=4, height=4, enabled=True)

    def GetColorBlends(self):
        return [SimpleNamespace(enabled=False)]

    def GetDepthTestState(self):
        return SimpleNamespace(depthEnable=True)

    def GetRasterState(self):
        return SimpleNamespace(cullMode=_Enum("CullMode.NoCull", 0))

    def GetReadOnlyResources(self, stage, only_used):
        return [SimpleNamespace(descriptor=SimpleNamespace(resource=_resource_id(11)))]

    def GetReadWriteResources(self, stage, only_used):
        return []

    def GetConstantBlocks(self, stage):
        return [SimpleNamespace(name="cb0")]

    def GetConstantBlock(self, stage, index, array_index):
        self.controller.calls.append(("constant-block", str(stage), index, array_index))
        return SimpleNamespace(
            descriptor=SimpleNamespace(resource=_resource_id(21), byteOffset=0, byteSize=64)
        )

    def GetGraphicsPipelineObject(self):
        return _resource_id(41)

    def GetComputePipelineObject(self):
        return _resource_id(0)


def _fake_rd():
    return SimpleNamespace(
        ShaderStage=SimpleNamespace(
            Vertex=_Enum("ShaderStage.Vertex", 0),
            Hull=_Enum("ShaderStage.Hull", 1),
            Domain=_Enum("ShaderStage.Domain", 2),
            Geometry=_Enum("ShaderStage.Geometry", 3),
            Pixel=_Enum("ShaderStage.Pixel", 5),
            Compute=_Enum("ShaderStage.Compute", 6),
        ),
        MeshDataStage=SimpleNamespace(
            VSIn=_Enum("MeshDataStage.VSIn", 0),
            VSOut=_Enum("MeshDataStage.VSOut", 1),
            GSOut=_Enum("MeshDataStage.GSOut", 2),
        ),
        CompType=SimpleNamespace(
            Typeless=_Enum("CompType.Typeless", 0), Float=_Enum("CompType.Float", 1)
        ),
        FileType=SimpleNamespace(PNG=_Enum("FileType.PNG", 0), JPG=_Enum("FileType.JPG", 1)),
        AlphaMapping=SimpleNamespace(Preserve=_Enum("AlphaMapping.Preserve", 0)),
        ActionFlags=SimpleNamespace(Drawcall=1, Clear=8, SetMarker=16),
        ResourceId=_ResourceId,
        Subresource=lambda: SimpleNamespace(mip=0, slice=0, sample=0),
        TextureSave=lambda: SimpleNamespace(
            resourceId=_resource_id(0),
            destType=None,
            alpha=None,
            mip=0,
            slice=SimpleNamespace(sliceIndex=0),
            typeCast=None,
        ),
        DebugPixelInputs=lambda: SimpleNamespace(sample=0, primitive=0, view=0),
        ReplayOptions=lambda: SimpleNamespace(),
    )


class FakeContext:
    def __init__(self, controller):
        self.controller = controller
        self.loaded = None
        self.closed = False

    def LoadCapture(self, *args):
        self.loaded = args

    def Replay(self):
        return SimpleNamespace(BlockInvoke=self._invoke)

    def _invoke(self, callback):
        callback(self.controller)

    def CloseCapture(self):
        self.closed = True


def run_bridge(monkeypatch, tmp_path, operation, params, controller=None, status_path=None):
    status_path = status_path or tmp_path / "status.json"
    params_path = tmp_path / "params.json"
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    params_path.write_text(json.dumps(params), encoding="utf-8")
    monkeypatch.setitem(sys.modules, "renderdoc", _fake_rd())
    monkeypatch.setenv("DCC_MCP_RENDERDOC_CAPTURE", str(capture))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_OPERATION", operation)
    monkeypatch.setenv("DCC_MCP_RENDERDOC_REPLAY_PARAMS", str(params_path))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_REPLAY_STATUS", str(status_path))
    context = FakeContext(controller or FakeController())
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(BRIDGE), init_globals={"pyrenderdoc": context}, run_name="__main__")
    assert error.value.code == 0
    return json.loads(status_path.read_text(encoding="utf-8")), context


# --------------------------------------------------------------------------- #
# Host-side driver
# --------------------------------------------------------------------------- #


def test_clean_params_drops_unset_optional_arguments():
    assert clean_params(a=1, b=None, c="") == {"a": 1, "c": ""}


def test_unknown_operation_is_rejected_before_launch(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    monkeypatch.setattr(
        replay.subprocess, "run", lambda *_a, **_k: pytest.fail("must not launch qrenderdoc")
    )
    with pytest.raises(runtime.RenderDocError, match="unknown RenderDoc replay operation"):
        replay.run_replay_operation(str(capture), "not_an_operation")


def _command_root(tmp_path, *, with_qrenderdoc):
    tmp_path.mkdir(parents=True, exist_ok=True)
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    if with_qrenderdoc:
        command.with_name("qrenderdoc.exe").touch()
    return command


def test_capabilities_report_the_deep_backend_as_missing(monkeypatch, tmp_path):
    status = capabilities.probe(command=str(_command_root(tmp_path, with_qrenderdoc=False)))
    assert status["baseline"] == {
        "backend": "renderdoccmd",
        "available": True,
        "host": str(tmp_path / "renderdoccmd.exe"),
        "reason": None,
    }
    assert status["deep"]["backend"] == "renderdoc.pyd"
    assert status["deep"]["available"] is False
    assert status["deep"]["driver"] is None
    assert "qrenderdoc" in status["deep"]["reason"]
    assert status["capabilities"] == {
        "inspect": False,
        "debug": False,
        "perf": False,
        "ext": False,
    }
    assert "qrenderdoc" in status["hint"]


def test_capabilities_report_the_deep_backend_as_reachable(monkeypatch, tmp_path):
    command = _command_root(tmp_path, with_qrenderdoc=True)
    status = capabilities.probe(command=str(command))
    assert status["deep"]["available"] is True
    assert status["deep"]["driver"] == "qrenderdoc-bridge"
    assert status["deep"]["host"] == str(tmp_path / "qrenderdoc.exe")
    assert status["deep"]["reason"] is None
    assert status["capabilities"]["inspect"] is True
    assert "run_python_script" in status["deep"]["operations"]


def test_require_capability_explains_how_to_enable_the_backend(tmp_path):
    command = _command_root(tmp_path / "missing", with_qrenderdoc=False)
    with pytest.raises(runtime.RenderDocError, match="qrenderdoc"):
        capabilities.require("inspect", command=str(command))
    enabled = _command_root(tmp_path / "ready", with_qrenderdoc=True)
    assert capabilities.require("inspect", command=str(enabled))["deep"]["available"] is True
    assert capabilities.require(None, command=str(command))["deep"]["available"] is False


def test_missing_deep_backend_is_reported_before_launch(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    monkeypatch.setattr(
        replay.subprocess, "run", lambda *_a, **_k: pytest.fail("must not launch qrenderdoc")
    )
    command = _command_root(tmp_path, with_qrenderdoc=False)
    with pytest.raises(runtime.RenderDocError, match="needs the renderdoc.pyd replay backend"):
        replay.run_replay_operation(str(capture), "describe_capture", command=str(command))


def test_replay_launches_qrenderdoc_with_operation_and_params(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    command.with_name("qrenderdoc.exe").touch()
    observed = {}

    def fake_run(arguments, **kwargs):
        observed["arguments"] = arguments
        observed["env"] = kwargs["env"]
        params = json.loads(Path(kwargs["env"]["DCC_MCP_RENDERDOC_REPLAY_PARAMS"]).read_text())
        observed["params"] = params
        Path(kwargs["env"]["DCC_MCP_RENDERDOC_REPLAY_STATUS"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation": "list_actions",
                    "result": {"actions": []},
                    "error": None,
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    result = replay.run_replay_operation(
        str(capture), "list_actions", {"limit": 5}, command=str(command)
    )

    assert observed["arguments"][:2] == [str(command.with_name("qrenderdoc.exe")), "--python"]
    assert observed["arguments"][2].endswith("_replay_bridge.py")
    assert observed["env"]["DCC_MCP_RENDERDOC_OPERATION"] == "list_actions"
    assert observed["env"]["DCC_MCP_RENDERDOC_CAPTURE"] == str(capture)
    assert observed["params"] == {"limit": 5}
    assert result == {
        "capture_file": str(capture),
        "operation": "list_actions",
        "result": {"actions": []},
    }


def _host_status(monkeypatch, tmp_path, status, returncode=0, write=True):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    command.with_name("qrenderdoc.exe").touch()

    def fake_run(arguments, **kwargs):
        if write:
            Path(kwargs["env"]["DCC_MCP_RENDERDOC_REPLAY_STATUS"]).write_text(
                json.dumps(status), encoding="utf-8"
            )
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    return replay.run_replay_operation(str(capture), "describe_capture", command=str(command))


def test_replay_rejects_status_from_another_operation(monkeypatch, tmp_path):
    status = {"schema_version": 1, "operation": "list_actions", "result": {}, "error": None}
    with pytest.raises(runtime.RenderDocError, match="instead of 'describe_capture'"):
        _host_status(monkeypatch, tmp_path, status)


def test_replay_reports_bridge_error(monkeypatch, tmp_path):
    status = {
        "schema_version": 1,
        "operation": "describe_capture",
        "result": None,
        "error": "RuntimeError: boom",
    }
    with pytest.raises(runtime.RenderDocError, match="RenderDoc replay failed: RuntimeError: boom"):
        _host_status(monkeypatch, tmp_path, status)


def test_replay_rejects_invalid_status_schema(monkeypatch, tmp_path):
    status = {"schema_version": 1, "operation": "describe_capture", "result": {}}
    with pytest.raises(runtime.RenderDocError, match="invalid status schema"):
        _host_status(monkeypatch, tmp_path, status)


def test_replay_rejects_unsupported_status_version(monkeypatch, tmp_path):
    status = {"schema_version": 2, "operation": "describe_capture", "result": {}, "error": None}
    with pytest.raises(runtime.RenderDocError, match="unsupported status version"):
        _host_status(monkeypatch, tmp_path, status)


def test_replay_rejects_non_object_result(monkeypatch, tmp_path):
    status = {"schema_version": 1, "operation": "describe_capture", "result": [], "error": None}
    with pytest.raises(runtime.RenderDocError, match="did not return an object result"):
        _host_status(monkeypatch, tmp_path, status)


def test_replay_rejects_nonzero_host_exit_even_with_success_status(monkeypatch, tmp_path):
    status = {"schema_version": 1, "operation": "describe_capture", "result": {}, "error": None}
    with pytest.raises(runtime.RenderDocError, match="host exited with code 3"):
        _host_status(monkeypatch, tmp_path, status, returncode=3)


def test_replay_requires_a_status_file(monkeypatch, tmp_path):
    status = {"schema_version": 1, "operation": "describe_capture", "result": {}, "error": None}
    with pytest.raises(runtime.RenderDocError, match="did not write status"):
        _host_status(monkeypatch, tmp_path, status, write=False)


def test_replay_rejects_malformed_status_json(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    command.with_name("qrenderdoc.exe").touch()

    def fake_run(arguments, **kwargs):
        Path(kwargs["env"]["DCC_MCP_RENDERDOC_REPLAY_STATUS"]).write_text("{", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    with pytest.raises(runtime.RenderDocError, match="malformed status JSON"):
        replay.run_replay_operation(str(capture), "describe_capture", command=str(command))


def test_replay_reports_timeout_diagnostics(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    command.with_name("qrenderdoc.exe").touch()

    def fake_run(arguments, **kwargs):
        kwargs["stderr"].write("qrenderdoc blocked")
        raise subprocess.TimeoutExpired(arguments, 1)

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    with pytest.raises(runtime.RenderDocError, match="qrenderdoc blocked"):
        replay.run_replay_operation(str(capture), "describe_capture", command=str(command))


def test_replay_requires_the_bundled_bridge(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    command.with_name("qrenderdoc.exe").touch()
    monkeypatch.setattr(replay, "__file__", str(tmp_path / "not_replay.py"))
    with pytest.raises(runtime.RenderDocError, match="Bundled replay bridge is missing"):
        replay.run_replay_operation(str(capture), "describe_capture", command=str(command))


# --------------------------------------------------------------------------- #
# Bundled bridge
# --------------------------------------------------------------------------- #


def test_bridge_has_an_import_safe_main_guard(tmp_path):
    status_path = tmp_path / "status.json"
    os.environ["DCC_MCP_RENDERDOC_REPLAY_STATUS"] = str(status_path)
    try:
        with pytest.raises(SystemExit) as error:
            runpy.run_path(str(BRIDGE), run_name="__main__")
    finally:
        os.environ.pop("DCC_MCP_RENDERDOC_REPLAY_STATUS", None)
    assert error.value.code == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["error"] is not None


def test_bridge_rejects_unknown_operation(monkeypatch, tmp_path):
    status, _ = run_bridge(monkeypatch, tmp_path, "not_an_operation", {})
    assert "unknown replay operation" in status["error"]
    assert status["operation"] == "not_an_operation"


def test_bridge_describe_capture_sums_the_action_tree(monkeypatch, tmp_path):
    status, context = run_bridge(monkeypatch, tmp_path, "describe_capture", {})
    result = status["result"]
    assert status["error"] is None
    assert result["action_count"] == 4
    assert result["root_action_count"] == 2
    assert result["first_event_id"] == 1
    assert result["last_event_id"] == 4
    assert result["texture_count"] == 2
    assert result["buffer_count"] == 1
    assert result["api_properties"]["pipelineType"] == "GraphicsAPI.D3D11"
    assert context.closed is True


def test_bridge_list_actions_walks_filters_and_pages(monkeypatch, tmp_path):
    status, _ = run_bridge(monkeypatch, tmp_path, "list_actions", {})
    result = status["result"]
    assert [entry["event_id"] for entry in result["actions"]] == [1, 2, 3, 4]
    assert result["actions"][0]["flag_names"] == []
    assert result["actions"][1]["flag_names"] == ["Drawcall"]
    assert result["actions"][1]["depth"] == 1
    assert result["actions"][1]["parent_event_id"] == 1

    status, _ = run_bridge(monkeypatch, tmp_path, "list_actions", {"name_filter": "clear"})
    assert [entry["event_id"] for entry in status["result"]["actions"]] == [3]

    status, _ = run_bridge(monkeypatch, tmp_path, "list_actions", {"flag_filter": "drawcall"})
    assert [entry["event_id"] for entry in status["result"]["actions"]] == [2, 4]

    status, _ = run_bridge(monkeypatch, tmp_path, "list_actions", {"max_depth": 0})
    assert [entry["event_id"] for entry in status["result"]["actions"]] == [1, 4]

    status, _ = run_bridge(monkeypatch, tmp_path, "list_actions", {"offset": 1, "limit": 2})
    assert [entry["event_id"] for entry in status["result"]["actions"]] == [2, 3]


def test_bridge_list_actions_filters_before_paging(monkeypatch, tmp_path):
    controller = FakeController(
        actions=[
            _action(1, "Draw A", flags=1),
            _action(2, "Clear B", flags=8),
            _action(3, "Clear C", flags=8),
            _action(4, "Draw D", flags=1),
            _action(5, "Clear E", flags=8),
        ]
    )

    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "list_actions",
        {"name_filter": "clear", "limit": 2},
        controller=controller,
    )
    result = status["result"]
    assert [entry["event_id"] for entry in result["actions"]] == [2, 3]
    assert result["matched_count"] == 3
    assert result["truncated"] is True

    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "list_actions",
        {"name_filter": "clear", "offset": 1, "limit": 2},
        controller=controller,
    )
    result = status["result"]
    assert [entry["event_id"] for entry in result["actions"]] == [3, 5]
    assert result["matched_count"] == 3
    assert result["truncated"] is False

    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "list_actions",
        {"name_filter": "clear", "offset": 2, "limit": 1},
        controller=controller,
    )
    result = status["result"]
    assert [entry["event_id"] for entry in result["actions"]] == [5]
    assert result["matched_count"] == 3


def test_bridge_list_actions_can_start_from_a_parent(monkeypatch, tmp_path):
    status, _ = run_bridge(monkeypatch, tmp_path, "list_actions", {"parent_event_id": 1})
    assert [entry["event_id"] for entry in status["result"]["actions"]] == [2, 3]

    status, _ = run_bridge(monkeypatch, tmp_path, "list_actions", {"parent_event_id": 99})
    assert "event 99 was not found" in status["error"]


def test_bridge_get_action_returns_event_detail(monkeypatch, tmp_path):
    status, _ = run_bridge(monkeypatch, tmp_path, "get_action", {"event_id": 2})
    result = status["result"]
    assert result["name"] == "Draw A"
    assert result["num_indices"] == 3
    assert result["flag_names"] == ["Drawcall"]
    assert result["outputs"] == [7]

    status, _ = run_bridge(monkeypatch, tmp_path, "get_action", {"event_id": 99})
    assert "event 99 was not found" in status["error"]


def test_bridge_list_resources_merges_texture_and_buffer_facts(monkeypatch, tmp_path):
    status, _ = run_bridge(monkeypatch, tmp_path, "list_resources", {})
    result = status["result"]
    assert [entry["resource_id"] for entry in result["resources"]] == [11, 21]
    assert result["resources"][0]["type"] == "ResourceType.Texture"
    assert result["resources"][0]["width"] == 4
    assert result["resources"][0]["format"] == "R8G8B8A8_UNORM"
    assert result["resources"][1]["length"] == 64

    status, _ = run_bridge(monkeypatch, tmp_path, "list_resources", {"resource_type": "buffer"})
    assert [entry["resource_id"] for entry in status["result"]["resources"]] == [21]

    status, _ = run_bridge(monkeypatch, tmp_path, "list_resources", {"name_filter": "colour"})
    assert [entry["resource_id"] for entry in status["result"]["resources"]] == [11]


def test_bridge_get_resource_usage_reports_renderdoc_roles(monkeypatch, tmp_path):
    status, context = run_bridge(monkeypatch, tmp_path, "get_resource_usage", {"resource_id": 11})
    result = status["result"]
    assert result["usage"] == [{"event_id": 2, "usage": "ResourceUsage.ColorTarget"}]
    assert ("usage", 11) in context.controller.calls

    status, _ = run_bridge(monkeypatch, tmp_path, "get_resource_usage", {"resource_id": 0})
    assert "resource_id must be a positive integer" in status["error"]


def test_bridge_get_resource_usage_pages_the_event_list(monkeypatch, tmp_path):
    controller = FakeController()
    usage = [
        SimpleNamespace(eventId=event, usage=_Enum("ResourceUsage.ColorTarget", 16))
        for event in (2, 5, 9)
    ]
    controller.GetUsage = lambda resource_id: usage

    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "get_resource_usage",
        {"resource_id": 11, "limit": 2},
        controller=controller,
    )
    result = status["result"]
    assert [entry["event_id"] for entry in result["usage"]] == [2, 5]
    assert result["usage_count"] == 3
    assert result["truncated"] is True

    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "get_resource_usage",
        {"resource_id": 11, "offset": 2, "limit": 2},
        controller=controller,
    )
    result = status["result"]
    assert [entry["event_id"] for entry in result["usage"]] == [9]
    assert result["usage_count"] == 3
    assert result["truncated"] is False


def test_bridge_rejects_resource_id_absent_from_the_capture(monkeypatch, tmp_path):
    status, context = run_bridge(monkeypatch, tmp_path, "get_resource_usage", {"resource_id": 999})
    assert "resource 999 is not present in this capture" in status["error"]
    assert not any(call[0] == "usage" for call in context.controller.calls)


def test_bridge_get_pipeline_state_moves_the_replay_and_reports_bindings(monkeypatch, tmp_path):
    status, context = run_bridge(monkeypatch, tmp_path, "get_pipeline_state", {"event_id": 3})
    result = status["result"]
    assert ("set-event", 3, True) in context.controller.calls
    assert result["event_id"] == 3
    assert result["primitive_topology"] == "Topology.TriangleList"
    assert result["shaders"][4]["resource_id"] == 31
    assert result["shaders"][4]["entry_point"] == "PSMain"
    assert result["shaders"][0]["resource_id"] == 0
    assert result["output_targets"][0]["resource"] == 11
    assert result["depth_target"]["resource"] == 12
    assert result["read_only_resources"]["Pixel"][0]["descriptor"]["resource"] == 11
    assert result["unavailable_sections"] == []


def test_bridge_get_shader_info_reports_reflection(monkeypatch, tmp_path):
    status, _ = run_bridge(
        monkeypatch, tmp_path, "get_shader_info", {"event_id": 3, "stage": "Pixel"}
    )
    result = status["result"]
    assert result["bound"] is True
    assert result["resource_id"] == 31
    assert result["entry_point"] == "PSMain"
    assert result["encoding"] == "ShaderEncoding.DXBC"
    assert result["input_signature"][0]["semantic"] == "COLOR"
    assert result["constant_blocks"][0]["name"] == "cb0"
    assert result["read_only_resources"][0]["name"] == "albedo"


def test_bridge_get_shader_info_reports_unbound_stage(monkeypatch, tmp_path):
    controller = FakeController(shader_bound=False)
    status, _ = run_bridge(monkeypatch, tmp_path, "get_shader_info", {"event_id": 3}, controller)
    assert status["result"]["bound"] is False

    status, _ = run_bridge(monkeypatch, tmp_path, "get_shader_info", {"stage": "Nonsense"})
    assert "unsupported shader stage" in status["error"]


def test_bridge_get_texture_data_saves_through_renderdoc(monkeypatch, tmp_path):
    output = tmp_path / "out.png"
    status, context = run_bridge(
        monkeypatch, tmp_path, "get_texture_data", {"resource_id": 11, "output_file": str(output)}
    )
    result = status["result"]
    assert result["output_file"] == str(output)
    assert result["size_bytes"] == 3
    assert result["readback"] is None
    assert ("save-texture", 11, str(output)) in context.controller.calls

    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "get_texture_data",
        {"resource_id": 11, "output_file": str(tmp_path / "out.gif")},
    )
    assert "must use one of these extensions" in status["error"]


def test_bridge_get_texture_data_reads_pixels_without_export(monkeypatch, tmp_path):
    status, context = run_bridge(
        monkeypatch, tmp_path, "get_texture_data", {"resource_id": 11, "include_pixels": True}
    )
    result = status["result"]
    assert result["output_file"] is None
    assert result["readback"] == {"byte_length": 1, "preview_hex": "00"}
    assert not any(call[0] == "save-texture" for call in context.controller.calls)

    status, _ = run_bridge(monkeypatch, tmp_path, "get_texture_data", {"resource_id": 11})
    assert "provide output_file" in status["error"]


def test_bridge_get_shader_info_reads_constant_buffer_values(monkeypatch, tmp_path):
    controller = FakeController(buffer_bytes=b"\x00\x00\x80\x3f" * 4 + b"\x07\x00\x00\x00")
    status, context = run_bridge(
        monkeypatch,
        tmp_path,
        "get_shader_info",
        {"event_id": 3, "include_constant_buffers": True},
        controller,
    )
    blocks = status["result"]["constant_block_values"]
    assert blocks[0]["buffer_resource_id"] == 21
    assert blocks[0]["variables"][0]["name"] == "tint"
    assert blocks[0]["variables"][0]["values"] == [1.0, 1.0, 1.0, 1.0]
    assert blocks[0]["variables"][1]["name"] == "count"
    assert blocks[0]["variables"][1]["values"] == [7]
    assert ("constant-block", "ShaderStage.Pixel", 0, 0) in context.controller.calls


def test_bridge_get_buffer_data_previews_and_writes(monkeypatch, tmp_path):
    status, _ = run_bridge(monkeypatch, tmp_path, "get_buffer_data", {"resource_id": 21})
    result = status["result"]
    assert result["length"] == 4
    assert result["preview_hex"] == "01020304"
    assert result["preview_base64"] == "AQIDBA=="
    assert base64.b64decode(result["preview_base64"]) == b"\x01\x02\x03\x04"
    assert result["output_file"] is None

    output = tmp_path / "buffer.bin"
    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "get_buffer_data",
        {"resource_id": 21, "output_file": str(output), "preview_bytes": 2},
    )
    assert status["result"]["preview_hex"] == "0102"
    assert output.read_bytes() == b"\x01\x02\x03\x04"
    assert status["result"]["size_bytes"] == 4


def test_bridge_get_mesh_data_decodes_vertices(monkeypatch, tmp_path):
    controller = FakeController()
    controller.buffer_bytes = b"\x00\x00\x80\x3f" * 3 + b"\x00\x00\x00\x40" * 3
    status, _ = run_bridge(monkeypatch, tmp_path, "get_mesh_data", {"event_id": 2}, controller)
    result = status["result"]
    assert result["stage"] == "VSOut"
    assert result["vertex_resource_id"] == 21
    assert result["vertex_format"] == "R32G32B32_FLOAT"
    assert result["vertices"] == [[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]

    status, _ = run_bridge(monkeypatch, tmp_path, "get_mesh_data", {"stage": "Nonsense"})
    assert "stage must be one of these values" in status["error"]


def test_bridge_get_pixel_history_requires_support(monkeypatch, tmp_path):
    controller = FakeController(pixel_history=False)
    status, _ = run_bridge(
        monkeypatch, tmp_path, "get_pixel_history", {"resource_id": 11, "x": 1, "y": 2}, controller
    )
    assert "does not support pixel history" in status["error"]

    status, context = run_bridge(
        monkeypatch, tmp_path, "get_pixel_history", {"resource_id": 11, "x": 1, "y": 2}
    )
    result = status["result"]
    assert ("pixel-history", 11, 1, 2) in context.controller.calls
    assert result["modifications"][0]["event_id"] == 2
    assert result["modifications"][0]["passed"] is True
    assert result["modifications"][0]["post_mod"]["col"]["floatValue"][0] == 1.0


def test_bridge_debug_pixel_requires_support_and_frees_the_trace(monkeypatch, tmp_path):
    controller = FakeController(shader_debugging=False)
    status, _ = run_bridge(monkeypatch, tmp_path, "debug_pixel", {"x": 1, "y": 2}, controller)
    assert "does not support shader debugging" in status["error"]

    status, context = run_bridge(monkeypatch, tmp_path, "debug_pixel", {"x": 1, "y": 2})
    result = status["result"]
    assert result["stage"] == "ShaderStage.Pixel"
    assert result["steps"][0]["step_index"] == 0
    assert ("debug-pixel", 1, 2) in context.controller.calls
    assert ("free-trace",) in context.controller.calls


def test_bridge_get_counters_enumerates_and_fetches(monkeypatch, tmp_path):
    status, _ = run_bridge(monkeypatch, tmp_path, "get_counters", {})
    result = status["result"]
    assert [entry["id"] for entry in result["counters"]] == [1, 2]
    assert result["counters"][0]["unit"] == "CounterUnit.Seconds"
    assert result["values"] is None

    status, context = run_bridge(
        monkeypatch, tmp_path, "get_counters", {"event_id": 2, "fetch": True, "counter_ids": [1]}
    )
    result = status["result"]
    assert ("fetch-counters", [1]) in context.controller.calls
    assert result["values"][0]["event_id"] == 2
    assert result["values"][0]["value"]["f"] == 0.5


def test_bridge_get_debug_messages_reports_severity(monkeypatch, tmp_path):
    status, _ = run_bridge(monkeypatch, tmp_path, "get_debug_messages", {})
    result = status["result"]
    assert result["messages"][0]["severity"] == "MessageSeverity.Medium"
    assert result["messages"][0]["description"] == "slow"


def test_bridge_run_python_script_returns_result_and_stdout(monkeypatch, tmp_path):
    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "run_python_script",
        {"source": "print('hello')\nresult = {'ok': True}", "event_id": 2},
    )
    result = status["result"]
    assert status["error"] is None
    assert result["result"] == {"ok": True}
    assert result["stdout"] == "hello\n"
    assert result["has_result"] is True
    assert result["event_id"] == 2

    status, _ = run_bridge(monkeypatch, tmp_path, "run_python_script", {"source": "   "})
    assert "source is required" in status["error"]


def test_bridge_run_python_script_exposes_renderdoc_globals(monkeypatch, tmp_path):
    source = (
        "result = {\n"
        "    'controller': controller is not None,\n"
        "    'context': pyrenderdoc is not None,\n"
        "    'stage': str(rd.ShaderStage.Pixel),\n"
        "    'params': params,\n"
        "}\n"
    )
    status, _ = run_bridge(
        monkeypatch, tmp_path, "run_python_script", {"source": source, "args": {"a": 1}}
    )
    assert status["result"]["result"] == {
        "controller": True,
        "context": True,
        "stage": "ShaderStage.Pixel",
        "params": {"a": 1},
    }


# --------------------------------------------------------------------------- #
# Debug domain
# --------------------------------------------------------------------------- #


def _mesh_controller():
    """A controller whose post-VS stage reports both a vertex and an index buffer."""
    controller = FakeController()
    controller.buffer_bytes = b"\x00\x00\x80\x3f" * 3 + b"\x00\x00\x00\x40" * 3
    controller.GetPostVSData = lambda instance, view, stage: SimpleNamespace(
        status=_Enum("MeshDataStatus.Succeeded", 1),
        numIndices=3,
        topology=_Enum("Topology.TriangleList", 3),
        baseVertex=0,
        vertexResourceId=21,
        vertexByteOffset=0,
        vertexByteStride=12,
        vertexByteSize=24,
        indexResourceId=21,
        indexByteOffset=0,
        indexByteStride=2,
        instanced=False,
        unproject=True,
        nearPlane=0.0,
        farPlane=1.0,
        format=SimpleNamespace(
            Name=lambda: "R32G32B32_FLOAT",
            compType=_Enum("CompType.Float", 1),
            compCount=3,
        ),
    )
    return controller


def test_debug_operations_are_declared_and_implemented():
    debug_operations = set(capabilities.DEBUG_TOOLS.values())
    assert debug_operations <= set(replay.REPLAY_OPERATIONS)
    assert debug_operations <= set(capabilities.CAPABILITY_GROUPS["debug"])
    bridge = runpy.run_path(str(BRIDGE), run_name="dcc_mcp_renderdoc_bridge")
    assert set(replay.REPLAY_OPERATIONS) <= set(bridge["OPERATIONS"])


def test_debug_skill_declares_one_tool_per_script():
    root = Path(replay.__file__).parent / "skills" / "renderdoc-debug"
    tools = (root / "tools.yaml").read_text(encoding="utf-8")
    scripts = sorted(path.stem for path in (root / "scripts").glob("*.py"))
    assert scripts
    assert sorted(re.findall(r"^  - name: ([a-z_]+)$", tools, re.MULTILINE)) == scripts
    for name in scripts:
        assert "source_file: scripts/{}.py".format(name) in tools
    assert set(capabilities.DEBUG_TOOLS) <= set(scripts)


def test_unsupported_backend_report_names_how_to_enable_it(tmp_path):
    command = _command_root(tmp_path / "missing", with_qrenderdoc=False)
    assert replay.unsupported_backend("debug", command=str(command)) is not None
    report = replay.unsupported_backend("debug", command=str(command))
    assert report["supported"] is False
    assert report["capability_group"] == "debug"
    assert report["backend"] == "renderdoc.pyd"
    assert report["reason"]
    assert "qrenderdoc" in report["error_message"]
    assert report["hint"]
    ready = _command_root(tmp_path / "ready", with_qrenderdoc=True)
    assert replay.unsupported_backend("debug", command=str(ready)) is None


def test_run_debug_operation_reports_a_missing_backend_without_launching(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    monkeypatch.setattr(
        replay.subprocess, "run", lambda *_a, **_k: pytest.fail("must not launch qrenderdoc")
    )
    command = _command_root(tmp_path, with_qrenderdoc=False)
    report = replay.run_debug_operation(str(capture), "pick_pixel", command=str(command))
    assert report["supported"] is False
    assert report["error_message"]


def test_debug_capabilities_reports_backend_and_per_tool_flags(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    command = _command_root(tmp_path, with_qrenderdoc=True)

    def fake_run(arguments, **kwargs):
        Path(kwargs["env"]["DCC_MCP_RENDERDOC_REPLAY_STATUS"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation": "describe_capture",
                    "result": {
                        "capabilities": {
                            "available": True,
                            "pixel_history": True,
                            "shader_debugging": False,
                            "post_vs_data": True,
                        }
                    },
                    "error": None,
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    report = replay.debug_capabilities(str(capture), command=str(command))
    assert report["deep"]["available"] is True
    assert report["capture_checked"] is True
    assert report["capture"]["flags"]["shader_debugging"] is False
    assert report["tools"]["pixel_history"]["supported"] is True
    assert report["tools"]["debug_pixel"]["flag_state"] is False
    assert report["tools"]["debug_pixel"]["supported"] is False
    assert report["tools"]["debug_vertex"]["requires_flag"] == "shader_debugging"
    assert report["tools"]["export_mesh"]["supported"] is True
    assert report["tools"]["pick_pixel"]["requires_flag"] is None

    unchecked = replay.debug_capabilities(command=str(command))
    assert unchecked["capture"] is None
    assert unchecked["capture_checked"] is False
    assert unchecked["tools"]["debug_pixel"]["flag_state"] is None
    assert unchecked["tools"]["debug_pixel"]["supported"] is True

    missing = replay.debug_capabilities(
        command=str(_command_root(tmp_path / "off", with_qrenderdoc=False))
    )
    assert missing["tools"]["debug_pixel"]["supported"] is False
    assert missing["tools"]["debug_pixel"]["backend_available"] is False


def _load_debug_script(name):
    path = Path(replay.__file__).parent / "skills" / "renderdoc-debug" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location("renderdoc_debug_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_debug_scripts_report_an_unreachable_backend(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    monkeypatch.setattr(
        replay.subprocess, "run", lambda *_a, **_k: pytest.fail("must not launch qrenderdoc")
    )
    command = _command_root(tmp_path, with_qrenderdoc=False)
    monkeypatch.setattr(replay, "probe", lambda *a, **k: capabilities.probe(command=str(command)))
    result = _load_debug_script("pick_pixel").main(
        capture_file=str(capture), resource_id=11, x=1, y=2
    )
    assert result["success"] is False
    assert result["error"] == "unsupported_backend"
    assert "renderdoc.pyd" in result["message"]
    assert result["context"]["capability_group"] == "debug"
    assert result["context"]["hint"]


def test_debug_capabilities_script_succeeds_without_a_backend(monkeypatch, tmp_path):
    command = _command_root(tmp_path, with_qrenderdoc=False)
    monkeypatch.setattr(replay, "probe", lambda *a, **k: capabilities.probe(command=str(command)))
    result = _load_debug_script("debug_capabilities").main()
    assert result["success"] is True
    assert result["context"]["deep"]["available"] is False
    assert result["context"]["tools"]["debug_pixel"]["supported"] is False


def test_bridge_pick_pixel_reports_the_writing_draw(monkeypatch, tmp_path):
    status, context = run_bridge(
        monkeypatch, tmp_path, "pick_pixel", {"event_id": 3, "resource_id": 11, "x": 3, "y": 4}
    )
    result = status["result"]
    assert ("set-event", 3, True) in context.controller.calls
    assert ("pick-pixel", 11, 3, 4) in context.controller.calls
    assert result["hit_event_id"] == 2
    assert result["primitive_id"] == 1
    assert result["passed"] is True
    assert result["post_mod"]["col"]["floatValue"][0] == 1.0

    status, _ = run_bridge(monkeypatch, tmp_path, "pick_pixel", {"resource_id": 0, "x": 0, "y": 0})
    assert "resource_id must be a positive integer" in status["error"]


def test_bridge_debug_pixel_can_return_a_summary_without_steps(monkeypatch, tmp_path):
    status, context = run_bridge(
        monkeypatch, tmp_path, "debug_pixel", {"x": 1, "y": 2, "detail": "summary"}
    )
    result = status["result"]
    assert result["detail"] == "summary"
    assert result["step_count"] == 1
    assert "steps" not in result
    assert ("free-trace",) in context.controller.calls

    status, _ = run_bridge(monkeypatch, tmp_path, "debug_pixel", {"x": 1, "y": 2, "detail": "raw"})
    assert "detail must be 'trace' or 'summary'" in status["error"]


def test_bridge_debug_vertex_steps_one_vertex(monkeypatch, tmp_path):
    controller = FakeController(shader_debugging=False)
    status, _ = run_bridge(monkeypatch, tmp_path, "debug_vertex", {"vertex_index": 3}, controller)
    assert "does not support shader debugging" in status["error"]

    status, context = run_bridge(
        monkeypatch,
        tmp_path,
        "debug_vertex",
        {"event_id": 3, "vertex_index": 3, "instance": 2, "max_steps": 5},
    )
    result = status["result"]
    assert ("debug-vertex", 3, 2, 0, 0, 0) in context.controller.calls
    assert result["vertex_index"] == 3
    assert result["instance"] == 2
    assert result["stage"] == "ShaderStage.Pixel"
    assert result["steps"][0]["step_index"] == 0
    assert ("free-trace",) in context.controller.calls


def test_bridge_debug_thread_steps_one_compute_thread(monkeypatch, tmp_path):
    controller = FakeController(shader_debugging=False)
    status, _ = run_bridge(monkeypatch, tmp_path, "debug_thread", {"event_id": 3}, controller)
    assert "does not support shader debugging" in status["error"]

    status, context = run_bridge(
        monkeypatch,
        tmp_path,
        "debug_thread",
        {"event_id": 3, "group_id": [1, 2, 3], "thread_id": [4, 5]},
    )
    result = status["result"]
    assert ("debug-thread", [1, 2, 3], [4, 5, 0]) in context.controller.calls
    assert result["group_id"] == [1, 2, 3]
    assert result["thread_id"] == [4, 5, 0]
    assert result["steps"][0]["step_index"] == 0
    assert ("free-trace",) in context.controller.calls

    status, _ = run_bridge(monkeypatch, tmp_path, "debug_thread", {"group_id": "nope"})
    assert "must be a list of three integers" in status["error"]


def test_bridge_export_mesh_writes_obj(monkeypatch, tmp_path):
    output = tmp_path / "mesh.obj"
    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "export_mesh",
        {"event_id": 2, "output_file": str(output)},
        _mesh_controller(),
    )
    result = status["result"]
    assert status["error"] is None
    assert result["output_format"] == "obj"
    assert result["vertex_count"] == 2
    assert result["index_count"] == 3
    assert result["vertex_preview"] == [[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]
    lines = output.read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.startswith("v ")] == [
        "v 1.000000 1.000000 1.000000",
        "v 2.000000 2.000000 2.000000",
    ]
    assert len([line for line in lines if line.startswith("f ")]) == 1
    assert result["size_bytes"] == output.stat().st_size


def test_bridge_export_mesh_writes_json(monkeypatch, tmp_path):
    output = tmp_path / "mesh.json"
    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "export_mesh",
        {"event_id": 2, "output_file": str(output), "preview_vertices": 1},
        _mesh_controller(),
    )
    result = status["result"]
    assert result["output_format"] == "json"
    assert result["vertex_preview"] == [[1.0, 1.0, 1.0]]
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["event_id"] == 2
    assert document["stage"] == "VSOut"
    assert document["vertices"] == [[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]
    assert len(document["indices"]) == 3
    assert document["vertex_format"] == "R32G32B32_FLOAT"


def test_bridge_export_mesh_validates_its_request(monkeypatch, tmp_path):
    status, _ = run_bridge(monkeypatch, tmp_path, "export_mesh", {"event_id": 2})
    assert "output_file is required" in status["error"]

    status, _ = run_bridge(
        monkeypatch, tmp_path, "export_mesh", {"output_file": str(tmp_path / "mesh.stl")}
    )
    assert "must use one of these extensions" in status["error"]

    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "export_mesh",
        {"output_file": str(tmp_path / "mesh.obj"), "stage": "Nonsense"},
    )
    assert "stage must be one of these values" in status["error"]

    status, _ = run_bridge(
        monkeypatch,
        tmp_path,
        "export_mesh",
        {"output_file": str(tmp_path / "mesh.obj")},
        FakeController(post_vs_data=False),
    )
    assert "does not support post-VS data" in status["error"]


def test_bridge_reports_post_vs_data_capability(monkeypatch, tmp_path):
    status, _ = run_bridge(monkeypatch, tmp_path, "describe_capture", {})
    assert status["result"]["capabilities"]["post_vs_data"] is True

    controller = FakeController()
    controller.GetAPIProperties = lambda: SimpleNamespace(
        pipelineType=_Enum("GraphicsAPI.D3D11", 2),
        localRenderer=False,
        degraded=True,
        pixelHistory=True,
        shaderDebugging=True,
    )
    status, _ = run_bridge(monkeypatch, tmp_path, "describe_capture", {}, controller)
    assert status["result"]["capabilities"]["post_vs_data"] is False
    assert status["result"]["capabilities"]["degraded"] is True


# --------------------------------------------------------------------------- #
# renderdoccmd-level conversion
# --------------------------------------------------------------------------- #


def test_convert_capture_requires_a_known_format(tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    with pytest.raises(runtime.RenderDocError, match="convert_format must be one of"):
        runtime.convert_capture(str(capture), str(tmp_path / "out.xml"), convert_format="obj")


def test_convert_capture_requires_a_matching_extension(tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    with pytest.raises(runtime.RenderDocError, match="Output must use one of these extensions"):
        runtime.convert_capture(
            str(capture), str(tmp_path / "out.xml"), convert_format="chrome.json"
        )


def test_convert_capture_runs_renderdoc_convert(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    output = tmp_path / "out.xml"
    observed = {}

    def fake_run(arguments, **kwargs):
        observed["arguments"] = arguments
        output.write_text("<capture/>")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runtime, "_run", fake_run)
    result = runtime.convert_capture(str(capture), str(output))
    assert observed["arguments"][0] == "convert"
    assert observed["arguments"][-1] == "xml"
    assert result["convert_format"] == "xml"
    assert result["size_bytes"] == output.stat().st_size


def test_convert_capture_rejects_missing_output(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    monkeypatch.setattr(
        runtime, "_run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    with pytest.raises(runtime.RenderDocError, match="did not create the converted file"):
        runtime.convert_capture(str(capture), str(tmp_path / "out.xml"))


def test_convert_capture_rejects_a_stale_output_file(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    output = tmp_path / "out.xml"
    output.write_text("<stale/>")
    monkeypatch.setattr(
        runtime, "_run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    with pytest.raises(runtime.RenderDocError, match="did not create the converted file"):
        runtime.convert_capture(str(capture), str(output))
    assert not output.is_file()


def test_inspect_skill_declares_one_tool_per_script():
    root = Path(replay.__file__).parent / "skills" / "renderdoc-inspect"
    tools = (root / "tools.yaml").read_text(encoding="utf-8")
    scripts = sorted(path.stem for path in (root / "scripts").glob("*.py"))
    assert scripts
    assert sorted(re.findall(r"^  - name: ([a-z_]+)$", tools, re.MULTILINE)) == scripts
    for name in scripts:
        assert "source_file: scripts/{}.py".format(name) in tools
