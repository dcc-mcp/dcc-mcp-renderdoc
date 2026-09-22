from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.assertions import assert_pixels


@skill_entry
def main(
    capture_file: str,
    resource_id: int,
    max_mean_abs_diff: Optional[float] = None,
    min_psnr: Optional[float] = None,
    max_failed_pixel_ratio: Optional[float] = None,
    other_capture_file: Optional[str] = None,
    event_id_a: Optional[int] = None,
    event_id_b: Optional[int] = None,
    match_by: str = "event_id",
    event_id: Optional[int] = None,
    event_index: Optional[int] = None,
    event_name: Optional[str] = None,
    other_resource_id: Optional[int] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    mip: int = 0,
    slice_index: int = 0,
    sample: int = 0,
    max_texels: Optional[int] = None,
    channels: Optional[list] = None,
    max_value: Optional[float] = None,
    threshold: Optional[float] = None,
    ssim_grid_step: Optional[int] = None,
    output_file: Optional[str] = None,
    force: bool = False,
    **_kwargs,
):
    result = assert_pixels(
        capture_file,
        other_capture_file=other_capture_file,
        event_id_a=event_id_a,
        event_id_b=event_id_b,
        match_by=match_by,
        event_id=event_id,
        event_index=event_index,
        event_name=event_name,
        resource_id=resource_id,
        other_resource_id=other_resource_id,
        max_mean_abs_diff=max_mean_abs_diff,
        min_psnr=min_psnr,
        max_failed_pixel_ratio=max_failed_pixel_ratio,
        x=x,
        y=y,
        width=width,
        height=height,
        mip=mip,
        slice_index=slice_index,
        sample_index=sample,
        max_texels=max_texels,
        channels=channels,
        max_value=max_value,
        threshold=threshold,
        ssim_grid_step=ssim_grid_step,
        output_file=output_file,
        force=force,
    )
    payload = result["result"]
    if payload.get("supported") is False:
        # A missing deep backend is a different fix from a capture that will not
        # replay, so the two are reported under different error kinds.
        if payload.get("capability_group"):
            return skill_error(
                payload["error_message"], "unsupported_backend", prompt=payload["hint"], **result
            )
        return skill_error(
            payload["error_message"],
            "unsupported_capture",
            prompt=payload["hint"],
            hint=payload["hint"],
            **result,
        )
    if payload.get("evaluable") is False:
        # Nothing was measured, so no threshold was judged. That is not a
        # regression and it is not a pass either.
        text = "assert_pixels could not evaluate {}: {} ({}).".format(
            ", ".join(payload.get("unevaluated_checks") or []) or "the gate",
            payload.get("reason") or "the two sides are not comparable",
            payload.get("reason_code") or "not_comparable",
        )
        text += " No threshold was judged, so this is neither a pass nor a regression."
        return skill_error(text, "unsupported_capture", **result)
    text = "assert_pixels {}: {} of {} check(s) passed.".format(
        "PASSED" if payload.get("passed") else "FAILED",
        len(payload.get("evaluated_checks") or []),
        len(payload.get("checks") or []),
    )
    for entry in payload.get("checks") or []:
        text += " {} {} {} (measured {}).".format(
            entry["name"],
            "<=" if entry["comparison"] == "max" else ">=",
            entry["threshold"],
            _fmt(entry["measured"]) if entry["measured"] is not None else "identical (infinite)",
        )
    if payload.get("forced_reason"):
        text += " Forced past a mismatch: {}.".format(payload.get("forced_reason"))
    if payload.get("estimate"):
        text += " Estimated: {}.".format(payload.get("estimate_method"))
    metrics = payload.get("metrics") or {}
    non_finite = metrics.get("non_finite") or {}
    if non_finite.get("excluded_texel_count"):
        # Reported rather than gated: a NaN is worth seeing, but it is not one
        # of the thresholds this tool was specified to enforce.
        text += " {} texel(s) held NaN/Inf and were excluded from the metrics.".format(
            non_finite["excluded_texel_count"]
        )
    if (metrics.get("output_file") or {}).get("path"):
        text += " Difference heatmap written to {}.".format(metrics["output_file"]["path"])
    return skill_success(text, **result)


def _fmt(value):
    """Render one measurement for prose without claiming false precision."""
    return "{:.6g}".format(float(value))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
