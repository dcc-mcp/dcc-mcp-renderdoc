from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_analysis_operation


@skill_entry
def main(
    capture_file: str,
    event_id: int,
    stages: Optional[list] = None,
    include_constant_buffers: bool = False,
    include_source: bool = False,
    variable_limit: Optional[int] = None,
    **_kwargs,
):
    result = run_analysis_operation(
        capture_file,
        "get_draw_call_state",
        clean_params(
            event_id=event_id,
            stages=stages,
            include_constant_buffers=include_constant_buffers,
            include_source=include_source,
            variable_limit=variable_limit,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    payload = result["result"]
    summary = payload.get("summary") or {}
    return skill_success(
        "Draw {} ({}): {} shader stage(s) bound, {} texture binding(s), "
        "{} read-write binding(s), {} vertex buffer(s).".format(
            event_id,
            summary.get("name") or "unnamed",
            summary.get("shader_count", 0),
            summary.get("texture_binding_count", 0),
            summary.get("read_write_binding_count", 0),
            summary.get("vertex_buffer_count", 0),
        ),
        **result,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
