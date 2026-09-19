"""Coverage for the renderdoc-analysis replay operations.

The fake RenderDoc controller this module builds extends the one the existing
replay suite uses: it keeps its action tree and pipeline state, and adds the
two things the analysis ops read that nothing else did before -- a texture
format description detailed enough to decode, and crafted texel bytes.
"""

from __future__ import annotations

import importlib.util
import runpy
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_package import load_skill_manifest
from test_replay import BRIDGE, FakeController, _command_root, _texture, run_bridge

from dcc_mcp_renderdoc import capabilities, replay

ANALYSIS_OPERATIONS = (
    "sample_pixel_region",
    "diagnose_pixel_values",
    "get_frame_overview",
    "get_draw_call_state",
    "analyze_render_passes",
    "analyze_state_changes",
)
#: Operations gated on the perf capability group rather than inspect.
PERF_ANALYSIS_OPERATIONS = ("get_pass_timing",)


def _format(name="R32G32B32A32_FLOAT", comp_type="CompType.Float", count=4, width=4, special=0):
    """Stand-in for one RenderDoc ``TextureFormat``."""
    return SimpleNamespace(
        Name=lambda: name,
        compType=comp_type,
        compCount=count,
        compByteWidth=width,
        elementByteSize=count * width,
        special=special,
    )


def _float_texture(resource_id, name, width, height, values):
    """One RGBA32F texture whose texels come from ``values``."""
    texture = _texture(resource_id, name, width, height)
    texture.format = _format()
    return texture, b"".join(struct.pack("<4f", *value) for value in values)


class AnalysisController(FakeController):
    """FakeController with a readable texture and a texel payload."""

    def __init__(self, data=b"", textures=None, **overrides):
        super().__init__(**overrides)
        self.region_data = data
        if textures is not None:
            self.textures = textures

    def GetTextureData(self, texture, sub):
        return self.region_data


def _run(monkeypatch, tmp_path, operation, params, controller):
    status, _context = run_bridge(monkeypatch, tmp_path, operation, params, controller=controller)
    assert status["error"] is None, status["error"]
    return status["result"]


def test_analysis_operations_are_declared_and_implemented():
    assert set(ANALYSIS_OPERATIONS) <= set(replay.REPLAY_OPERATIONS)
    assert set(ANALYSIS_OPERATIONS) <= set(capabilities.CAPABILITY_GROUPS["inspect"])
    assert set(PERF_ANALYSIS_OPERATIONS) <= set(replay.REPLAY_OPERATIONS)
    assert set(PERF_ANALYSIS_OPERATIONS) <= set(capabilities.CAPABILITY_GROUPS["perf"])
    bridge = runpy.run_path(str(BRIDGE), run_name="dcc_mcp_renderdoc_bridge")
    assert set(replay.REPLAY_OPERATIONS) <= set(bridge["OPERATIONS"])


# --------------------------------------------------------------------------- #
# sample_pixel_region
# --------------------------------------------------------------------------- #


def _four_texels():
    """2x2 RGBA32F: one ordinary, one negative, one NaN, one huge."""
    return [
        (0.0, 0.25, 0.5, 1.0),
        (-1.0, 0.0, 0.0, 1.0),
        (float("nan"), 0.0, 0.0, 1.0),
        (4.0, 0.0, 0.0, 1.0),
    ]


def test_sample_pixel_region_decodes_every_texel_of_a_covering_grid(monkeypatch, tmp_path):
    texture, data = _float_texture(11, "colour", 2, 2, _four_texels())
    controller = AnalysisController(data, textures=[texture])
    result = _run(
        monkeypatch,
        tmp_path,
        "sample_pixel_region",
        {"resource_id": 11, "grid_x": 2, "grid_y": 2},
        controller,
    )
    assert result["supported"] is True
    # A 2x2 grid over a 2x2 region reads every texel, so nothing is estimated.
    assert result["estimate"] is False
    assert result["estimate_method"] is None
    assert result["sample_count"] == 4
    assert result["stats"]["texel_count"] == 4
    channels = result["stats"]["channels"]
    assert channels[0]["min"] == -1.0
    assert channels[0]["max"] == 4.0
    assert channels[1]["mean"] == pytest.approx(0.25 / 4)
    # The NaN texel is counted, not folded into the statistics.
    assert channels[0]["nan_count"] == 1
    assert channels[0]["finite_texel_count"] == 3


