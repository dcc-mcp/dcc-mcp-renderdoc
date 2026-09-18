from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_debug_operation


@skill_entry
def main(
    capture_file: str,
    x: int,
    y: int,
    event_id: Optional[int] = None,
    sample: Optional[int] = None,
    primitive: Optional[int] = None,
    view: Optional[int] = None,
    max_steps: Optional[int] = None,
    detail: Optional[str] = None,
    **_kwargs,
):
    result = run_debug_operation(
        capture_file,
        "debug_pixel",
        clean_params(
            x=x,
            y=y,
            event_id=event_id,
            sample=sample,
            primitive=primitive,
            view=view,
            max_steps=max_steps,
            detail=detail,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    return skill_success(
        "RenderDoc stepped the pixel shader that shaded ({}, {}).".format(x, y),
        **result,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
