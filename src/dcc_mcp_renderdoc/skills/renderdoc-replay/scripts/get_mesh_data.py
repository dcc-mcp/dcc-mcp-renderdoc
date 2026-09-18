from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(
    capture_file: str,
    event_id: Optional[int] = None,
    stage: str = "VSOut",
    instance: int = 0,
    view: int = 0,
    max_vertices: int = 256,
    **_kwargs,
):
    result = run_replay_operation(
        capture_file,
        "get_mesh_data",
        clean_params(
            event_id=event_id, stage=stage, instance=instance, view=view, max_vertices=max_vertices
        ),
    )
    return skill_success("RenderDoc mesh data read.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