def test_sample_pixel_region_marks_a_coarse_grid_as_an_estimate(monkeypatch, tmp_path):
    texture, data = _float_texture(11, "colour", 2, 2, _four_texels())
    controller = AnalysisController(data, textures=[texture])
    result = _run(
        monkeypatch,
        tmp_path,
        "sample_pixel_region",
        {"resource_id": 11, "grid_x": 1, "grid_y": 1},
        controller,
    )
    assert result["supported"] is True
    assert result["sampled"] is True
    assert result["estimate"] is True
    assert "1x1 grid" in result["estimate_method"]
    assert result["sample_count"] == 1
    assert result["region_texel_count"] == 4
    assert result["samples"][0]["x"] == 1
    assert result["samples"][0]["y"] == 1


def test_sample_pixel_region_clamps_the_region_into_the_level(monkeypatch, tmp_path):
    texture, data = _float_texture(11, "colour", 2, 2, _four_texels())
    controller = AnalysisController(data, textures=[texture])
    result = _run(
        monkeypatch,
        tmp_path,
        "sample_pixel_region",
        {"resource_id": 11, "x": 1, "y": 1, "width": 8, "height": 8, "grid_x": 2, "grid_y": 2},
        controller,
    )
    region = result["region"]
    assert region["width"] == 1
    assert region["height"] == 1
    assert result["sample_count"] == 4


def test_sample_pixel_region_reports_an_undecodable_format(monkeypatch, tmp_path):
    texture, data = _float_texture(11, "bc7", 2, 2, _four_texels())
    texture.format = _format(name="BC7_UNORM", comp_type="CompType.UNorm", special=1)
    controller = AnalysisController(data, textures=[texture])
    result = _run(monkeypatch, tmp_path, "sample_pixel_region", {"resource_id": 11}, controller)
    assert result["supported"] is False
    assert "special-encoded" in result["error_message"]
    assert result["hint"]
    assert result["samples"] == []
    assert result["stats"] is None


def test_sample_pixel_region_reports_a_short_readback(monkeypatch, tmp_path):
    texture, data = _float_texture(11, "colour", 4, 4, _four_texels())
    controller = AnalysisController(data, textures=[texture])
    result = _run(monkeypatch, tmp_path, "sample_pixel_region", {"resource_id": 11}, controller)
    assert result["supported"] is False
    assert "returned 64 byte(s) for a 4x4 level" in result["error_message"]


# --------------------------------------------------------------------------- #
# diagnose_pixel_values
# --------------------------------------------------------------------------- #


def test_diagnose_pixel_values_flags_nan_inf_and_negative(monkeypatch, tmp_path):
    texture, data = _float_texture(11, "colour", 2, 2, _four_texels())
    controller = AnalysisController(data, textures=[texture])
    result = _run(monkeypatch, tmp_path, "diagnose_pixel_values", {"resource_id": 11}, controller)
    assert result["supported"] is True
    assert result["scanned_texels"] == 4
    # The NaN texel and the negative texel; the 4.0 texel is ordinary.
    assert result["anomaly_texel_count"] == 2
    assert result["clean"] is False
    checks = result["checks"]
    assert checks["nan"]["count"] == 1
    assert checks["nan"]["samples"][0]["x"] == 0
    assert checks["nan"]["samples"][0]["y"] == 1
    # Infinity is absent from this texture, so the check reports zero rather
    # than silently dropping out of the report.
    assert checks["inf"]["count"] == 0
    assert checks["negative"]["count"] == 1
    assert checks["negative"]["samples"][0]["value"] == -1.0
    assert checks["precision"]["count"] == 0


