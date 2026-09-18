from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import describe_capabilities


@skill_entry
def main(**_kwargs):
    status = describe_capabilities()
    summary = (
        "RenderDoc deep replay available."
        if status["deep"]["available"]
        else ("RenderDoc deep replay unavailable: {}".format(status["deep"]["reason"]))
    )
    return skill_success(summary, **status)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
