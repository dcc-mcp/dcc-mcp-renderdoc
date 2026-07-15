from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.runtime import inspect_capture


@skill_entry
def main(capture_file: str, representative_limit: int = 20, **_kwargs):
    result = inspect_capture(capture_file, representative_limit=representative_limit)
    return skill_success("RenderDoc capture inspected.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
