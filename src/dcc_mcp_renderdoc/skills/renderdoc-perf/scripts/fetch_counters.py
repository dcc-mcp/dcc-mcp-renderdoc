from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_perf_operation


@skill_entry
def main(
    capture_file: str,
    counter_ids: Optional[list] = None,
    first_event_id: Optional[int] = None,
    last_event_id: Optional[int] = None,
    limit: Optional[int] = None,
    **_kwargs,
):
    result = run_perf_operation(
        capture_file,
        "get_counters",
        clean_params(
            fetch=True,
            counter_ids=counter_ids,
            first_event_id=first_event_id,
            last_event_id=last_event_id,
            limit=limit,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    payload = result["result"]
    missing = payload.get("missing_counter_ids") or []
    if missing:
        return skill_error(
            "RenderDoc does not expose counter id(s) {} in this capture; call "
            "renderdoc_perf__list_counters to see what this driver offers.".format(
                ", ".join(str(item) for item in missing)
            ),
            "unknown_counter",
            **result,
        )
    return skill_success(
        "RenderDoc sampled {} counter value(s).".format(payload.get("value_count", 0)), **result
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
