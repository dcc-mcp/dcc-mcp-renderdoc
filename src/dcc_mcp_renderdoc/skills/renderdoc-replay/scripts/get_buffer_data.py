from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(
    capture_file: str,
    resource_id: int,
    offset: int = 0,
    length: int = 0,
    preview_bytes: int = 256,
    output_file: str = "",
    **_kwargs,
):
    result = run_replay_operation(
        capture_file,
        "get_buffer_data",
        clean_params(
            resource_id=resource_id,
            offset=offset,
            length=length,
            preview_bytes=preview_bytes,
            output_file=output_file or None,
        ),
    )
    return skill_success("RenderDoc buffer read.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
