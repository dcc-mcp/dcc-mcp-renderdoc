from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(
    capture_file: str,
    parent_event_id: Optional[int] = None,
    max_depth: int = 32,
    name_filter: str = "",
    flag_filter: str = "",
    offset: int = 0,
    limit: int = 200,
    **_kwargs,
):
    result = run_replay_operation(
        capture_file,
        "list_actions",
        clean_params(
            parent_event_id=parent_event_id,
            max_depth=max_depth,
            name_filter=name_filter,
            flag_filter=flag_filter,
            offset=offset,
            limit=limit,
        ),
    )
    return skill_success("RenderDoc actions listed.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
