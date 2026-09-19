from typing import Optional

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_renderdoc.replay import clean_params, run_analysis_operation


@skill_entry
def main(
    capture_file: str,
    resource_id: int,
    x: Optional[int] = None,
    y: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    checks: Optional[list] = None,
    mip: int = 0,
    slice_index: int = 0,
    sample: int = 0,
    event_id: Optional[int] = None,
    max_anomalies: Optional[int] = None,
    max_texels: Optional[int] = None,
    **_kwargs,
):
    result = run_analysis_operation(
        capture_file,
        "diagnose_pixel_values",
        clean_params(
            resource_id=resource_id,
            x=x,
            y=y,
            width=width,
            height=height,
            checks=checks,
            mip=mip,
            slice=slice_index,
            sample=sample,
            event_id=event_id,
            max_anomalies=max_anomalies,
            max_texels=max_texels,
        ),
    )
    if result.get("supported") is False:
        return skill_error(
            result["error_message"], "unsupported_backend", prompt=result["hint"], **result
        )
    payload = result["result"]
    if payload.get("supported") is False:
        return skill_error(
            payload["error_message"],
            "unsupported_capture",
            prompt=payload["hint"],
            hint=payload["hint"],
            **result,
        )
    findings = []
    for name, check in sorted((payload.get("checks") or {}).items()):
        if not check.get("applicable"):
            findings.append("{}: not applicable ({})".format(name, check.get("reason")))
        else:
            findings.append("{}: {}".format(name, check.get("count", 0)))
    text = "Scanned {} texel(s) of {}; {} anomaly texel(s). {}".format(
        payload.get("scanned_texels", 0),
        payload.get("resource_name") or "resource {}".format(payload.get("resource_id")),
        payload.get("anomaly_texel_count", 0),
        "; ".join(findings),
    )
    if payload.get("estimate"):
        # The stride belongs in the agent's context: the counts below are a
        # sample of the region, not a census of it.
        text += " Estimated by strided sampling ({}).".format(payload.get("estimate_method"))
    return skill_success(text, **result)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
