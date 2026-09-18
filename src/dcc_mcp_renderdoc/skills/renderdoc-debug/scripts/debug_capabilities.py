from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import debug_capabilities


@skill_entry
def main(capture_file: Optional[str] = None, **_kwargs):
    report = debug_capabilities(capture_file)
    deep = report["deep"]
    if deep["available"]:
        summary = "RenderDoc deep replay available; debug tools are enabled."
    else:
        summary = "RenderDoc deep replay unavailable: {}".format(deep["reason"])
    return skill_success(summary, **report)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
