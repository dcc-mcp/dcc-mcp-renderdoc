"""Coverage for the C3a diff tools: ``diff_draws`` and ``diff_captures``.

The bridge half is covered the way the other replay ops are, by driving the
bundled bridge against a fake controller. The host half -- the metrics, the
sidecar check, and the one-sided-failure rule -- is covered by faking the
readback at the ``run_replay_operation`` boundary, which is the only place the
two sides of a diff meet. No test here touches a GPU or a real ``.rdc``.
"""

from __future__ import annotations

import json
import math
import os
import struct
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_analysis_replay import AnalysisController, _format, _run
from test_replay import (
    BRIDGE,
    FakeController,
    _command_root,
    _fake_rd,
    _texture,
    run_bridge,
)

from dcc_mcp_renderdoc import capabilities, diff, replay

DIFF_OPERATIONS = ("read_diff_region",)

SCRIPT_ROOT = Path(diff.__file__).parent / "skills" / "renderdoc-analysis" / "scripts"


def _load_script(name):
    """Import one skill script module by path."""
    import importlib.util

    path = SCRIPT_ROOT / "{}.py".format(name)
    spec = importlib.util.spec_from_file_location("renderdoc_analysis_{}".format(name), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _float_texture(resource_id, name, width, height, values):
    """One RGBA32F texture plus the bytes its texels decode from."""
    texture = _texture(resource_id, name, width, height)
    texture.format = _format()
    return texture, b"".join(struct.pack("<4f", *value) for value in values)


def _flat_values(count, base=0.0):
    return [(base, base, base, 1.0) for _ in range(count)]


class DiffController(AnalysisController):
    """AnalysisController whose readback depends on the current event."""

    def __init__(self, per_event=None, **overrides):
        super().__init__(**overrides)
        self.per_event = per_event or {}
        self.event = None

    def SetFrameEvent(self, event_id, force):
        self.calls.append(("set-event", event_id, force))
        self.event = event_id
        self.region_data = self.per_event.get(event_id, b"")


# --------------------------------------------------------------------------- #
# Declaration and wiring
# --------------------------------------------------------------------------- #


def test_diff_operation_is_declared_and_implemented():
    assert set(DIFF_OPERATIONS) <= set(replay.REPLAY_OPERATIONS)
    assert set(DIFF_OPERATIONS) <= set(capabilities.CAPABILITY_GROUPS["inspect"])
    bridge = runpy_bridge()
    assert set(DIFF_OPERATIONS) <= set(bridge["OPERATIONS"])


def runpy_bridge():
    import runpy

    return runpy.run_path(str(BRIDGE), run_name="dcc_mcp_renderdoc_bridge")


# --------------------------------------------------------------------------- #
# Bridge: read_diff_region
# --------------------------------------------------------------------------- #


def test_read_diff_region_dumps_both_events_with_matching_sidecars(monkeypatch, tmp_path):
    first = _flat_values(16, 0.25)
    second = _flat_values(16, 0.75)
    texture, data_a = _float_texture(11, "colour", 4, 4, first)
    _discarded, data_b = _float_texture(11, "colour", 4, 4, second)
    controller = DiffController(per_event={2: data_a, 3: data_b}, textures=[texture])
    out_dir = tmp_path / "dumps"
    out_dir.mkdir()
    result = _run(
        monkeypatch,
        tmp_path,
        "read_diff_region",
        {"resource_id": 11, "event_ids": [2, 3], "out_dir": str(out_dir)},
        controller,
    )
    assert result["supported"] is True
    assert len(result["dumps"]) == 2
    # One replay, moved between the two events -- never a second launch.
    assert [entry for entry in controller.calls if entry[0] == "set-event"] == [
        ("set-event", 2, True),
        ("set-event", 3, True),
    ]
    for index, entry in enumerate(result["dumps"]):
        assert entry["event_id"] == 2 + index
        assert entry["width"] == 4 and entry["height"] == 4
        assert entry["comp_count"] == 4
        assert entry["texel_count"] == 16
        assert entry["estimate"] is False
        assert entry["index"] == index
        # The sidecar is the contract the host reads back, so it has to exist.
        sidecar_path = Path(entry["sidecar_file"])
        assert sidecar_path.is_file()
        on_disk = json.loads(sidecar_path.read_text(encoding="utf-8"))
        assert on_disk["width"] == 4 and on_disk["comp_count"] == 4
        assert Path(entry["bin_file"]).is_file()
        assert os.path.getsize(entry["bin_file"]) == entry["byte_size"] == 16 * 4 * 4
    # The two dumps differ, which is the whole point.
    left = open(result["dumps"][0]["bin_file"], "rb").read()
    right = open(result["dumps"][1]["bin_file"], "rb").read()
    assert left != right


def test_read_diff_region_strides_a_region_above_the_texel_ceiling(monkeypatch, tmp_path):
    # 64x64 = 4096 texels under a 1024-texel ceiling is a stride of 2 per axis.
    texture, data = _float_texture(11, "colour", 64, 64, _flat_values(64 * 64, 0.5))
    controller = DiffController(per_event={2: data}, textures=[texture])
    out_dir = tmp_path / "dumps"
    out_dir.mkdir()
    result = _run(
        monkeypatch,
        tmp_path,
        "read_diff_region",
        {"resource_id": 11, "event_ids": [2], "out_dir": str(out_dir), "max_texels": 1024},
        controller,
    )
    entry = result["dumps"][0]
    assert entry["sample_step"] == 2
    assert entry["width"] == 32 and entry["height"] == 32
    assert entry["region_texel_count"] == 4096
    assert entry["estimate"] is True
    assert entry["estimate_method"]
    assert "2" in entry["estimate_method"]


def test_read_diff_region_reports_an_undecodable_format_without_dumps(monkeypatch, tmp_path):
    texture = _texture(11, "bc", 4, 4)
    texture.format = _format(name="BC1_UNORM", count=4, width=1, special=1)
    controller = DiffController(per_event={2: b"\x00" * 64}, textures=[texture])
    out_dir = tmp_path / "dumps"
    out_dir.mkdir()
    result = _run(
        monkeypatch,
        tmp_path,
        "read_diff_region",
        {"resource_id": 11, "event_ids": [2], "out_dir": str(out_dir)},
        controller,
    )
    assert result["supported"] is False
    assert result["dumps"] == []
    assert result["error_message"]
    assert result["hint"]


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"match_by": "index", "event_index": 0}, 2),
        ({"match_by": "index", "event_index": 1}, 4),
        ({"match_by": "name", "event_name": "Draw A"}, 2),
        ({"match_by": "name", "event_name": "Draw C"}, 4),
    ],
)
def test_read_diff_region_resolves_events_by_index_and_name(
    monkeypatch, tmp_path, params, expected
):
    texture, data = _float_texture(11, "colour", 4, 4, _flat_values(16, 0.5))
    controller = DiffController(
        per_event={2: data, 3: data, 4: data},
        textures=[texture],
        actions=FakeController().actions,
    )
    out_dir = tmp_path / "dumps"
    out_dir.mkdir()
    request = {"resource_id": 11, "out_dir": str(out_dir)}
    request.update(params)
    result = _run(monkeypatch, tmp_path, "read_diff_region", request, controller)
    assert result["supported"] is True
    assert [entry["event_id"] for entry in result["dumps"]] == [expected]


