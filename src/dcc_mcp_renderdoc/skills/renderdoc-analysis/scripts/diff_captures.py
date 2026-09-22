from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.diff import DIFF_CAPTURES, diff_region


@skill_entry
def main(
    capture_file: str,
    other_capture_file: str,
    resource_id: int,
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
    result = diff_region(
        DIFF_CAPTURES,
        capture_file,
        other_capture_file=other_capture_file,
        match_by=match_by,
        event_ids=None if event_id is None else [event_id],
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
        sample_index=sample,
        max_texels=max_texels,
        channels=channels,
        max_value=max_value,
        ssim_grid_step=8 if ssim_grid_step is None else ssim_grid_step,
        threshold=1.0 / 255.0 if threshold is None else threshold,
        output_file=output_file,
        force=force,
    )
    payload = result["result"]
    if payload.get("supported") is False:
        # A missing deep backend is a different fix from one capture refusing
        # to replay, so the two are reported under different error kinds.
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
    sides = payload.get("sides") or [{}, {}]
    text = "Compared resource {} in two captures (matched by {}): ".format(
        sides[0].get("resource_name") or resource_id, payload.get("match_by")
    )
    if payload.get("comparable") is False:
        # A mismatch is an answer, not a failure: the caller asked for a
        # comparison that cannot be made honestly, and is told why.
        text += "not comparable ({}): {}. Pass force=true to compare anyway.".format(
            payload.get("reason_code") or "not_comparable", payload.get("reason")
        )
        return skill_success(text, **result)
    metrics = payload.get("metrics") or {}
    if payload.get("forced_reason"):
        text += "forced past {}; ".format(payload.get("reason_code") or "a mismatch")
    if metrics.get("identical"):
        text += "the two are identical (PSNR has no finite value; identical=true)."
    else:
        text += "PSNR {}, mean |diff| {}, max |diff| {}.".format(
            _fmt(metrics.get("psnr")),
            _fmt(metrics.get("mean_abs_diff")),
            _fmt(metrics.get("max_abs_diff")),
        )
    basis = metrics.get("psnr_basis") or {}
    text += " PSNR basis: {}-bit, {} channel(s), max_value {} from {}.".format(
        basis.get("bit_depth"),
        basis.get("channel_count"),
        basis.get("max_value"),
        basis.get("max_value_source"),
    )
    text += " {} of {} texel(s) exceed the {} threshold.".format(
        metrics.get("failed_texel_count", 0),
        metrics.get("compared_texel_count", 0),
        _fmt(metrics.get("threshold")),
    )
    non_finite = metrics.get("non_finite") or {}
    if non_finite.get("excluded_texel_count"):
        text += " {} texel(s) held NaN/Inf and were excluded ({} NaN, {} Inf mismatched).".format(
            non_finite.get("excluded_texel_count", 0),
            non_finite.get("nan_mismatch_count", 0),
            non_finite.get("inf_mismatch_count", 0),
        )
    if metrics.get("ssim_approx") is None:
        text += " ssim_approx unavailable ({}).".format(
            metrics.get("ssim_skipped_reason") or "not computed"
        )
    else:
        # The approximation is not the standard SSIM, so its method travels in
        # the summary and not only in SKILL.md.
        text += (
            " ssim_approx {} over {}x{} blocks with a {} window on a grid of every "
            "{}th pixel - an approximation, not the standard 11x11 Gaussian SSIM."
        ).format(
            _fmt(metrics.get("ssim_approx")),
            metrics.get("ssim_block_size"),
            metrics.get("ssim_block_size"),
            metrics.get("ssim_window"),
            metrics.get("ssim_grid_step"),
        )
    if payload.get("estimate"):
        text += " Estimated: {}.".format(payload.get("estimate_method"))
    if (metrics.get("output_file") or {}).get("path"):
        text += " Difference heatmap written to {}.".format(metrics["output_file"]["path"])
    return skill_success(text, **result)


def _fmt(value):
    """Render one metric for prose without claiming more digits than a float."""
    if value is None:
        return "n/a"
    return "{:.6g}".format(float(value))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
