from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(capture_file: str, **_kwargs):
    result = run_replay_operation(
        capture_file,
        "describe_capture",
        clean_params(),
    )
    return skill_success("RenderDoc capture described.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