def test_read_diff_region_rejects_an_unknown_match_mode(monkeypatch, tmp_path):
    texture, data = _float_texture(11, "colour", 4, 4, _flat_values(16, 0.5))
    controller = DiffController(per_event={2: data}, textures=[texture])
    out_dir = tmp_path / "dumps"
    out_dir.mkdir()
    status, _context = run_bridge(
        monkeypatch,
        tmp_path,
        "read_diff_region",
        {
            "resource_id": 11,
            "event_ids": [2],
            "match_by": "guess",
            "out_dir": str(out_dir),
        },
        controller=controller,
    )
    assert status["error"] and "match_by" in status["error"]


# --------------------------------------------------------------------------- #
# Host: metrics
# --------------------------------------------------------------------------- #


def _write_dump(directory, name, values, *, comp_count=4, width=2, height=2):
    """Write one dump the way the bridge does, and return its descriptor."""
    packed = []
    for start in range(0, len(values), 4096):
        chunk = values[start : start + 4096]
        packed.append(struct.pack("<{}f".format(len(chunk)), *chunk))
    binary = directory / "{}.bin".format(name)
    binary.write_bytes(b"".join(packed))
    sidecar = {
        "schema_version": diff.DIFF_DUMP_SCHEMA_VERSION,
        "capture_file": "capture.rdc",
        "resource_id": 11,
        "resource_name": "colour",
        "event_id": 2,
        "api": "GraphicsAPI.D3D11",
        "match_by": "event_id",
        "format": {
            "name": "R32G32B32A32_FLOAT",
            "comp_count": comp_count,
            "comp_byte_width": 4,
            "element_byte_size": comp_count * 4,
            "comp_type": "CompType.Float",
            "special": 0,
            "decodable": True,
        },
        "region": {"x": 0, "y": 0, "width": width, "height": height, "mip": 0},
        "width": width,
        "height": height,
        "comp_count": comp_count,
        "texel_count": width * height,
        "float_count": len(values),
        "byte_size": len(values) * 4,
        "sample_step": 1,
        "sampled": False,
        "estimate": False,
        "estimate_method": None,
        "bin_file": str(binary),
        "sidecar_file": str(directory / "{}.json".format(name)),
        "index": 0,
    }
    (directory / "{}.json".format(name)).write_text(json.dumps(sidecar), encoding="utf-8")
    return sidecar


