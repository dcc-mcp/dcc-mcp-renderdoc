from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(
    capture_file: str,
    resource_id: int,
    output_file: str,
    mip: int = 0,
    slice_index: int = 0,
    type_cast: str = "Typeless",
    **_kwargs,
):
    result = run_replay_operation(
        capture_file,
        "get_texture_data",
        clean_params(
            resource_id=resource_id,
            output_file=output_file,
            mip=mip,
            slice=slice_index,
            type_cast=type_cast,
        ),
    )
    return skill_success("RenderDoc texture exported.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
