from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(capture_file: str, resource_id: int, limit: int = 200, **_kwargs):
    result = run_replay_operation(
        capture_file,
        "get_resource_usage",
        clean_params(resource_id=resource_id, limit=limit),
    )
    return skill_success("RenderDoc resource usage listed.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