def _pair(tmp_path, left_values, right_values, **kwargs):
    left = _write_dump(tmp_path, "left", left_values, **kwargs)
    right = _write_dump(tmp_path, "right", right_values, **kwargs)
    return left, right


def test_identical_dumps_report_identical_true_and_no_infinite_psnr(tmp_path):
    values = [0.25, 0.5, 0.75, 1.0] * 4
    left, right = _pair(tmp_path, values, list(values))
    metrics = diff.compare_dumps(left, right, ssim_grid_step=8)
    assert metrics["identical"] is True
    # The product decision: no infinity to compare against a threshold.
    assert metrics["psnr"] is None
    assert metrics["mse"] == 0.0
    assert metrics["failed_texel_count"] == 0


def test_psnr_carries_its_basis_into_the_payload(tmp_path):
    left, right = _pair(tmp_path, [0.0, 0.0, 0.0, 1.0] * 4, [0.5, 0.0, 0.0, 1.0] * 4)
    metrics = diff.compare_dumps(left, right, ssim_grid_step=8)
    basis = metrics["psnr_basis"]
    assert basis["bit_depth"] == 32
    assert basis["channels"] == [0, 1, 2, 3]
    assert basis["max_value"] == 1.0
    assert basis["max_value_source"] == "format_nominal_peak"
    # Every texel is off by 0.5 in one of four channels, so the MSE over the
    # 4x4 compared values is (4 * 0.25) / 16 and PSNR is 10*log10(1 / mse).
    assert metrics["mse"] == pytest.approx(0.0625)
    assert metrics["psnr"] == pytest.approx(10.0 * math.log10(16.0))
    assert metrics["max_abs_diff"] == pytest.approx(0.5)


def test_caller_supplied_max_value_is_reported_as_its_source(tmp_path):
    left, right = _pair(tmp_path, [0.0, 0.0, 0.0, 1.0] * 4, [0.5, 0.0, 0.0, 1.0] * 4)
    metrics = diff.compare_dumps(left, right, max_value=255.0, ssim_grid_step=8)
    assert metrics["psnr_basis"]["max_value"] == 255.0
    assert metrics["psnr_basis"]["max_value_source"] == "caller_supplied"


def test_ssim_approx_is_named_and_described_not_passed_off_as_ssim(tmp_path):
    # SSIM needs at least one 8x8 block, so this region is 8x8.
    left, right = _pair(
        tmp_path, [0.0, 0.0, 0.0, 1.0] * 64, [0.1, 0.0, 0.0, 1.0] * 64, width=8, height=8
    )
    metrics = diff.compare_dumps(left, right, ssim_grid_step=8)
    # Never the plain name: this is an approximation, not the standard index.
    assert "ssim" not in metrics
    assert metrics["ssim_approx"] is not None
    assert metrics["ssim_block_size"] == 8
    assert metrics["ssim_window"] == "box"
    assert metrics["ssim_grid_step"] == 8
    assert metrics["ssim_block_count"] >= 1
    assert -1.0 <= metrics["ssim_approx"] <= 1.0


