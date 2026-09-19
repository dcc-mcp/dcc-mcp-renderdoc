from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_perf_operation


@skill_entry
def main(
    capture_file: str,
    output_file: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    depth_test: Optional[bool] = None,
    first_event_id: Optional[int] = None,
    last_event_id: Optional[int] = None,
    event_ids: Optional[list] = None,
    max_draws: Optional[int] = None,
    max_triangles: Optional[int] = None,
    **_kwargs,
):
    result = run_perf_operation(
        capture_file,
        "get_overdraw",
        clean_params(
            output_file=output_file,
            width=width,
            height=height,
            depth_test=depth_test,
            first_event_id=first_event_id,
            last_event_id=last_event_id,
            event_ids=event_ids,
            max_draws=max_draws,
            max_triangles=max_triangles,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    payload = result["result"]
    return skill_success(
        "CPU-estimated overdraw over {} draw(s): average {:.2f}x over {} covered "
        "pixel(s). Estimated by CPU rasterisation of post-VS geometry, not "
        "measured by RenderDoc's GPU quad-overdraw overlay.".format(
            payload.get("draw_count", 0),
            payload.get("average_overdraw", 0.0),
            payload.get("covered_pixels", 0),
        ),
        **result,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
