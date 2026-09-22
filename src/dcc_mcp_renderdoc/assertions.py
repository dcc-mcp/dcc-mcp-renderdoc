"""Host-side CI assertion gates for the RenderDoc analysis tools.

This is the layer that turns a measurement into a verdict. :mod:`diff` answers
"how different are these two images?"; this module answers "is that difference
acceptable?", which is the question a CI gate actually asks.

Two properties carry over from the diff layer on purpose:

* **A gate that cannot be evaluated is not a gate that passed.** When the two
  sides cannot be compared, the result says so and reports `passed: false` with
  `evaluable: false`, so a threshold check never reads a missing measurement as
  a zero.
* **Nothing here raises on a regression.** A failing assertion is a normal,
  successful tool call that reports a verdict. Only a backend or replay problem
  comes back as an unsupported result, the same as every other analysis tool.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .diff import (
    DIFF_CAPTURES,
    DIFF_DRAWS,
    diff_region,
)
from .replay import (
    RenderDocError,
    run_replay_operation,
    unsupported_backend,
)

#: Operation names this module reports, one per skill tool.
ASSERT_PIXELS = "assert_pixels"
ASSERT_STATE = "assert_state"

#: The pixel thresholds a caller may gate on, and how each one is read.
#: ``lower_is_better`` is the only thing that differs between them, so the
#: comparison is driven by the table rather than by three near-identical blocks.
PIXEL_CHECKS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "mean_abs_diff",
        "threshold_arg": "max_mean_abs_diff",
        "metric": "mean_abs_diff",
        "comparison": "max",
        "unit": "decoded texel value",
    },
    {
        "name": "psnr",
        "threshold_arg": "min_psnr",
        "metric": "psnr",
        "comparison": "min",
        "unit": "dB",
    },
    {
        "name": "failed_texel_ratio",
        "threshold_arg": "max_failed_pixel_ratio",
        "metric": "failed_texel_ratio",
        "comparison": "max",
        "unit": "fraction of compared texels",
    },
)


def selected_pixel_checks(
    *,
    max_mean_abs_diff: Optional[float] = None,
    min_psnr: Optional[float] = None,
    max_failed_pixel_ratio: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Pair each supplied threshold with its check, or explain that none was.

    A gate with no criterion always passes, which is worse than no gate at all,
    so an empty set is reported instead of quietly returning a green result.
    """
    supplied = {
        "max_mean_abs_diff": max_mean_abs_diff,
        "min_psnr": min_psnr,
        "max_failed_pixel_ratio": max_failed_pixel_ratio,
    }
    checks = []
    names = []
    for check in PIXEL_CHECKS:
        threshold = supplied[check["threshold_arg"]]
        if threshold is None:
            continue
        entry = dict(check)
        entry["threshold"] = float(threshold)
        checks.append(entry)
        names.append(check["name"])
    return checks, names


def _evaluate(measured: Optional[float], check: Mapping[str, Any]) -> Optional[bool]:
    """Whether ``measured`` satisfies one threshold.

    Returns ``None`` when there is nothing to compare, which is a different
    outcome from failing: a gate cannot be judged on a missing measurement.
    """
    if measured is None:
        return None
    threshold = float(check["threshold"])
    if check["comparison"] == "max":
        return measured <= threshold
    return measured >= threshold


def _identical_passes_psnr(metrics: Mapping[str, Any], check: Mapping[str, Any]) -> bool:
    """PSNR has no finite value for identical images, and that must pass.

    Two identical images have an infinite PSNR, so ``psnr`` is ``None``. Under a
    ``min_psnr`` gate that is the best possible outcome, not a missing
    measurement -- treating it as ``None`` would fail a perfect frame.
    """
    return bool(metrics.get("identical")) and check["metric"] == "psnr"


def evaluate_pixel_checks(metrics: Mapping[str, Any], checks: Sequence[Mapping[str, Any]]):
    """Run every supplied threshold against one diff's metrics."""
    results = []
    evaluated = []
    unevaluated = []
    for check in checks:
        measured = metrics.get(check["metric"])
        passed = _evaluate(measured, check)
        if passed is None and _identical_passes_psnr(metrics, check):
            passed = True
        entry = {
            "name": check["name"],
            "metric": check["metric"],
            "comparison": check["comparison"],
            "threshold": check["threshold"],
            "measured": measured,
            "unit": check["unit"],
            "passed": passed,
        }
        results.append(entry)
        if passed is None:
            unevaluated.append(entry)
        else:
            evaluated.append(entry)
    return results, evaluated, unevaluated


