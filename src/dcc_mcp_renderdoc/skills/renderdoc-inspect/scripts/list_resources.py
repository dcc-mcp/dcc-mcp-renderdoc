from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(
    capture_file: str,
    resource_type: str = "",
    name_filter: str = "",
    offset: int = 0,
    limit: int = 200,
    **_kwargs,
):
    result = run_replay_operation(
        capture_file,
        "list_resources",
        clean_params(
            resource_type=resource_type, name_filter=name_filter, offset=offset, limit=limit
        ),
    )
    return skill_success("RenderDoc resources listed.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
