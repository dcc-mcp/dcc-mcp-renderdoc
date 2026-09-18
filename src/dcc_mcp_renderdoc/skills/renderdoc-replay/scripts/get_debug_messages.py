from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(capture_file: str, limit: int = 200, **_kwargs):
    result = run_replay_operation(
        capture_file,
        "get_debug_messages",
        clean_params(limit=limit),
    )
    return skill_success("RenderDoc debug messages listed.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
