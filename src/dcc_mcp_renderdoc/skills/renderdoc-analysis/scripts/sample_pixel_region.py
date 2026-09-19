from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_analysis_operation


@skill_entry
def main(
    capture_file: str,
    resource_id: int,
    x: Optional[int] = None,
    y: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    grid_x: Optional[int] = None,
    grid_y: Optional[int] = None,
    mip: int = 0,
    slice_index: int = 0,
    sample: int = 0,
    event_id: Optional[int] = None,
    max_samples: Optional[int] = None,
    **_kwargs,
):
    result = run_analysis_operation(
        capture_file,
        "sample_pixel_region",
        clean_params(
            resource_id=resource_id,
            x=x,
            y=y,
            width=width,
            height=height,
            grid_x=grid_x,
            grid_y=grid_y,
            mip=mip,
            slice=slice_index,
            sample=sample,
            event_id=event_id,
            max_samples=max_samples,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    payload = result["result"]
    if payload.get("supported") is False:
        return skill_error(
            payload["error_message"],
            "unsupported_capture",
            prompt=payload["hint"],
            hint=payload["hint"],
            **result,
        )
    stats = payload.get("stats") or {}
    channels = stats.get("channels") or []
    text = "Sampled {} point(s) of {} in a {}x{} region.".format(
        payload.get("sample_count", 0),
        payload.get("resource_name") or "resource {}".format(payload.get("resource_id")),
        (payload.get("region") or {}).get("width", 0),
        (payload.get("region") or {}).get("height", 0),
    )
    if channels:
        text += " Per-channel min {} / max {}.".format(
            [channel.get("min") for channel in channels],
            [channel.get("max") for channel in channels],
        )
    if payload.get("estimate"):
        # The estimate belongs in the agent's context, not only in SKILL.md:
        # the grid decides how much of the region was actually read.
        text += " Estimated from a grid over the region ({}) - not every texel was read.".format(
            payload.get("estimate_method")
        )
    return skill_success(text, **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