def assert_pixels(
    capture_file: str,
    *,
    other_capture_file: Optional[str] = None,
    event_id_a: Optional[int] = None,
    event_id_b: Optional[int] = None,
    match_by: str = "event_id",
    event_id: Optional[int] = None,
    event_index: Optional[int] = None,
    event_name: Optional[str] = None,
    resource_id: Any = None,
    other_resource_id: Any = None,
    max_mean_abs_diff: Optional[float] = None,
    min_psnr: Optional[float] = None,
    max_failed_pixel_ratio: Optional[float] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    mip: int = 0,
    slice_index: int = 0,
    sample_index: int = 0,
    max_texels: Optional[int] = None,
    channels: Optional[Sequence[int]] = None,
    max_value: Optional[float] = None,
    threshold: Optional[float] = None,
    ssim_grid_step: Optional[int] = None,
    output_file: Optional[str] = None,
    force: bool = False,
    timeout_secs: int = 300,
    command: Optional[str] = None,
) -> Dict[str, Any]:
    """Gate one region's difference against the thresholds the caller supplies.

    Delegates the measurement to :func:`dcc_mcp_renderdoc.diff.diff_region` so
    there is exactly one implementation of the pixel comparison, then turns the
    numbers into a verdict. Returns the same
    ``{"capture_file", "operation", "result"}`` shape the other drivers do.
    """
    checks, names = selected_pixel_checks(
        max_mean_abs_diff=max_mean_abs_diff,
        min_psnr=min_psnr,
        max_failed_pixel_ratio=max_failed_pixel_ratio,
    )
    if not checks:
        return _unsupported(
            ASSERT_PIXELS,
            capture_file,
            "assert_pixels needs at least one threshold: max_mean_abs_diff, min_psnr, "
            "or max_failed_pixel_ratio",
            "a gate with no criterion always passes, so name the tolerance you mean",
        )
    operation = DIFF_CAPTURES if other_capture_file else DIFF_DRAWS
    if operation == DIFF_DRAWS and (event_id_a is None or event_id_b is None):
        return _unsupported(
            ASSERT_PIXELS,
            capture_file,
            "event_id_a and event_id_b are required when comparing two draws of one capture",
            "pass both event ids, or pass other_capture_file to compare two captures",
        )
    # Within one capture both event ids are known. Across two captures the
    # caller supplies one event and match_by decides how it is resolved on
    # each side, so no id is needed for index or name matching.
    if operation == DIFF_DRAWS:
        event_ids: Optional[List[int]] = [event_id_a, event_id_b]
    elif event_id is None:
        event_ids = None
    else:
        event_ids = [event_id]
    report = diff_region(
        operation,
        capture_file,
        other_capture_file=other_capture_file,
        event_ids=event_ids,
        match_by=match_by,
        event_index=event_index,
        event_name=event_name,
        resource_id=resource_id,
        other_resource_id=other_resource_id,
        x=x,
        y=y,
        width=width,
        height=height,
        mip=mip,
        slice_index=slice_index,
        sample_index=sample_index,
        max_texels=max_texels,
        channels=channels,
        max_value=max_value,
        **_optional(threshold=threshold, ssim_grid_step=ssim_grid_step),
        output_file=output_file,
        force=force,
        timeout_secs=timeout_secs,
        command=command,
    )
    payload = report["result"]
    if payload.get("supported") is False:
        return report
    result: Dict[str, Any] = {
        "supported": True,
        "operation": ASSERT_PIXELS,
        "mode": "captures" if operation == DIFF_CAPTURES else "draws",
        "comparable": payload.get("comparable"),
        "thresholds_checked": names,
        "sides": payload.get("sides"),
        "match_by": payload.get("match_by"),
        "estimate": payload.get("estimate"),
        "estimate_method": payload.get("estimate_method"),
    }
    if payload.get("comparable") is False:
        # Nothing was measured, so no threshold can be judged. This is reported
        # as unevaluated rather than as a pass or a regression.
        result.update(
            {
                "reason_code": payload.get("reason_code"),
                "reason": payload.get("reason"),
                "passed": False,
                "evaluable": False,
                "checks": [],
                "evaluated_checks": [],
                "unevaluated_checks": names,
                "metrics": None,
            }
        )
        return {
            "capture_file": report["capture_file"],
            "operation": ASSERT_PIXELS,
            "result": result,
        }
    metrics = payload.get("metrics") or {}
    checks_result, evaluated, unevaluated = evaluate_pixel_checks(metrics, checks)
    result.update(
        {
            "passed": all(entry["passed"] for entry in evaluated) and not unevaluated,
            "evaluable": not unevaluated,
            "checks": checks_result,
            "evaluated_checks": evaluated,
            "unevaluated_checks": [entry["name"] for entry in unevaluated],
            "failed_checks": [entry for entry in evaluated if entry["passed"] is False],
            "metrics": metrics,
            "output_file": metrics.get("output_file"),
        }
    )
    if payload.get("force_applied"):
        result["forced_reason"] = payload.get("reason")
    return {"capture_file": report["capture_file"], "operation": ASSERT_PIXELS, "result": result}


