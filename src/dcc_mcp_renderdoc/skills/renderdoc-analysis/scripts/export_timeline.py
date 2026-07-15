from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.runtime import export_timeline


@skill_entry
def main(capture_file: str, output_file: str, **_kwargs):
    result = export_timeline(capture_file, output_file)
    return skill_success("RenderDoc Chrome timeline exported.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
