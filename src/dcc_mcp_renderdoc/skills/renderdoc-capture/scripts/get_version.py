from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.runtime import get_version


@skill_entry
def main(**_kwargs):
    return skill_success("RenderDoc CLI is available.", **get_version())


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
