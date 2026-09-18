from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(capture_file: str, event_id: int, **_kwargs):
    result = run_replay_operation(
        capture_file,
        "get_pipeline_state",
        clean_params(event_id=event_id),
    )
    return skill_success("RenderDoc pipeline state described.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