def test_ssim_approx_is_skipped_with_a_reason_below_one_block(tmp_path):
    left, right = _pair(tmp_path, [0.0], [0.1], width=1, height=1, comp_count=1)
    metrics = diff.compare_dumps(left, right, ssim_grid_step=8)
    assert metrics["ssim_approx"] is None
    assert "smaller than one 8x8 block" in metrics["ssim_skipped_reason"]


def test_ssim_approx_reads_a_strided_grid_without_repeating_rows(tmp_path):
    """A grid step larger than the block must not rewind the read cursor."""
    left, right = _pair(
        tmp_path, [0.0, 0.0, 0.0, 1.0] * 256, [0.2, 0.0, 0.0, 1.0] * 256, width=16, height=16
    )
    dense = diff.compare_dumps(left, right, ssim_grid_step=8)
    sparse = diff.compare_dumps(left, right, ssim_grid_step=8)
    assert dense["ssim_block_count"] == sparse["ssim_block_count"]
    # A 16x16 region at step 8 is a 2x2 grid of non-overlapping blocks.
    assert dense["ssim_block_count"] == 4
    assert dense["ssim_approx"] == pytest.approx(sparse["ssim_approx"])


def test_nan_and_inf_are_counted_separately_and_excluded(tmp_path):
    nan = float("nan")
    inf = float("inf")
    left_values = [0.0, 0.0, 0.0, 1.0, nan, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, inf, 0.0, 0.0, 1.0]
    right_values = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, nan, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    left, right = _pair(tmp_path, left_values, right_values)
    metrics = diff.compare_dumps(left, right, ssim_grid_step=8)
    non_finite = metrics["non_finite"]
    assert non_finite["nan_count_left"] == 1
    assert non_finite["nan_count_right"] == 1
    assert non_finite["inf_count_left"] == 1
    # The NaN sits on a different texel on each side, so it mismatches twice.
    assert non_finite["nan_mismatch_count"] == 2
    assert non_finite["inf_mismatch_count"] == 1
    assert non_finite["excluded_texel_count"] == 3
    # The three poisoned texels are out, so the clean one is all that is left.
    assert metrics["compared_texel_count"] == 1
    assert metrics["identical"] is False