def test_diagnose_pixel_values_flags_out_of_band_magnitudes(monkeypatch, tmp_path):
    values = [
        (1e-30, 0.0, 0.0, 1.0),
        (1e30, 0.0, 0.0, 1.0),
        (1.0, 0.0, 0.0, 1.0),
        (0.0, 0.0, 0.0, 1.0),
    ]
    texture, data = _float_texture(11, "colour", 2, 2, values)
    controller = AnalysisController(data, textures=[texture])
    result = _run(
        monkeypatch,
        tmp_path,
        "diagnose_pixel_values",
        {"resource_id": 11, "checks": ["precision"]},
        controller,
    )
    assert result["checks_requested"] == ["precision"]
    precision = result["checks"]["precision"]
    assert precision["tiny_count"] == 1
    assert precision["huge_count"] == 1
    assert {sample["kind"] for sample in precision["samples"]} == {"tiny", "huge"}
    assert result["anomaly_texel_count"] == 2


def test_diagnose_pixel_values_marks_integer_checks_inapplicable(monkeypatch, tmp_path):
    texture = _texture(11, "colour", 2, 2)
    texture.format = _format(name="R8G8B8A8_UNORM", comp_type="CompType.UNorm", count=4, width=1)
    controller = AnalysisController(b"\x80" * 32, textures=[texture])
    result = _run(monkeypatch, tmp_path, "diagnose_pixel_values", {"resource_id": 11}, controller)
    for name in ("nan", "inf", "negative", "precision"):
        check = result["checks"][name]
        assert check["applicable"] is False
        assert check["reason"]
        assert check["count"] == 0
        assert check["samples"] == []
    assert result["anomaly_texel_count"] == 0
    assert result["clean"] is True


def test_diagnose_pixel_values_strides_a_large_region_and_says_so(monkeypatch, tmp_path):
    values = [(0.0, 0.0, 0.0, 1.0)] * 1024
    texture, data = _float_texture(11, "colour", 32, 32, values)
    controller = AnalysisController(data, textures=[texture])
    result = _run(
        monkeypatch,
        tmp_path,
        "diagnose_pixel_values",
        {"resource_id": 11, "max_texels": 64},
        controller,
    )
    assert result["region"]["step"] == 4
    assert result["sampled"] is True
    assert result["estimate"] is True
    assert "every 4. texel" in result["estimate_method"]
    assert result["scanned_texels"] == 64
    assert result["region_texel_count"] == 1024


def test_diagnose_pixel_values_rejects_an_empty_check_list(monkeypatch, tmp_path):
    texture, data = _float_texture(11, "colour", 2, 2, _four_texels())
    controller = AnalysisController(data, textures=[texture])
    status, _context = run_bridge(
        monkeypatch,
        tmp_path,
        "diagnose_pixel_values",
        {"resource_id": 11, "checks": ["nope"]},
        controller=controller,
    )
    assert "checks must name at least one of" in status["error"]


# --------------------------------------------------------------------------- #
# get_frame_overview
# --------------------------------------------------------------------------- #


def test_get_frame_overview_joins_structure_resources_and_signals(monkeypatch, tmp_path):
    texture, data = _float_texture(11, "colour", 2, 2, _four_texels())
    controller = AnalysisController(data, textures=[texture])
    result = _run(monkeypatch, tmp_path, "get_frame_overview", {}, controller)
    assert result["actions"]["action_count"] == 4
    assert result["actions"]["draw_count"] == 2
    assert result["actions"]["clear_count"] == 1
    assert result["pass_count"] == 2
    assert result["passes"][0]["event_id"] == 1
    assert result["passes"][0]["draw_count"] == 1
    assert result["passes"][0]["triangle_estimate"] == 1
    assert result["resources"]["texture_count"] == 1
    # 2x2 RGBA, four bytes per texel: the fake texture reports no byteSize, so
    # the inventory derives it from the dimensions.
    assert result["resources"]["total_texture_bytes"] == 16
    assert result["counters"]["counter_count"] == 2
    assert result["counters"]["timing_counter"]["name"] == "duration"
    assert result["debug_messages"]["count"] == 1
    signals = {signal["code"] for signal in result["signals"]}
    assert "debug_messages" in signals
    assert all(signal["basis"] == "heuristic" for signal in result["signals"])
    assert "signals" in result["estimate_fields"]
    assert "passes[].triangle_estimate" in result["estimate_fields"]


