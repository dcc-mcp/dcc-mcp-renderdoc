"""RenderDoc 1.45 bundled-Python bridge for headless Target Control capture."""

import json
import os
import time


def _positive_int(name):
    value = int(os.environ[name])
    if value <= 0:
        raise ValueError("{} must be positive".format(name))
    return value


def _nonnegative_float(name):
    value = float(os.environ[name])
    if value < 0:
        raise ValueError("{} must not be negative".format(name))
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
    actual = str(actual).replace("\\", "/").rsplit("/", 1)[-1].casefold()
    expected = str(expected).replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return (
        actual == expected
        or (actual.endswith(".exe") and actual[:-4] == expected)
        or (expected.endswith(".exe") and expected[:-4] == actual)
    )


def _wait_for_child_target(rd, parent, target_name, trigger_at, deadline):
    matches = []
    quiet_deadline = None
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if (
                len(matches) == 1
                and quiet_deadline is not None
                and now >= trigger_at
                and now >= quiet_deadline
            ):
                return matches.pop()
            message = parent.ReceiveMessage(None)
            if message is not None and message.type == rd.TargetControlMessageType.NewChild:
                child_ident = int(message.newChild.ident)
                child_pid = int(message.newChild.processId)
                child = _open_target(rd, child_ident)
                try:
                    if int(child.GetPID()) != child_pid:
                        raise RuntimeError(
                            "RenderDoc child target PID did not match NewChild message"
                        )
                    if _target_name_matches(child.GetTarget(), target_name):
                        matches.append(child)
                        child = None
                finally:
                    if child is not None:
                        child.Shutdown()
                if matches:
                    if len(matches) > 1:
                        raise RuntimeError("multiple child targets matched {}".format(target_name))
                    quiet_deadline = time.monotonic() + 0.25
                    if quiet_deadline > deadline:
                        raise RuntimeError("insufficient time to select a unique child target")
                continue
            if message is not None and message.type == rd.TargetControlMessageType.Disconnected:
                if len(matches) == 1:
                    return matches.pop()
                raise RuntimeError("RenderDoc parent Target Control disconnected before child")
            for match in matches:
                child_message = match.ReceiveMessage(None)
                if (
                    child_message is not None
                    and child_message.type == rd.TargetControlMessageType.Disconnected
                ):
                    raise RuntimeError("RenderDoc child Target Control disconnected before trigger")
            time.sleep(0.05)
        if len(matches) == 1:
            raise RuntimeError("insufficient time to select a unique child target")
        raise RuntimeError("no child target matched {}".format(target_name))
    finally:
        for match in matches:
            match.Shutdown()


def _pump_until(rd, target, deadline):
    while time.monotonic() < deadline:
        message = target.ReceiveMessage(None)
        if message is not None and message.type == rd.TargetControlMessageType.Disconnected:
            raise RuntimeError("RenderDoc Target Control disconnected before trigger")
        time.sleep(0.05)


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
        capture_wait_secs = _positive_int("DCC_MCP_RENDERDOC_TARGET_TIMEOUT_SECS")
        trigger_after_secs = _nonnegative_float("DCC_MCP_RENDERDOC_TRIGGER_AFTER_SECS")
        target_name = os.environ.get("DCC_MCP_RENDERDOC_TARGET_NAME", "").strip()
        trigger_at = time.monotonic() + trigger_after_secs
        capture_deadline = trigger_at + capture_wait_secs
        target = _open_target(rd, ident)
        if target_name and not _target_name_matches(target.GetTarget(), target_name):
            child = _wait_for_child_target(rd, target, target_name, trigger_at, capture_deadline)
            try:
                target.Shutdown()
            except BaseException:
                child.Shutdown()
                raise
            target = child
        status["connected"] = True
        status["target_pid"] = int(target.GetPID())
        _pump_until(rd, target, trigger_at)
        target.TriggerCapture(1)
        status["triggered"] = True
        while time.monotonic() < capture_deadline:
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


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    raise SystemExit(0)
