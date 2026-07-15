from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.runtime import capture_process


@skill_entry
def main(
    process_id: int,
    output_template: str,
    working_directory=None,
    trigger_after_secs: float = 2.0,
    capture_wait_secs: int = 30,
    api_validation: bool = False,
    timeout_secs: int = 60,
    **_kwargs,
):
    result = capture_process(
        process_id,
        output_template,
        working_directory=working_directory,
        trigger_after_secs=trigger_after_secs,
        capture_wait_secs=capture_wait_secs,
        api_validation=api_validation,
        timeout_secs=timeout_secs,
    )
    return skill_success(f"Created {len(result['captures'])} RenderDoc capture(s).", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
