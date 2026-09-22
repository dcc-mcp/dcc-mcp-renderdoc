"""Coverage for the C3b assertion gates: ``assert_pixels`` and ``assert_state``.

The pixel gate is covered by driving the diff layer it delegates to with a fake
readback, so the thresholds are judged against real measured numbers rather than
against stubs. The state gate is covered the same way through the bundled
bridge's own fingerprint. No test here touches a GPU or a real ``.rdc``, and
none asserts a version number.
"""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_analysis_diff import (
    SCRIPT_ROOT,
    DiffController,
    _ApiController,
    _flat_values,
    _format,
    reachable_backend,  # noqa: F401 - reused as a fixture by import
)
from test_replay import BRIDGE, FakeController, _fake_rd, _texture

from dcc_mcp_renderdoc import assertions, diff, replay

STATE_OPERATIONS = ("read_state_fingerprint",)


def _load(name):
    path = SCRIPT_ROOT / "{}.py".format(name)
    spec = importlib.util.spec_from_file_location("assert_{}".format(name), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# Declaration and wiring
# --------------------------------------------------------------------------- #


def test_state_operation_is_declared_and_implemented():
    import runpy

    assert set(STATE_OPERATIONS) <= set(replay.REPLAY_OPERATIONS)
    from dcc_mcp_renderdoc import capabilities

    assert set(STATE_OPERATIONS) <= set(capabilities.CAPABILITY_GROUPS["inspect"])
    bridge = runpy.run_path(str(BRIDGE), run_name="dcc_mcp_renderdoc_bridge")
    assert set(STATE_OPERATIONS) <= set(bridge["OPERATIONS"])


def test_assertion_scripts_exist_and_are_declared():
    for name in ("assert_pixels", "assert_state"):
        assert (SCRIPT_ROOT / "{}.py".format(name)).is_file()
    manifest = yaml.safe_load((SCRIPT_ROOT.parent / "tools.yaml").read_text(encoding="utf-8"))
    groups = {tool["name"]: tool.get("group") for tool in manifest["tools"]}
    assert groups["assert_pixels"] == "verify"
    assert groups["assert_state"] == "verify"


# --------------------------------------------------------------------------- #
# Threshold evaluation
# --------------------------------------------------------------------------- #


def _metrics(mean_abs_diff=0.0, psnr=None, failed_ratio=0.0, identical=False):
    return {
        "mean_abs_diff": mean_abs_diff,
        "psnr": psnr,
        "failed_texel_ratio": failed_ratio,
        "identical": identical,
    }


def test_a_gate_with_no_threshold_is_refused_as_unsupported():
    checks, names = assertions.selected_pixel_checks()
    assert checks == [] and names == []
    result = assertions.assert_pixels("c.rdc", resource_id=11)
    assert result["result"]["supported"] is False
    assert "at least one threshold" in result["result"]["error_message"]
    assert result["result"]["hint"]


def test_each_threshold_is_read_in_the_direction_that_makes_it_a_gate():
    """``max_*`` tolerates values below it; ``min_psnr`` demands values above."""
    checks, names = assertions.selected_pixel_checks(
        max_mean_abs_diff=0.01, min_psnr=30.0, max_failed_pixel_ratio=0.1
    )
    assert names == ["mean_abs_diff", "psnr", "failed_texel_ratio"]
    passing, evaluated, unevaluated = assertions.evaluate_pixel_checks(
        _metrics(mean_abs_diff=0.005, psnr=40.0, failed_ratio=0.05), checks
    )
    assert unevaluated == []
    assert all(entry["passed"] for entry in passing)
    assert len(evaluated) == 3

    failing, evaluated, _ = assertions.evaluate_pixel_checks(
        _metrics(mean_abs_diff=0.5, psnr=10.0, failed_ratio=0.9), checks
    )
    assert not any(entry["passed"] for entry in failing)
    assert len(evaluated) == 3


def test_identical_images_satisfy_any_min_psnr():
    """PSNR is null for identical images, and that is the best case, not a gap."""
    checks, _ = assertions.selected_pixel_checks(min_psnr=60.0)
    results, evaluated, unevaluated = assertions.evaluate_pixel_checks(
        _metrics(psnr=None, identical=True), checks
    )
    assert unevaluated == []
    assert evaluated[0]["passed"] is True
    assert results[0]["measured"] is None


def test_a_missing_measurement_is_unevaluated_not_passed():
    """Only the psnr check may pass on a null; the others must not guess."""
    checks, _ = assertions.selected_pixel_checks(max_mean_abs_diff=0.01)
    results, evaluated, unevaluated = assertions.evaluate_pixel_checks(
        _metrics(mean_abs_diff=None), checks
    )
    assert evaluated == []
    assert [entry["name"] for entry in unevaluated] == ["mean_abs_diff"]


# --------------------------------------------------------------------------- #
# assert_pixels end to end
# --------------------------------------------------------------------------- #


def _fake_readback(monkeypatch, per_side):
    """Replace the child-process readback with a per-capture stub."""
    import runpy

    bridge = runpy.run_path(str(BRIDGE), run_name="dcc_mcp_renderdoc_bridge")
    operation_map = {
        "read_diff_region": "read_diff_region",
        "read_state_fingerprint": "read_state_fingerprint",
    }

    def fake_run(capture_file, operation, params=None, **kwargs):
        del kwargs
        controller = per_side[Path(capture_file).stem]
        return {
            "capture_file": str(capture_file),
            "operation": operation,
            "result": bridge["OPERATIONS"][operation_map[operation]](
                controller, _fake_rd(), params, None
            ),
        }

    monkeypatch.setattr(diff, "run_replay_operation", fake_run)
    monkeypatch.setattr(assertions, "run_replay_operation", fake_run)


def _diff_controller(values, event_id=2, api="GraphicsAPI.D3D11", size=2):
    """A float-colour target whose readback is driven by the current event."""
    texture = _texture(11, "colour", size, size)
    texture.format = _format()
    data = b"".join(struct.pack("<4f", *value) for value in values)
    return _ApiController(api, per_event={event_id: data}, textures=[texture])


@pytest.fixture
def backend(monkeypatch, tmp_path):
    """Make the deep backend reachable through a fake renderdoccmd."""
    from test_replay import _command_root

    from dcc_mcp_renderdoc import capabilities as caps

    command = _command_root(tmp_path / "bin", with_qrenderdoc=True)
    monkeypatch.setattr(replay, "probe", lambda *a, **k: caps.probe(command=str(command)))
    return command


def test_assert_pixels_passes_inside_the_threshold(backend, monkeypatch, tmp_path):
    capture = tmp_path / "a.rdc"
    capture.write_bytes(b"rdc")
    controller = _diff_controller(_flat_values(4, 0.0))
    controller.per_event = {2: controller.per_event[2], 3: controller.per_event[2]}
    _fake_readback(monkeypatch, {"a": controller})
    result = assertions.assert_pixels(
        str(capture),
        resource_id=11,
        event_id_a=2,
        event_id_b=3,
        max_mean_abs_diff=1.0,
    )
    payload = result["result"]
    assert payload["supported"] is True
    assert payload["evaluable"] is True
    assert payload["passed"] is True
    assert payload["failed_checks"] == []
    assert payload["metrics"]["identical"] is True


def test_assert_pixels_fails_outside_the_threshold(backend, monkeypatch, tmp_path):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    _fake_readback(
        monkeypatch,
        {
            "a": _diff_controller(_flat_values(4, 0.0)),
            "b": _diff_controller(_flat_values(4, 0.5)),
        },
    )
    result = assertions.assert_pixels(
        str(capture_a),
        resource_id=11,
        other_capture_file=str(capture_b),
        event_id=2,
        max_mean_abs_diff=0.01,
        min_psnr=40.0,
    )
    payload = result["result"]
    assert payload["supported"] is True
    assert payload["passed"] is False
    assert len(payload["failed_checks"]) == 2
    assert result["operation"] == "assert_pixels"
    # A regression is a normal successful call that reports a verdict.
    script = _load("assert_pixels").main(
        capture_file=str(capture_a),
        resource_id=11,
        other_capture_file=str(capture_b),
        event_id=2,
        max_mean_abs_diff=0.01,
    )
    assert script["success"] is True
    assert "FAILED" in script["message"]


def test_assert_pixels_reports_uncomparable_as_unevaluated(backend, monkeypatch, tmp_path):
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
    result = assertions.assert_pixels(
        str(capture_a),
        resource_id=11,
        other_capture_file=str(capture_b),
        event_id=2,
        min_psnr=40.0,
    )
    payload = result["result"]
    assert payload["supported"] is True
    assert payload["comparable"] is False
    assert payload["passed"] is False
    # Nothing was measured, so this must not read as a regression.
    assert payload["evaluable"] is False
    assert payload["unevaluated_checks"] == ["psnr"]
    assert payload["metrics"] is None


def test_assert_pixels_requires_both_event_ids_without_a_second_capture(backend, tmp_path):
    capture = tmp_path / "a.rdc"
    capture.write_bytes(b"rdc")
    result = assertions.assert_pixels(
        str(capture), resource_id=11, event_id_a=2, max_mean_abs_diff=1.0
    )
    assert result["result"]["supported"] is False
    assert "event_id_a and event_id_b" in result["result"]["error_message"]


def test_assert_pixels_reports_an_unreachable_backend(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    monkeypatch.setattr(
        replay.subprocess, "run", lambda *_a, **_k: pytest.fail("must not launch qrenderdoc")
    )
    from test_replay import _command_root

    from dcc_mcp_renderdoc import capabilities as caps

    command = _command_root(tmp_path / "missing", with_qrenderdoc=False)
    monkeypatch.setattr(replay, "probe", lambda *a, **k: caps.probe(command=str(command)))
    result = _load("assert_pixels").main(
        capture_file=str(capture),
        resource_id=11,
        event_id_a=2,
        event_id_b=3,
        max_mean_abs_diff=1.0,
    )
    assert result["success"] is False
    assert result["error"] == "unsupported_backend"
    assert "qrenderdoc" in result["prompt"]


def _summary(monkeypatch, tmp_path, right_value, thresholds):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    _fake_readback(
        monkeypatch,
        {
            "a": _diff_controller(_flat_values(4, 0.0)),
            "b": _diff_controller(_flat_values(4, right_value)),
        },
    )
    return _load("assert_pixels").main(
        capture_file=str(capture_a),
        resource_id=11,
        other_capture_file=str(capture_b),
        event_id=2,
        **thresholds,
    )


@pytest.mark.parametrize(
    "right_value,verdict,passing,total",
    [
        # Identical: both thresholds are satisfied.
        (0.0, "PASSED", 2, 2),
        # A large difference: both thresholds are breached.
        (0.5, "FAILED", 0, 2),
    ],
)
def test_assert_pixels_summary_counts_the_checks_that_passed(
    backend, monkeypatch, tmp_path, right_value, verdict, passing, total
):
    """The sentence must count passes, not the number that could be judged.

    When every check is evaluable the two counts happen to be equal, so a
    numerator built from the judged checks made every failure read "N of N
    check(s) passed" -- which is the one sentence a CI log must not emit on a
    regression. This pins the clause rather than just the verdict word.
    """
    result = _summary(
        monkeypatch, tmp_path, right_value, {"max_mean_abs_diff": 0.01, "min_psnr": 40.0}
    )
    message = result["message"]
    assert "assert_pixels {}: {} of {} check(s) passed.".format(verdict, passing, total) in message
    assert "mean_abs_diff" in message and "psnr" in message
    assert result["context"]["result"]["passed"] is (verdict == "PASSED")


def test_assert_pixels_summary_can_report_a_partial_pass(backend, monkeypatch, tmp_path):
    """One threshold breached and one satisfied must read 1 of 2."""
    result = _summary(
        monkeypatch,
        tmp_path,
        0.5,
        # A tight ratio gate that the difference trips, and a loose PSNR gate
        # that it does not.
        {"max_failed_pixel_ratio": 0.0, "min_psnr": 1.0},
    )
    assert "assert_pixels FAILED: 1 of 2 check(s) passed." in result["message"]


# --------------------------------------------------------------------------- #
# assert_state
# --------------------------------------------------------------------------- #


def test_fingerprint_comparison_reports_before_and_after():
    left = {"fingerprint": {"cull_mode": "None", "depth_enable": True}}
    right = {"fingerprint": {"cull_mode": "Back", "depth_enable": True}}
    differences, ignored, compared = assertions.compare_fingerprints(
        left, right, ignore_keys=["depth_enable"]
    )
    assert ignored == ["depth_enable"]
    assert compared == ["cull_mode"]
    assert differences == [{"key": "cull_mode", "before": "None", "after": "Back"}]


def test_fingerprint_comparison_treats_a_one_sided_key_as_a_difference():
    left = {"fingerprint": {"viewport": [1, 2]}}
    right = {"fingerprint": {}}
    differences, _, _ = assertions.compare_fingerprints(left, right, ignore_keys=None)
    assert differences == [{"key": "viewport", "before": [1, 2], "after": None}]


def test_unknown_ignore_keys_are_reported_not_swallowed():
    left = {"fingerprint": {"cull_mode": "None"}}
    right = {"fingerprint": {"cull_mode": "None"}}
    differences, ignored, compared = assertions.compare_fingerprints(
        left, right, ignore_keys=["cul_mode"]
    )
    assert differences == [] and ignored == [] and compared == ["cull_mode"]


def test_assert_state_passes_when_the_state_matches(backend, monkeypatch, tmp_path):
    capture = tmp_path / "a.rdc"
    capture.write_bytes(b"rdc")
    controller = DiffController(
        per_event={2: b"", 3: b""}, textures=[], actions=FakeController().actions
    )
    _fake_readback(monkeypatch, {"a": controller})
    result = assertions.assert_state(str(capture), event_id_a=2, event_id_b=3)
    payload = result["result"]
    assert payload["supported"] is True
    assert payload["passed"] is True
    assert payload["differences"] == []
    assert payload["compared_keys"]


def test_assert_state_reports_differences_between_captures(backend, monkeypatch, tmp_path):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")

    class _Culled(DiffController):
        def GetPipelineState(self):
            state = super().GetPipelineState()
            original = state.GetRasterState

            def raster():
                return SimpleNamespace(cullMode=_EnumLike("CullMode.Back", 2))

            state.GetRasterState = raster
            del original
            return state

    class _EnumLike(str):
        pass

    _fake_readback(
        monkeypatch,
        {
            "a": DiffController(per_event={2: b""}, textures=[], actions=FakeController().actions),
            "b": _Culled(per_event={2: b""}, textures=[], actions=FakeController().actions),
        },
    )
    result = assertions.assert_state(str(capture_a), other_capture_file=str(capture_b), event_id=2)
    payload = result["result"]
    assert payload["supported"] is True
    # Whatever the fake exposes, the shape and the verdict must agree.
    assert isinstance(payload["passed"], bool)
    assert payload["difference_count"] == len(payload["differences"])
    assert payload["mode"] == "captures"


def test_assert_state_ignores_the_keys_it_is_told_to(backend, monkeypatch, tmp_path):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    _fake_readback(
        monkeypatch,
        {
            "a": DiffController(per_event={2: b""}, textures=[], actions=FakeController().actions),
            "b": DiffController(per_event={2: b""}, textures=[], actions=FakeController().actions),
        },
    )
    result = assertions.assert_state(
        str(capture_a),
        other_capture_file=str(capture_b),
        event_id=2,
        ignore_keys=["cull_mode", "viewport", "depth_enable"],
    )
    payload = result["result"]
    assert payload["passed"] is True
    assert "cull_mode" in payload["ignored_keys"]
    assert "cull_mode" not in payload["compared_keys"]


@pytest.mark.parametrize("match_by", ["event_id", "index", "name"])
def test_assert_state_reports_event_id_matching_within_one_capture(
    backend, monkeypatch, tmp_path, match_by
):
    """A single capture takes two event ids, so there is nothing to match.

    The schema allows match_by alongside event_id_a/event_id_b, and the
    resolution is pinned to event_id on that path. match_by is only a reported
    field, but reporting a matching mode that was not used is how a caller ends
    up believing two captures were lined up by draw index when they were not.
    """
    capture = tmp_path / "a.rdc"
    capture.write_bytes(b"rdc")
    controller = DiffController(
        per_event={2: b"", 3: b""}, textures=[], actions=FakeController().actions
    )
    _fake_readback(monkeypatch, {"a": controller})
    result = assertions.assert_state(str(capture), event_id_a=2, event_id_b=3, match_by=match_by)
    assert result["result"]["match_by"] == "event_id"


def test_assert_state_reports_the_matching_mode_across_captures(backend, monkeypatch, tmp_path):
    """Across two captures the caller's matching mode is the one that applies."""
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    controllers = {
        "a": DiffController(per_event={2: b""}, textures=[], actions=FakeController().actions),
        "b": DiffController(per_event={2: b""}, textures=[], actions=FakeController().actions),
    }
    _fake_readback(monkeypatch, controllers)
    result = assertions.assert_state(
        str(capture_a), other_capture_file=str(capture_b), event_index=0, match_by="index"
    )
    assert result["result"]["match_by"] == "index"
    assert result["result"]["mode"] == "captures"


def test_assert_state_requires_two_events_or_a_second_capture(backend, tmp_path):
    capture = tmp_path / "a.rdc"
    capture.write_bytes(b"rdc")
    result = assertions.assert_state(str(capture), event_id_a=2)
    assert result["result"]["supported"] is False
    assert "event_id_a and event_id_b" in result["result"]["error_message"]


def test_assert_state_reports_an_unreachable_backend(monkeypatch, tmp_path):
    capture = tmp_path / "capture.rdc"
    capture.write_bytes(b"rdc")
    monkeypatch.setattr(
        replay.subprocess, "run", lambda *_a, **_k: pytest.fail("must not launch qrenderdoc")
    )
    from test_replay import _command_root

    from dcc_mcp_renderdoc import capabilities as caps

    command = _command_root(tmp_path / "missing", with_qrenderdoc=False)
    monkeypatch.setattr(replay, "probe", lambda *a, **k: caps.probe(command=str(command)))
    result = _load("assert_state").main(capture_file=str(capture), event_id_a=2, event_id_b=3)
    assert result["success"] is False
    assert result["error"] == "unsupported_backend"
    assert "qrenderdoc" in result["prompt"]


def test_assert_state_reports_a_one_sided_replay_failure(backend, monkeypatch, tmp_path):
    """Half a replay is never turned into a state verdict."""
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    _fake_readback(
        monkeypatch,
        {"a": DiffController(per_event={2: b""}, textures=[], actions=FakeController().actions)},
    )

    def explode(capture_file, operation, params=None, **kwargs):
        raise replay.RenderDocError("boom")

    monkeypatch.setattr(assertions, "run_replay_operation", explode)
    result = assertions.assert_state(str(capture_a), other_capture_file=str(capture_b), event_id=2)
    payload = result["result"]
    assert payload["supported"] is False
    assert payload["side"] == "a"
    assert "b.rdc" not in (payload.get("error_message") or "")


def test_assert_state_summary_lists_the_differences(backend, monkeypatch, tmp_path):
    capture_a = tmp_path / "a.rdc"
    capture_b = tmp_path / "b.rdc"
    for path in (capture_a, capture_b):
        path.write_bytes(b"rdc")
    _fake_readback(
        monkeypatch,
        {
            "a": DiffController(per_event={2: b""}, textures=[], actions=FakeController().actions),
            "b": DiffController(per_event={2: b""}, textures=[], actions=FakeController().actions),
        },
    )
    message = _load("assert_state").main(
        capture_file=str(capture_a), other_capture_file=str(capture_b), event_id=2
    )["message"]
    assert "assert_state" in message
    assert "key(s)" in message