def _optional(**values: Any) -> Dict[str, Any]:
    """Keep unset optional arguments out of the delegated call."""
    return {key: value for key, value in values.items() if value is not None}


def _unsupported(
    operation: str, capture_file: str, message: str, hint: Optional[str]
) -> Dict[str, Any]:
    return {
        "capture_file": str(capture_file),
        "operation": operation,
        "result": {"supported": False, "error_message": message, "hint": hint},
    }


# --------------------------------------------------------------------------- #
# State assertions
# --------------------------------------------------------------------------- #


def _read_state_side(
    capture_file: str,
    params: Mapping[str, Any],
    side: str,
    *,
    timeout_secs: int,
    command: Optional[str],
) -> Dict[str, Any]:
    """Run one state readback, turning any failure into a structured report.

    Mirrors ``diff._read_one_side``: a cross-capture comparison replays twice,
    and the second replay has to be able to fail on its own without the result
    being turned into a comparison against nothing.
    """
    unsupported = unsupported_backend("inspect", command=command)
    if unsupported is not None:
        return unsupported
    try:
        return run_replay_operation(
            capture_file,
            "read_state_fingerprint",
            params,
            timeout_secs=timeout_secs,
            command=command,
        )
    except RenderDocError as exc:
        return {
            "supported": False,
            "side": side,
            "capture_file": str(capture_file),
            "error_message": "side {} ({}) could not be replayed: {}".format(
                side, capture_file, exc
            ),
            "hint": "both sides have to replay for a state assertion to mean anything",
        }


def compare_fingerprints(
    left: Mapping[str, Any], right: Mapping[str, Any], *, ignore_keys: Optional[Sequence[str]]
):
    """Diff two pipeline-state fingerprints, skipping the keys the caller names.

    Returns ``(differences, ignored, compared)``. A key present on only one side
    is a difference with ``None`` on the other, because a state section one
    replay exposes and the other does not is itself a difference worth seeing.
    """
    skipped = set(ignore_keys or ())
    before = left.get("fingerprint") or {}
    after = right.get("fingerprint") or {}
    differences = []
    compared = []
    for key in sorted(set(before) | set(after)):
        if key in skipped:
            continue
        compared.append(key)
        if before.get(key) == after.get(key):
            continue
        differences.append(
            {
                "key": key,
                "before": before.get(key),
                "after": after.get(key),
            }
        )
    ignored = sorted(skipped & (set(before) | set(after)))
    return differences, ignored, compared


