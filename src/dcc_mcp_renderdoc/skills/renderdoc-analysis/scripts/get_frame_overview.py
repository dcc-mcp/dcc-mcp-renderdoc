from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_analysis_operation


@skill_entry
def main(
    capture_file: str,
    max_passes: Optional[int] = None,
    max_textures: Optional[int] = None,
    max_actions_per_pass: Optional[int] = None,
    max_messages: Optional[int] = None,
    include_debug_messages: bool = True,
    **_kwargs,
):
    result = run_analysis_operation(
        capture_file,
        "get_frame_overview",
        clean_params(
            max_passes=max_passes,
            max_textures=max_textures,
            max_actions_per_pass=max_actions_per_pass,
            max_messages=max_messages,
            include_debug_messages=include_debug_messages,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    payload = result["result"]
    actions = payload.get("actions") or {}
    text = (
        "Frame overview: {} action(s) ({} draw, {} dispatch) across {} pass(es); {} signal(s)."
    ).format(
        actions.get("action_count", 0),
        actions.get("draw_count", 0),
        actions.get("dispatch_count", 0),
        payload.get("pass_count", 0),
        len(payload.get("signals") or []),
    )
    # Triangle counts are derived from API draw parameters and the signals are
    # thresholds over structure, so both are stated here as well as in
    # estimate_fields: the payload is what reaches the agent, SKILL.md is not.
    text += (
        " Pass triangle counts are CPU-derived estimates (numIndices / 3 * "
        "numInstances, before GPU culling); signals are heuristics, not measurements."
    )
    return skill_success(text, **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
