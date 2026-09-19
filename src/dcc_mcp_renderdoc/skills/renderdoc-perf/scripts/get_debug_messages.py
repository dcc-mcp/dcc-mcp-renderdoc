from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_perf_operation


@skill_entry
def main(
    capture_file: str,
    event_id: Optional[int] = None,
    severity_filter: Optional[str] = None,
    category_filter: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    **_kwargs,
):
    result = run_perf_operation(
        capture_file,
        "get_debug_messages",
        clean_params(
            event_id=event_id,
            severity_filter=severity_filter,
            category_filter=category_filter,
            offset=offset,
            limit=limit,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    payload = result["result"]
    return skill_success(
        "RenderDoc reported {} debug message(s).".format(payload.get("message_count", 0)),
        **result,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
