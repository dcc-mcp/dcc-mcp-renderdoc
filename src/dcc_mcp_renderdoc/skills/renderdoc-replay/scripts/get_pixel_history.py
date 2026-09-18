from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(
    capture_file: str,
    resource_id: int,
    x: int,
    y: int,
    event_id: Optional[int] = None,
    mip: int = 0,
    slice_index: int = 0,
    sample: int = 0,
    type_cast: str = "Typeless",
    limit: int = 200,
    **_kwargs,
):
    result = run_replay_operation(
        capture_file,
        "get_pixel_history",
        clean_params(
            resource_id=resource_id,
            x=x,
            y=y,
            event_id=event_id,
            mip=mip,
            slice=slice_index,
            sample=sample,
            type_cast=type_cast,
            limit=limit,
        ),
    )
    return skill_success("RenderDoc pixel history read.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