def test_get_frame_overview_signals_a_driver_without_a_timing_counter(monkeypatch, tmp_path):
    controller = AnalysisController()
    monkeypatch.setattr(controller, "EnumerateCounters", lambda: [])
    result = _run(monkeypatch, tmp_path, "get_frame_overview", {}, controller)
    codes = {signal["code"] for signal in result["signals"]}
    assert "no_counters" in codes
    assert "no_timing_counter" not in codes


def test_get_frame_overview_can_skip_debug_messages(monkeypatch, tmp_path):
    controller = AnalysisController()
    result = _run(
        monkeypatch,
        tmp_path,
        "get_frame_overview",
        {"include_debug_messages": False, "max_passes": 1},
        controller,
    )
    assert result["debug_messages"]["count"] is None
    assert result["passes_truncated"] is True
    assert len(result["passes"]) == 1


# --------------------------------------------------------------------------- #
# get_draw_call_state
# --------------------------------------------------------------------------- #


def test_get_draw_call_state_joins_action_pipeline_and_shaders(monkeypatch, tmp_path):
    controller = AnalysisController()
    result = _run(monkeypatch, tmp_path, "get_draw_call_state", {"event_id": 2}, controller)
    assert result["event_id"] == 2
    # The action tree puts "Draw A" one level below the frame root.
    assert result["action"]["depth"] == 1
    assert result["action"]["parent_event_id"] == 1
    assert result["action"]["name"] == "Draw A"
    assert result["pipeline_state"]["event_id"] == 2
    assert [entry["requested_stage"] for entry in result["shaders"]] == ["Pixel"]
    assert result["shaders"][0]["bound"] is True
    assert result["summary"]["shader_count"] == 1
    assert result["summary"]["texture_binding_count"] == 6
    assert result["summary"]["vertex_buffer_count"] == 1
    assert result["summary"]["num_indices"] == 3


def test_get_draw_call_state_reports_a_stage_that_cannot_be_reflected(monkeypatch, tmp_path):
    controller = AnalysisController()
    result = _run(
        monkeypatch,
        tmp_path,
        "get_draw_call_state",
        {"event_id": 2, "stages": ["Vertex", "Pixel"]},
        controller,
    )
    assert [entry["requested_stage"] for entry in result["shaders"]] == ["Vertex", "Pixel"]
    assert result["shaders"][0]["bound"] is False


def test_get_draw_call_state_rejects_an_unknown_event(monkeypatch, tmp_path):
    controller = AnalysisController()
    status, _context = run_bridge(
        monkeypatch, tmp_path, "get_draw_call_state", {"event_id": 99}, controller=controller
    )
    assert "event 99 was not found" in status["error"]


# --------------------------------------------------------------------------- #
# analyze_render_passes
# --------------------------------------------------------------------------- #


