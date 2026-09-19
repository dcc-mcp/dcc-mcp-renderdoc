from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_analysis_operation


@skill_entry
def main(
    capture_file: str,
    first_event_id: Optional[int] = None,
    last_event_id: Optional[int] = None,
    max_events: Optional[int] = None,
    max_changes: Optional[int] = None,
    min_run_length: Optional[int] = None,
    max_depth: Optional[int] = None,
    **_kwargs,
):
    result = run_analysis_operation(
        capture_file,
        "analyze_state_changes",
        clean_params(
            first_event_id=first_event_id,
            last_event_id=last_event_id,
            max_events=max_events,
            max_changes=max_changes,
            min_run_length=min_run_length,
            max_depth=max_depth,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    payload = result["result"]
    top = (payload.get("changes_by_key") or [{}])[0]
    return skill_success(
        "Compared {} draw(s): {} state change(s), most often {}. {} draw(s) sit in "
        "{} run(s) of identical state, so those switches were avoidable.".format(
            payload.get("analyzed_event_count", 0),
            payload.get("change_count", 0),
            top.get("key") or "none",
            payload.get("batchable_draw_count", 0),
            len(payload.get("runs") or []),
        ),
        **result,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
