from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_perf_operation


@skill_entry
def main(
    capture_file: str,
    name_filter: Optional[str] = None,
    category_filter: Optional[str] = None,
    limit: Optional[int] = None,
    **_kwargs,
):
    result = run_perf_operation(
        capture_file,
        "get_counters",
        clean_params(
            name_filter=name_filter,
            category_filter=category_filter,
            limit=limit,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    count = result["result"].get("counter_count", 0)
    return skill_success(
        "RenderDoc exposed {} counter(s) for this capture.".format(count), **result
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