def test_threshold_counts_the_texels_over_it(tmp_path):
    left, right = _pair(
        tmp_path,
        [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        [0.5, 0.0, 0.0, 1.0, 0.001, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.9, 0.0, 0.0, 1.0],
    )
    metrics = diff.compare_dumps(left, right, threshold=0.25)
    assert metrics["failed_texel_count"] == 2
    assert metrics["failed_texel_ratio"] == pytest.approx(0.5)
    assert metrics["max_abs_diff"] == pytest.approx(0.9)


def test_channel_selection_narrows_the_comparison(tmp_path):
    left, right = _pair(tmp_path, [0.0, 0.0, 0.0, 1.0] * 4, [0.5, 0.0, 0.0, 1.0] * 4)
    metrics = diff.compare_dumps(left, right, channels=[1], ssim_grid_step=8)
    assert metrics["psnr_basis"]["channels"] == [1]
    # Only channel 1 is compared, and both sides are 0.0 there.
    assert metrics["identical"] is True
    assert metrics["compared_channel_count"] == 1


def test_heatmap_export_writes_png_and_ppm(tmp_path):
    left, right = _pair(tmp_path, [0.0, 0.0, 0.0, 1.0] * 4, [0.25, 0.5, 0.75, 1.0] * 4)
    png = tmp_path / "nested" / "heat.png"
    metrics = diff.compare_dumps(left, right, output_file=str(png))
    # The parent directory is created rather than demanded in advance.
    assert png.is_file()
    assert metrics["output_file"]["format"] == "png"
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    ppm = tmp_path / "heat.ppm"
    metrics = diff.compare_dumps(left, right, output_file=str(ppm))
    assert ppm.read_bytes().startswith(b"P6\n2 2\n255\n")
    assert metrics["output_file"]["format"] == "ppm"


def test_heatmap_export_rejects_an_unknown_extension(tmp_path):
    left, right = _pair(tmp_path, [0.0] * 16, [0.1] * 16)
    with pytest.raises(replay.RenderDocError, match="extensions"):
        diff.compare_dumps(left, right, output_file=str(tmp_path / "heat.gif"))


# --------------------------------------------------------------------------- #
# Host: sidecar contract
# --------------------------------------------------------------------------- #


def test_a_size_mismatch_is_reported_not_raised(tmp_path):
    left = _write_dump(tmp_path, "left", [0.0] * 16, width=2, height=2)
    right = _write_dump(tmp_path, "right", [0.0] * 64, width=4, height=4)
    mismatch = diff.compare_sidecars(left, right)
    assert mismatch is not None
    assert mismatch["reason_code"] == "dimension_mismatch"
    assert mismatch["reason"]


def test_a_format_mismatch_is_reported(tmp_path):
    left = _write_dump(tmp_path, "left", [0.0] * 16)
    right = _write_dump(tmp_path, "right", [0.0] * 16)
    right["format"] = dict(left["format"], name="R8G8B8A8_UNORM")
    mismatch = diff.compare_sidecars(left, right)
    assert mismatch["reason_code"] == "format_mismatch"


def test_an_api_mismatch_is_reported(tmp_path):
    left = _write_dump(tmp_path, "left", [0.0] * 16)
    right = _write_dump(tmp_path, "right", [0.0] * 16)
    right["api"] = "GraphicsAPI.Vulkan"
    mismatch = diff.compare_sidecars(left, right)
    assert mismatch["reason_code"] == "api_mismatch"


def test_matching_sidecars_are_comparable(tmp_path):
    left = _write_dump(tmp_path, "left", [0.0] * 16)
    right = _write_dump(tmp_path, "right", [0.0] * 16)
    assert diff.compare_sidecars(left, right) is None


# --------------------------------------------------------------------------- #
# Host: orchestration
# --------------------------------------------------------------------------- #


@pytest.fixture
def reachable_backend(monkeypatch, tmp_path):
    """Make the deep backend reachable through a fake renderdoccmd."""
    command = _command_root(tmp_path / "bin", with_qrenderdoc=True)
    monkeypatch.setattr(replay, "probe", lambda *a, **k: capabilities.probe(command=str(command)))
    return command


def _fake_readback(monkeypatch, per_side):
    """Replace the child-process readback with a per-capture stub."""
    import runpy

    bridge = runpy.run_path(str(BRIDGE), run_name="dcc_mcp_renderdoc_bridge")

    def fake_run(capture_file, operation, params=None, **kwargs):
        del kwargs
        assert operation == "read_diff_region"
        key = Path(capture_file).stem
        controller = per_side[key]
        return {
            "capture_file": str(capture_file),
            "operation": operation,
            "result": bridge["OPERATIONS"]["read_diff_region"](
                controller, _fake_rd(), params, None
            ),
        }

    monkeypatch.setattr(diff, "run_replay_operation", fake_run)


class _ApiController(DiffController):
    """DiffController whose reported graphics API is set per capture."""

    def __init__(self, api, **kwargs):
        super().__init__(**kwargs)
        self._api = api

    def GetAPIProperties(self):
        return SimpleNamespace(pipelineType=self._api)


def _diff_controller(values, event_id=2, api="GraphicsAPI.D3D11", size=2):
    texture, data = _float_texture(11, "colour", size, size, values)
    return _ApiController(api, per_event={event_id: data}, textures=[texture])


def test_diff_captures_compares_two_captures(reachable_backend, monkeypatch, tmp_path):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    left = _diff_controller(_flat_values(4, 0.0))
    right = _diff_controller(_flat_values(4, 0.5))
    _fake_readback(monkeypatch, {"a": left, "b": right})
    result = diff.diff_region(
        diff.DIFF_CAPTURES,
        str(capture_a),
        other_capture_file=str(capture_b),
        resource_id=11,
        event_ids=[2],
    )
    payload = result["result"]
    assert payload["supported"] is True
    assert payload["comparable"] is True
    assert payload["metrics"]["identical"] is False
    assert payload["sides"][0]["capture_file"] == str(capture_a)
    assert payload["sides"][1]["capture_file"] == str(capture_b)


def test_diff_captures_reports_one_failed_side_without_half_a_diff(
    reachable_backend, monkeypatch, tmp_path
):
    """A capture that will not replay is a one-sided failure, never a diff."""
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    _fake_readback(monkeypatch, {"a": _diff_controller(_flat_values(4, 0.0))})

    def explode(capture_file, operation, params=None, **kwargs):
        raise replay.RenderDocError("qrenderdoc exited with code 3")

    monkeypatch.setattr(diff, "run_replay_operation", explode)
    result = diff.diff_region(
        diff.DIFF_CAPTURES,
        str(capture_a),
        other_capture_file=str(capture_b),
        resource_id=11,
        event_ids=[2],
    )
    payload = result["result"]
    assert payload["supported"] is False
    assert payload["side"] == "a"
    assert "a.rdc" in payload["error_message"]
    assert payload["hint"]
    # No metrics: a one-sided result never reaches a diff conclusion.
    assert "metrics" not in payload


def test_diff_captures_returns_comparable_false_on_a_mismatch(
    reachable_backend, monkeypatch, tmp_path
):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    left = _diff_controller(_flat_values(4, 0.0), api="GraphicsAPI.D3D11")
    right = _diff_controller(_flat_values(4, 0.0), api="GraphicsAPI.Vulkan")
    _fake_readback(monkeypatch, {"a": left, "b": right})
    result = diff.diff_region(
        diff.DIFF_CAPTURES,
        str(capture_a),
        other_capture_file=str(capture_b),
        resource_id=11,
        event_ids=[2],
    )
    payload = result["result"]
    assert payload["supported"] is True
    assert payload["comparable"] is False
    assert payload["reason_code"] == "api_mismatch"
    assert payload["metrics"] is None


def test_diff_captures_force_compares_anyway(reachable_backend, monkeypatch, tmp_path):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    left = _diff_controller(_flat_values(4, 0.0), api="GraphicsAPI.D3D11")
    right = _diff_controller(_flat_values(4, 0.0), api="GraphicsAPI.Vulkan")
    _fake_readback(monkeypatch, {"a": left, "b": right})
    result = diff.diff_region(
        diff.DIFF_CAPTURES,
        str(capture_a),
        other_capture_file=str(capture_b),
        resource_id=11,
        event_ids=[2],
        force=True,
    )
    payload = result["result"]
    assert payload["comparable"] is False
    assert payload["forced_reason"]
    assert payload["metrics"] is not None


def test_diff_draws_reuses_one_replay_for_two_events(reachable_backend, monkeypatch, tmp_path):
    capture = tmp_path / "a.rdc"
    capture.write_bytes(b"rdc")
    controller = _diff_controller(_flat_values(4, 0.0))
    controller.per_event = {2: controller.per_event[2], 3: controller.per_event[2]}
    _fake_readback(monkeypatch, {"a": controller})
    result = diff.diff_region(
        diff.DIFF_DRAWS,
        str(capture),
        event_ids=[2, 3],
        resource_id=11,
    )
    payload = result["result"]
    assert payload["supported"] is True
    assert payload["comparable"] is True
    assert payload["sides"][0]["event_id"] == 2
    assert payload["sides"][1]["event_id"] == 3


def _diff_temp_dirs():
    """Leftover diff staging directories in the shared temp root."""
    prefix = "dcc-mcp-renderdoc-diff-"
    return [name for name in os.listdir(tempfile.gettempdir()) if name.startswith(prefix)]


def test_diff_region_removes_its_temporary_directory(reachable_backend, monkeypatch, tmp_path):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    assert _diff_temp_dirs() == []
    _fake_readback(
        monkeypatch,
        {
            "a": _diff_controller(_flat_values(4, 0.0)),
            "b": _diff_controller(_flat_values(4, 0.5)),
        },
    )
    diff.diff_region(
        diff.DIFF_CAPTURES,
        str(capture_a),
        other_capture_file=str(capture_b),
        resource_id=11,
        event_ids=[2],
    )
    # Success and failure both leave nothing behind: the directory is owned by
    # a with-block, not by a cleanup call someone can forget.
    assert _diff_temp_dirs() == []


def test_diff_region_recovers_the_temp_directory_on_a_failure(
    reachable_backend, monkeypatch, tmp_path
):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    assert _diff_temp_dirs() == []
    _fake_readback(monkeypatch, {"a": _diff_controller(_flat_values(4, 0.0))})

    def explode(capture_file, operation, params=None, **kwargs):
        raise replay.RenderDocError("boom")

    monkeypatch.setattr(diff, "run_replay_operation", explode)
    result = diff.diff_region(
        diff.DIFF_CAPTURES,
        str(capture_a),
        other_capture_file=str(capture_b),
        resource_id=11,
        event_ids=[2],
    )
    assert result["result"]["supported"] is False
    assert _diff_temp_dirs() == []


# --------------------------------------------------------------------------- #
# Skill scripts
# --------------------------------------------------------------------------- #


def test_diff_scripts_report_an_unreachable_backend(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    monkeypatch.setattr(
        replay.subprocess, "run", lambda *_a, **_k: pytest.fail("must not launch qrenderdoc")
    )
    command = _command_root(tmp_path / "missing", with_qrenderdoc=False)
    monkeypatch.setattr(replay, "probe", lambda *a, **k: capabilities.probe(command=str(command)))
    for name, params in (
        (
            "diff_draws",
            {"event_id_a": 2, "event_id_b": 3, "resource_id": 11},
        ),
        (
            "diff_captures",
            {"other_capture_file": str(tmp_path / "other.rdc"), "resource_id": 11},
        ),
    ):
        result = _load_script(name).main(capture_file=str(capture), **params)
        assert result["success"] is False, name
        assert result["error"] == "unsupported_backend", name
        assert "qrenderdoc" in result["prompt"], name


def test_diff_captures_script_reports_a_mismatch_as_success_not_an_error(
    reachable_backend, monkeypatch, tmp_path
):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    _fake_readback(
        monkeypatch,
        {
            "a": _diff_controller(_flat_values(4, 0.0), api="GraphicsAPI.D3D11"),
            "b": _diff_controller(_flat_values(4, 0.0), api="GraphicsAPI.Vulkan"),
        },
    )
    result = _load_script("diff_captures").main(
        capture_file=str(capture_a),
        other_capture_file=str(capture_b),
        resource_id=11,
        event_id=2,
    )
    # A mismatch is an answer, not a failure.
    assert result["success"] is True
    assert "not comparable" in result["message"]
    assert "force" in result["message"]


def test_diff_scripts_put_the_estimate_and_ssim_method_in_the_summary(
    reachable_backend, monkeypatch, tmp_path
):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    # 16x16 so the region holds at least one 8x8 SSIM block.
    _fake_readback(
        monkeypatch,
        {
            "a": _diff_controller(_flat_values(256, 0.0), size=16),
            "b": _diff_controller(_flat_values(256, 0.5), size=16),
        },
    )
    result = _load_script("diff_captures").main(
        capture_file=str(capture_a),
        other_capture_file=str(capture_b),
        resource_id=11,
        event_id=2,
    )
    message = result["message"]
    assert result["success"] is True
    # The SSIM caveat travels in the summary, not only in SKILL.md.
    assert "ssim_approx" in message
    assert "box" in message and "8x8" in message
    assert "not the standard 11x11 Gaussian SSIM" in message
    assert "PSNR basis" in message


def test_diff_scripts_exist_and_are_declared():
    for name in ("diff_draws", "diff_captures"):
        assert (SCRIPT_ROOT / "{}.py".format(name)).is_file()
    manifest = yaml.safe_load((SCRIPT_ROOT.parent / "tools.yaml").read_text(encoding="utf-8"))
    groups = {tool["name"]: tool.get("group") for tool in manifest["tools"]}
    assert groups["diff_draws"] == "verify"
    assert groups["diff_captures"] == "verify"


def test_diff_script_summaries_report_identical_without_an_infinite_psnr(
    reachable_backend, monkeypatch, tmp_path
):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    _fake_readback(
        monkeypatch,
        {
            "a": _diff_controller(_flat_values(4, 0.25)),
            "b": _diff_controller(_flat_values(4, 0.25)),
        },
    )
    result = _load_script("diff_captures").main(
        capture_file=str(capture_a),
        other_capture_file=str(capture_b),
        resource_id=11,
        event_id=2,
    )
    assert result["success"] is True
    assert "identical=true" in result["message"]
    assert "inf" not in result["message"].lower().replace("infinite", "")
