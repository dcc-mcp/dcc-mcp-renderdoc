from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_debug_operation


@skill_entry
def main(
    capture_file: str,
    event_id: Optional[int] = None,
    vertex_index: Optional[int] = None,
    instance: Optional[int] = None,
    index: Optional[int] = None,
    instance_offset: Optional[int] = None,
    vertex_offset: Optional[int] = None,
    max_steps: Optional[int] = None,
    detail: Optional[str] = None,
    **_kwargs,
):
    result = run_debug_operation(
        capture_file,
        "debug_vertex",
        clean_params(
            event_id=event_id,
            vertex_index=vertex_index,
            instance=instance,
            index=index,
            instance_offset=instance_offset,
            vertex_offset=vertex_offset,
            max_steps=max_steps,
            detail=detail,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    return skill_success("RenderDoc stepped a vertex shader invocation.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
