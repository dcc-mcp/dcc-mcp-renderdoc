from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_perf_operation


@skill_entry
def main(
    capture_file: str,
    counter_id: Optional[int] = None,
    name_filter: Optional[str] = None,
    flag_filter: Optional[str] = None,
    max_depth: Optional[int] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    slowest: Optional[int] = None,
    **_kwargs,
):
    result = run_perf_operation(
        capture_file,
        "get_action_timing",
        clean_params(
            counter_id=counter_id,
            name_filter=name_filter,
            flag_filter=flag_filter,
            max_depth=max_depth,
            offset=offset,
            limit=limit,
            slowest=slowest,
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
        # exist, not an empty timing table that looks like a fast frame. The hint
        # is passed twice on purpose: ``prompt`` is the recovery message the
        # caller is shown, ``hint`` keeps it readable in the same place every
        # other backend report carries it.
        return skill_error(
            payload["error_message"],
            "unsupported_capture",
            prompt=payload["hint"],
            hint=payload["hint"],
            **result,
        )
    totals = payload.get("totals") or {}
    return skill_success(
        "RenderDoc timed {} action(s), {} in total.".format(
            payload.get("action_count", 0), totals.get("total", 0.0)
        ),
        **result,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
