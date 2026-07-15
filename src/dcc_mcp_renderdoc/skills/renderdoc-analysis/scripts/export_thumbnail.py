from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.runtime import export_thumbnail


@skill_entry
def main(capture_file: str, output_file: str, max_size: int = 0, **_kwargs):
    result = export_thumbnail(capture_file, output_file, max_size=max_size)
    return skill_success("RenderDoc thumbnail exported.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
