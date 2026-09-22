from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.assertions import assert_state


@skill_entry
def main(
    capture_file: str,
    event_id_a: Optional[int] = None,
    event_id_b: Optional[int] = None,
    other_capture_file: Optional[str] = None,
    match_by: str = "event_id",
    event_id: Optional[int] = None,
    event_index: Optional[int] = None,
    event_name: Optional[str] = None,
    ignore_keys: Optional[list] = None,
    **_kwargs,
):
    result = assert_state(
        capture_file,
        other_capture_file=other_capture_file,
        event_id_a=event_id_a,
        event_id_b=event_id_b,
        match_by=match_by,
        event_id=event_id,
        event_index=event_index,
        event_name=event_name,
        ignore_keys=ignore_keys,
    )
    payload = result["result"]
    if payload.get("supported") is False:
        # A missing deep backend is a different fix from a capture that will not
        # replay, so the two are reported under different error kinds.
        if payload.get("capability_group"):
            return skill_error(
                payload["error_message"], "unsupported_backend", prompt=payload["hint"], **result
            )
        return skill_error(
            payload["error_message"],
            "unsupported_capture",
            prompt=payload["hint"],
            hint=payload["hint"],
            **result,
        )
    events = payload.get("events") or [{}, {}]
    text = "assert_state {}: events {} and {} differ in {} of {} checked key(s).".format(
        "PASSED" if payload.get("passed") else "FAILED",
        events[0].get("event_id"),
        events[1].get("event_id"),
        payload.get("difference_count", 0),
        len(payload.get("compared_keys") or []),
    )
    for difference in (payload.get("differences") or [])[:10]:
        text += " {}: {} -> {}.".format(
            difference["key"],
            _render(difference.get("before")),
            _render(difference.get("after")),
        )
    if payload.get("difference_count", 0) > 10:
        text += " ({} more in differences).".format(payload["difference_count"] - 10)
    if payload.get("ignored_keys"):
        text += " Ignored: {}.".format(", ".join(payload["ignored_keys"]))
    if payload.get("unknown_ignore_keys"):
        # Naming a key that is not in the fingerprint is usually a typo, and
        # silently ignoring it would read as "this key was excluded".
        text += " Not found in the fingerprint, so ignored nothing: {}.".format(
            ", ".join(payload["unknown_ignore_keys"])
        )
    return skill_success(text, **result)


def _render(value):
    """One fingerprint value as prose, truncated so a large list stays readable."""
    if isinstance(value, list):
        return (
            "[" + ", ".join(str(item) for item in value[:8]) + (", ...]" if len(value) > 8 else "]")
        )
    return str(value)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
