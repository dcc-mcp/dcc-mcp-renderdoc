from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_debug_operation


@skill_entry
def main(
    capture_file: str,
    output_file: str,
    event_id: Optional[int] = None,
    stage: Optional[str] = None,
    instance: Optional[int] = None,
    view: Optional[int] = None,
    max_vertices: Optional[int] = None,
    preview_vertices: Optional[int] = None,
    **_kwargs,
):
    result = run_debug_operation(
        capture_file,
        "export_mesh",
        clean_params(
            output_file=output_file,
            event_id=event_id,
            stage=stage,
            instance=instance,
            view=view,
            max_vertices=max_vertices,
            preview_vertices=preview_vertices,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    return skill_success("RenderDoc exported the post-VS mesh.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
