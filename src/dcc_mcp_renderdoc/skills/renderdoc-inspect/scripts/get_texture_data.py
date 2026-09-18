from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(
    capture_file: str,
    resource_id: int,
    output_file: Optional[str] = None,
    include_pixels: bool = False,
    mip: int = 0,
    slice_index: int = 0,
    type_cast: str = "Typeless",
    preview_bytes: Optional[int] = None,
    **_kwargs,
):
    result = run_replay_operation(
        capture_file,
        "get_texture_data",
        clean_params(
            resource_id=resource_id,
            output_file=output_file,
            include_pixels=include_pixels,
            mip=mip,
            slice=slice_index,
            type_cast=type_cast,
            preview_bytes=preview_bytes,
        ),
    )
    return skill_success("RenderDoc texture read.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
