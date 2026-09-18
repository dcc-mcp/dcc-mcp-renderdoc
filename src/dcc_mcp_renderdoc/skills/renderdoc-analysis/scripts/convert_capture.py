from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.runtime import convert_capture


@skill_entry
def main(capture_file: str, output_file: str, convert_format: str = "xml", **_kwargs):
    result = convert_capture(capture_file, output_file, convert_format=convert_format)
    return skill_success("RenderDoc capture converted.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