def test_analyze_render_passes_reports_roots_and_their_load(monkeypatch, tmp_path):
    result = _run(monkeypatch, tmp_path, "analyze_render_passes", {}, AnalysisController())
    assert result["pass_depth"] == 0
    assert result["pass_count"] == 2
    assert result["passes"][0]["event_id"] == 1
    assert result["passes"][0]["name"] == "Frame"
    assert result["passes"][0]["draw_count"] == 1
    assert result["passes"][0]["clear_count"] == 1
    # The clear contributes no geometry, so the pass carries one triangle.
    assert result["passes"][0]["triangle_estimate"] == 1
    assert result["passes"][0]["outputs"] == [7]
    assert result["passes"][1]["event_id"] == 4
    assert result["totals"]["draw_count"] == 2
    assert result["totals"]["clear_count"] == 1
    assert result["estimate_fields"]["passes[].triangle_estimate"]


def test_analyze_render_passes_can_select_a_deeper_level(monkeypatch, tmp_path):
    result = _run(
        monkeypatch, tmp_path, "analyze_render_passes", {"pass_depth": 1}, AnalysisController()
    )
    assert result["pass_depth"] == 1
    assert [entry["event_id"] for entry in result["passes"]] == [2, 3]
    assert [entry["name"] for entry in result["passes"]] == ["Draw A", "Clear B"]
    # Totals stay over the whole frame, not over the selected level twice.
    assert result["totals"]["action_count"] == 4


def test_analyze_render_passes_filters_by_name(monkeypatch, tmp_path):
    result = _run(
        monkeypatch,
        tmp_path,
        "analyze_render_passes",
        {"name_filter": "draw"},
        AnalysisController(),
    )
    # At pass_depth 0 only the root actions are passes, so the filter matches
    # the root named "Draw C" and not the nested "Draw A".
    assert [entry["event_id"] for entry in result["passes"]] == [4]
    assert result["name_filter"] == "draw"


def test_pass_names_come_from_the_structured_file_before_custom_name(monkeypatch, tmp_path):
    """A pass named only in the structured file must still be named and matchable.

    RenderDoc markers often carry no ``customName``; their readable name is
    resolved through ``GetName(structured)``. Reading only ``customName`` made
    the same event report different names in different tools, and made
    ``name_filter`` silently drop passes.
    """

    def named(root):
        for action in root:
            label = action.GetName(None)
            action.customName = ""
            action.GetName = lambda _structured, label=label: label
            named(getattr(action, "children", []) or [])
        return root

    controller = AnalysisController(actions=named(AnalysisController().actions))
    overview = _run(monkeypatch, tmp_path, "get_frame_overview", {}, controller)
    assert [entry["name"] for entry in overview["passes"]] == ["Frame", "Draw C"]

    passes = _run(monkeypatch, tmp_path, "analyze_render_passes", {}, controller)
    assert [entry["name"] for entry in passes["passes"]] == ["Frame", "Draw C"]
    # The filter runs on the resolved name, not on the empty customName.
    filtered = _run(
        monkeypatch,
        tmp_path,
        "analyze_render_passes",
        {"name_filter": "draw c"},
        controller,
    )
    assert [entry["event_id"] for entry in filtered["passes"]] == [4]


# --------------------------------------------------------------------------- #
# analyze_state_changes
# --------------------------------------------------------------------------- #


class SwitchingController(AnalysisController):
    """A controller whose pipeline state changes on the second draw."""

    def __init__(self, **overrides):
        super().__init__(**overrides)
        self.events = []

    def GetPipelineState(self):
        return SwitchingPipeState(self)


def _blend(enabled):
    return SimpleNamespace(enabled=enabled)


class SwitchingPipeState:
    def __init__(self, controller):
        self.controller = controller

    def GetShader(self, stage):
        return 31

    def GetPrimitiveTopology(self):
        return "Topology.TriangleList"

    def GetGraphicsPipelineObject(self):
        return 41

    def GetComputePipelineObject(self):
        return 0

    def GetOutputTargets(self):
        return [SimpleNamespace(resource=11)]

    def GetDepthTarget(self):
        return SimpleNamespace(resource=12)

    def GetVBuffers(self):
        return [SimpleNamespace(resource=21)]

    def GetIBuffer(self):
        return SimpleNamespace(resource=0)

    def GetViewport(self, index):
        return SimpleNamespace(width=4, height=4)

    def GetScissor(self, index):
        return SimpleNamespace(width=4, height=4)

    def GetColorBlends(self):
        # The third draw repeats the first, so the state switch is a round trip.
        steps = [False, True, False]
        return [_blend(steps[min(len(self.controller.calls) - 1, len(steps) - 1)])]

    def GetDepthTestState(self):
        return SimpleNamespace(depthEnable=True)

    def GetRasterState(self):
        return SimpleNamespace(cullMode="CullMode.NoCull")


