from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_debug_operation


@skill_entry
def main(
    capture_file: str,
    resource_id: int,
    x: int,
    y: int,
    event_id: Optional[int] = None,
    limit: Optional[int] = None,
    mip: Optional[int] = None,
    slice_index: Optional[int] = None,
    sample: Optional[int] = None,
    type_cast: Optional[str] = None,
    **_kwargs,
):
    result = run_debug_operation(
        capture_file,
        "get_pixel_history",
        clean_params(
            resource_id=resource_id,
            x=x,
            y=y,
            event_id=event_id,
            limit=limit,
            mip=mip,
            slice=slice_index,
            sample=sample,
            type_cast=type_cast,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    return skill_success(
        "RenderDoc read the history of pixel ({}, {}) of resource {}.".format(x, y, resource_id),
        **result,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
