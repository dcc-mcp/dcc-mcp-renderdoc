from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.runtime import export_drawcall_resources


@skill_entry
def main(capture_file: str, event_id: int, output_dir: str, **_kwargs):
    result = export_drawcall_resources(capture_file, event_id, output_dir)
    return skill_success("RenderDoc drawcall resources exported.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