def test_analyze_state_changes_diffs_adjacent_draws(monkeypatch, tmp_path):
    controller = SwitchingController()
    result = _run(monkeypatch, tmp_path, "analyze_state_changes", {}, controller)
    assert result["draw_count"] == 2
    assert result["analyzed_event_count"] == 2
    assert result["analyzed_events"] == [2, 4]
    assert result["change_count"] == 1
    assert result["changes"][0]["key"] == "blend_enabled"
    assert result["changes"][0]["before"] == [False]
    assert result["changes"][0]["after"] == [True]
    assert result["changes_by_key"] == [{"key": "blend_enabled", "count": 1}]
    assert result["runs"] == []


def test_analyze_state_changes_reports_state_that_cannot_be_read(monkeypatch, tmp_path):
    controller = AnalysisController()

    def broken():
        raise RuntimeError("state section unavailable")

    monkeypatch.setattr(controller, "GetPipelineState", broken)
    result = _run(monkeypatch, tmp_path, "analyze_state_changes", {}, controller)
    assert result["analyzed_event_count"] == 2
    assert result["change_count"] == 0
    assert result["unavailable_sections"]
    assert "state.event_2" in result["unavailable_sections"][0]


# --------------------------------------------------------------------------- #
# get_pass_timing
# --------------------------------------------------------------------------- #


def test_get_pass_timing_joins_the_timing_counter_onto_passes(monkeypatch, tmp_path):
    controller = AnalysisController()
    samples = {1: 4.0, 2: 3.0, 3: 1.0, 4: 2.0}
    monkeypatch.setattr(
        controller,
        "FetchCounters",
        lambda counters: [
            SimpleNamespace(counter=1, eventId=event, value=SimpleNamespace(f=value))
            for event, value in samples.items()
        ],
    )
    result = _run(monkeypatch, tmp_path, "get_pass_timing", {}, controller)
    assert result["supported"] is True
    assert result["timing_counter"]["name"] == "duration"
    assert result["pass_count"] == 2
    by_event = {entry["event_id"]: entry for entry in result["passes"]}
    # Both root events carry their own sample, so neither is a derived sum.
    assert by_event[1]["duration"] == 4.0
    assert by_event[1]["duration_method"] == "pass_event_counter"
    assert by_event[1]["derived"] is False
    assert by_event[4]["duration"] == 2.0
    assert by_event[1]["share_percent"] == pytest.approx(66.666, rel=1e-3)
    assert by_event[1]["untimed_action_count"] == 0
    assert result["totals"]["pass_total"] == 6.0
    assert result["estimate_fields"]["passes[].duration"]


def test_get_pass_timing_falls_back_to_summing_timed_actions(monkeypatch, tmp_path):
    controller = AnalysisController()
    # The frame root is not sampled, only the draws inside it are.
    samples = {2: 3.0, 4: 2.0}
    monkeypatch.setattr(
        controller,
        "FetchCounters",
        lambda counters: [
            SimpleNamespace(counter=1, eventId=event, value=SimpleNamespace(f=value))
            for event, value in samples.items()
        ],
    )
    result = _run(monkeypatch, tmp_path, "get_pass_timing", {}, controller)
    by_event = {entry["event_id"]: entry for entry in result["passes"]}
    assert by_event[1]["duration"] == 3.0
    assert by_event[1]["duration_method"] == "sum_of_timed_actions"
    assert by_event[1]["derived"] is True
    assert by_event[1]["untimed_action_count"] == 2
    assert by_event[4]["duration"] == 2.0


