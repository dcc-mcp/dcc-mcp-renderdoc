from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.runtime import capture_program


@skill_entry
def main(
    executable: str,
    output_template: str,
    arguments=None,
    working_directory=None,
    wait_for_exit: bool = True,
    api_validation: bool = False,
    hook_children: bool = False,
    timeout_secs: int = 300,
    **_kwargs,
):
    result = capture_program(
        executable,
        output_template,
        arguments=arguments,
        working_directory=working_directory,
        wait_for_exit=wait_for_exit,
        api_validation=api_validation,
        hook_children=hook_children,
        timeout_secs=timeout_secs,
    )
    return skill_success(f"Created {len(result['captures'])} RenderDoc capture(s).", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
