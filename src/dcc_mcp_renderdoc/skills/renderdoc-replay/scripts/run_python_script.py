from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(
    capture_file: str,
    source: str,
    event_id: Optional[int] = None,
    args: Optional[dict] = None,
    **_kwargs,
):
    result = run_replay_operation(
        capture_file,
        "run_python_script",
        clean_params(source=source, event_id=event_id, args=args),
    )
    return skill_success("RenderDoc Python script executed.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