def test_get_pass_timing_reports_a_driver_without_a_timing_counter(monkeypatch, tmp_path):
    controller = AnalysisController()
    monkeypatch.setattr(controller, "EnumerateCounters", lambda: [])
    result = _run(monkeypatch, tmp_path, "get_pass_timing", {}, controller)
    assert result["supported"] is False
    assert "no GPU timing counter" in result["error_message"]
    assert result["available_counters"] == []
    assert result["passes"] == []


# --------------------------------------------------------------------------- #
# Skill surface
# --------------------------------------------------------------------------- #


def _load_analysis_script(name):
    path = Path(replay.__file__).parent / "skills" / "renderdoc-analysis" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location("renderdoc_analysis_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_analysis_skill_declares_one_tool_per_script():
    root = Path(replay.__file__).parent / "skills" / "renderdoc-analysis"
    manifest = load_skill_manifest(root)
    scripts = sorted(path.stem for path in (root / "scripts").glob("*.py"))
    assert scripts
    assert sorted(entry["name"] for entry in manifest["tools"]) == scripts
    for entry in manifest["tools"]:
        assert entry["source_file"] == "scripts/{}.py".format(entry["name"])
    assert set(ANALYSIS_OPERATIONS) | set(PERF_ANALYSIS_OPERATIONS) <= set(scripts)


def test_analysis_scripts_report_an_unreachable_backend(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    monkeypatch.setattr(
        replay.subprocess, "run", lambda *_a, **_k: pytest.fail("must not launch qrenderdoc")
    )
    command = _command_root(tmp_path / "missing", with_qrenderdoc=False)
    monkeypatch.setattr(replay, "probe", lambda *a, **k: capabilities.probe(command=str(command)))
    calls = {
        "sample_pixel_region": {"resource_id": 11},
        "diagnose_pixel_values": {"resource_id": 11},
        "get_frame_overview": {},
        "get_draw_call_state": {"event_id": 2},
        "analyze_render_passes": {},
        "analyze_state_changes": {},
        "get_pass_timing": {},
    }
    for name, params in calls.items():
        result = _load_analysis_script(name).main(capture_file=str(capture), **params)
        assert result["success"] is False, name
        assert result["error"] == "unsupported_backend", name
        assert "qrenderdoc" in result["prompt"], name


def test_texel_decoder_covers_the_numeric_component_types():
    """Both pixel ops decode through this helper, so cover it directly."""
    bridge = runpy.run_path(str(BRIDGE), run_name="dcc_mcp_renderdoc_bridge")
    unorm = bridge["_format_facts"](None, _format(count=2, width=2, comp_type="CompType.UNorm"))
    assert unorm["decodable"] is True
    assert bridge["_decode_texel"](struct.pack("<HH", 0, 65535), 0, unorm) == [0.0, 1.0]
    snorm = bridge["_format_facts"](None, _format(count=1, width=1, comp_type="CompType.SNorm"))
    assert bridge["_decode_texel"](struct.pack("<b", -128), 0, snorm) == [-1.0]
    half = bridge["_format_facts"](None, _format(count=1, width=2))
    assert bridge["_decode_texel"](struct.pack("<e", 1.5), 0, half) == [1.5]
    uint = bridge["_format_facts"](None, _format(count=1, width=4, comp_type="CompType.UInt"))
    assert bridge["_decode_texel"](struct.pack("<I", 7), 0, uint) == [7.0]
    # A format that reports nothing at all is undecodable with a reason, not
    # silently sampled as zeros.
    unknown = bridge["_format_facts"](None, SimpleNamespace(Name=lambda: "unknown"))
    assert unknown["decodable"] is False
    assert unknown["reason"]
    assert bridge["_decode_texel"](b"\x00\x00\x00\x00", 0, unknown) is None
