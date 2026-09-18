from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(
    capture_file: str,
    x: int,
    y: int,
    event_id: Optional[int] = None,
    sample: int = 0,
    primitive: int = 0,
    view: int = 0,
    max_steps: int = 200,
    **_kwargs,
):
    result = run_replay_operation(
        capture_file,
        "debug_pixel",
        clean_params(
            x=x,
            y=y,
            event_id=event_id,
            sample=sample,
            primitive=primitive,
            view=view,
            max_steps=max_steps,
        ),
    )
    return skill_success("RenderDoc pixel shader debug trace read.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
