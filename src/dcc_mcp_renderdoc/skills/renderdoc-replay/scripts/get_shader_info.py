from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(
    capture_file: str,
    event_id: Optional[int] = None,
    stage: str = "Pixel",
    include_source: bool = False,
    include_disassembly: bool = False,
    disassembly_target: str = "",
    **_kwargs,
):
    result = run_replay_operation(
        capture_file,
        "get_shader_info",
        clean_params(
            event_id=event_id,
            stage=stage,
            include_source=include_source,
            include_disassembly=include_disassembly,
            disassembly_target=disassembly_target,
        ),
    )
    return skill_success("RenderDoc shader described.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
