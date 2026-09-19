from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_analysis_operation


@skill_entry
def main(
    capture_file: str,
    counter_id: Optional[int] = None,
    pass_depth: Optional[int] = None,
    max_depth: Optional[int] = None,
    slowest_actions: Optional[int] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    **_kwargs,
):
    result = run_analysis_operation(
        capture_file,
        "get_pass_timing",
        clean_params(
            counter_id=counter_id,
            pass_depth=pass_depth,
            max_depth=max_depth,
            slowest_actions=slowest_actions,
            offset=offset,
            limit=limit,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    payload = result["result"]
    if payload.get("supported") is False:
        # The backend is reachable but this driver exposes no timing counter, so
        # the honest answer is "not measurable here" with the counters that do
        # exist, not a frame of zero-duration passes that looks free.
        return skill_error(
            payload["error_message"],
            "unsupported_capture",
            prompt=payload["hint"],
            hint=payload["hint"],
            **result,
        )
    totals = payload.get("totals") or {}
    return skill_success(
        "RenderDoc timed {} pass(es), {} in total. A pass reports its own counter "
        "sample when the driver sampled it and the sum of its timed actions "
        "otherwise; the method is reported per pass.".format(
            payload.get("pass_count", 0), totals.get("pass_total", 0.0)
        ),
        **result,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
