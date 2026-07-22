"""RenderDoc 1.45 bundled-Python bridge for headless Target Control capture."""

import json
import os
import time


def _positive_int(name):
    value = int(os.environ[name])
    if value <= 0:
        raise ValueError("{} must be positive".format(name))
    return value


def _open_target(rd, ident):
    target = rd.CreateTargetControl("", ident, "dcc-mcp-renderdoc", False)
    if target is None:
        raise RuntimeError("RenderDoc target ident {} is busy or unavailable".format(ident))
    if not target.Connected():
        busy_client = target.GetBusyClient()
        detail = " (busy client: {})".format(busy_client) if busy_client else ""
        target.Shutdown()
        raise RuntimeError("could not connect to RenderDoc target ident {}{}".format(ident, detail))
    return target


def _target_name_matches(actual, expected):
    actual = os.path.basename(actual).casefold()
    expected = os.path.basename(expected).casefold()
    return (
        actual == expected
        or (actual.endswith(".exe") and actual[:-4] == expected)
        or (expected.endswith(".exe") and expected[:-4] == actual)
    )


def _find_named_target(rd, target_name, deadline):
    while time.monotonic() < deadline:
        cursor = 0
        seen = set()
        matches = []
        while True:
            ident = int(rd.EnumerateRemoteTargets("", cursor))
            if ident == 0 or ident in seen:
                break
            seen.add(ident)
            cursor = ident
            candidate = None
            try:
                candidate = rd.CreateTargetControl("", ident, "dcc-mcp-renderdoc", False)
                if candidate is None or not candidate.Connected():
                    continue
                if _target_name_matches(str(candidate.GetTarget()), target_name):
                    matches.append(candidate)
                    candidate = None
            finally:
                if candidate is not None:
                    candidate.Shutdown()
        if len(matches) == 1:
            return matches[0]
        for match in matches:
            match.Shutdown()
        if len(matches) > 1:
            raise RuntimeError("multiple RenderDoc targets matched {}".format(target_name))
        time.sleep(0.05)
    raise RuntimeError("no RenderDoc target matched {}".format(target_name))


def main():
    status_path = os.environ.get("DCC_MCP_RENDERDOC_TARGET_STATUS")
    status = {
        "schema_version": 1,
        "connected": False,
        "triggered": False,
        "shutdown": False,
        "timed_out": False,
        "target_pid": None,
        "capture_path": None,
        "error": None,
    }
    target = None
    try:
        import renderdoc as rd

        ident = _positive_int("DCC_MCP_RENDERDOC_TARGET_IDENT")
        timeout_secs = _positive_int("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS")
        target_name = os.environ.get("DCC_MCP_RENDERDOC_TARGET_NAME", "").strip()
        deadline = time.monotonic() + timeout_secs
        target = (
            _find_named_target(rd, target_name, deadline)
            if target_name
            else _open_target(rd, ident)
        )
        status["connected"] = True
        status["target_pid"] = int(target.GetPID())
        target.TriggerCapture(1)
        status["triggered"] = True
        while time.monotonic() < deadline:
            message = target.ReceiveMessage(None)
            if message is not None and message.type == rd.TargetControlMessageType.NewCapture:
                status["capture_path"] = str(message.newCapture.path)
                break
            if message is not None and message.type == rd.TargetControlMessageType.Disconnected:
                raise RuntimeError("RenderDoc Target Control disconnected before capture")
            time.sleep(0.05)
        if status["capture_path"] is None:
            status["timed_out"] = True
            raise RuntimeError("timed out waiting for RenderDoc NewCapture")
    except BaseException as exc:
        status["error"] = str(exc)
    finally:
        if target is not None:
            try:
                target.Shutdown()
                status["shutdown"] = True
                target = None
            except BaseException as exc:
                status["error"] = status["error"] or ("Target Control shutdown failed: " + str(exc))
        if status_path:
            with open(status_path, "w") as status_file:
                json.dump(status, status_file)


try:
    main()
except BaseException:
    pass
raise SystemExit(0)