def assert_state(
    capture_file: str,
    *,
    other_capture_file: Optional[str] = None,
    event_id_a: Optional[int] = None,
    event_id_b: Optional[int] = None,
    match_by: str = "event_id",
    event_id: Optional[int] = None,
    event_index: Optional[int] = None,
    event_name: Optional[str] = None,
    ignore_keys: Optional[Sequence[str]] = None,
    timeout_secs: int = 300,
    command: Optional[str] = None,
) -> Dict[str, Any]:
    """Assert that two events' pipeline states match, apart from ignored keys.

    Within one capture this is a single replay moved between the two events;
    across two captures it is two replays, because a RenderDoc replay context
    holds one capture at a time. Fingerprints are small enough to ride back in
    the status document, so unlike the pixel path there is nothing to stage on
    disk.
    """
    if other_capture_file:
        event_ids = None if event_id is None else [event_id]
        params = _state_params(match_by, event_ids, event_index, event_name)
        snapshots = []
        for side, capture in (("a", capture_file), ("b", other_capture_file)):
            report = _read_state_side(
                capture, params, side, timeout_secs=timeout_secs, command=command
            )
            problem = _snapshots(report, side, capture)
            if problem is not None:
                return problem
            snapshots.append(report["result"]["snapshots"][0])
    else:
        if event_id_a is None or event_id_b is None:
            return _unsupported(
                ASSERT_STATE,
                capture_file,
                "event_id_a and event_id_b are required when comparing two events of one capture",
                "pass both event ids, or pass other_capture_file to compare two captures",
            )
        params = _state_params("event_id", [event_id_a, event_id_b], None, None)
        report = _read_state_side(
            capture_file, params, "events", timeout_secs=timeout_secs, command=command
        )
        if report.get("supported") is False:
            return {"capture_file": str(capture_file), "operation": ASSERT_STATE, "result": report}
        payload = report["result"]
        if payload.get("supported") is False:
            payload = dict(payload)
            payload.setdefault("capture_file", str(capture_file))
            return {"capture_file": str(capture_file), "operation": ASSERT_STATE, "result": payload}
        found = payload.get("snapshots") or []
        if len(found) < 2:
            return _unsupported(
                ASSERT_STATE,
                capture_file,
                "assert_state needs two events, got {}".format(len(found)),
                "pass both event_id_a and event_id_b",
            )
        snapshots = found[:2]

    left, right = snapshots[0], snapshots[1]
    differences, ignored, compared = compare_fingerprints(left, right, ignore_keys=ignore_keys)
    result: Dict[str, Any] = {
        "supported": True,
        "operation": ASSERT_STATE,
        "mode": "captures" if other_capture_file else "events",
        # The bridge puts match_by on the result, not on each snapshot, so
        # reading it off the snapshot always fell through to the caller's
        # argument. Within one capture there is nothing to match: both event
        # ids are given, so the resolution is event_id whatever was passed.
        "match_by": match_by if other_capture_file else "event_id",
        "passed": not differences,
        "difference_count": len(differences),
        "differences": differences,
        "compared_keys": compared,
        "ignored_keys": ignored,
        "unknown_ignore_keys": sorted(set(ignore_keys or ()) - set(ignored)),
        "events": [
            {"side": "before", "event_id": left.get("event_id")},
            {"side": "after", "event_id": right.get("event_id")},
        ],
    }
    return {"capture_file": str(capture_file), "operation": ASSERT_STATE, "result": result}


def _state_params(
    match_by: str,
    event_ids: Optional[Sequence[int]],
    event_index: Optional[int],
    event_name: Optional[str],
) -> Dict[str, Any]:
    params: Dict[str, Any] = {"match_by": match_by}
    if event_ids:
        params["event_ids"] = list(event_ids)
    if event_index is not None:
        params["event_index"] = event_index
    if event_name is not None:
        params["event_name"] = event_name
    return params


def _snapshots(report: Mapping[str, Any], side: str, capture_file: str):
    """Validate one side's state readback, or return the structured failure."""
    if report.get("supported") is False:
        return {
            "capture_file": str(capture_file),
            "operation": ASSERT_STATE,
            "result": report,
        }
    payload = report["result"]
    if payload.get("supported") is False:
        payload = dict(payload)
        payload.setdefault("side", side)
        payload.setdefault("capture_file", str(capture_file))
        return {
            "capture_file": str(capture_file),
            "operation": ASSERT_STATE,
            "result": payload,
        }
    if not (payload.get("snapshots") or []):
        return _unsupported(
            ASSERT_STATE,
            capture_file,
            "no pipeline state was read for side {} ({})".format(side, capture_file),
            "pick events this capture's replay can move to",
        )
    return None
