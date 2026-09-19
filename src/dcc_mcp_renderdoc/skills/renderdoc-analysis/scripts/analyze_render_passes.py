from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_analysis_operation


@skill_entry
def main(
    capture_file: str,
    pass_depth: Optional[int] = None,
    max_depth: Optional[int] = None,
    name_filter: Optional[str] = None,
    max_actions_per_pass: Optional[int] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    **_kwargs,
):
    result = run_analysis_operation(
        capture_file,
        "analyze_render_passes",
        clean_params(
            pass_depth=pass_depth,
            max_depth=max_depth,
            name_filter=name_filter,
            max_actions_per_pass=max_actions_per_pass,
            offset=offset,
            limit=limit,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    payload = result["result"]
    totals = payload.get("totals") or {}
    return skill_success(
        "RenderDoc structured {} pass(es): {} draw(s), {} dispatch(es). Triangle "
        "counts are CPU-derived estimates (numIndices / 3 * numInstances, before GPU "
        "culling), not measured geometry.".format(
            payload.get("pass_count", 0),
            totals.get("draw_count", 0),
            totals.get("dispatch_count", 0),
        ),
        **result,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
