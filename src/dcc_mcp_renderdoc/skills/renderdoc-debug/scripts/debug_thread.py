from typing import Optional, Sequence

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_debug_operation


@skill_entry
def main(
    capture_file: str,
    event_id: Optional[int] = None,
    group_id: Optional[Sequence[int]] = None,
    thread_id: Optional[Sequence[int]] = None,
    max_steps: Optional[int] = None,
    detail: Optional[str] = None,
    **_kwargs,
):
    result = run_debug_operation(
        capture_file,
        "debug_thread",
        clean_params(
            event_id=event_id,
            group_id=list(group_id) if group_id is not None else None,
            thread_id=list(thread_id) if thread_id is not None else None,
            max_steps=max_steps,
            detail=detail,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    return skill_success("RenderDoc stepped a compute shader invocation.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
