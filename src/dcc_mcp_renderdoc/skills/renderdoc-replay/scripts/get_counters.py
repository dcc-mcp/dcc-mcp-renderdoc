from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_replay_operation


@skill_entry
def main(
    capture_file: str,
    event_id: Optional[int] = None,
    fetch: bool = False,
    counter_ids: Optional[list] = None,
    limit: int = 200,
    **_kwargs,
):
    result = run_replay_operation(
        capture_file,
        "get_counters",
        clean_params(event_id=event_id, fetch=fetch, counter_ids=counter_ids, limit=limit),
    )
    return skill_success("RenderDoc GPU counters listed.", **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
