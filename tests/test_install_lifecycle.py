from __future__ import annotations

import json
from subprocess import CompletedProcess


def test_doctor_json_reports_preflight_with_stable_exit(
    monkeypatch,
    capsys,
    tmp_path,
):
    from dcc_mcp_renderdoc import cli

    monkeypatch.setenv("DCC_MCP_RENDERDOC_CMD", str(tmp_path / "missing-renderdoccmd"))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_AUTO_DOWNLOAD", "0")

    exit_code = cli.run(["doctor", "--json"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 10
    assert report["schema_version"] == 1
    assert report["operation"] == "doctor"
    assert report["directly_usable"] is False
    assert report["exit_code"] == 10
    assert report["versions"]["core"]["minimum"]
    assert report["versions"]["renderdoc"]["minimum"]
    assert report["next_steps"]
    assert set(report["next_steps"][0]) == {
        "name",
        "description",
        "url",
        "command",
        "requires_live_instance",
    }
    assert any(
        step["command"] == ["dcc-mcp-renderdoc", "verify", "--json"]
        for step in report["next_steps"]
    )


def test_verify_json_reports_ready_runtime_and_version_floors(
    monkeypatch,
    capsys,
    tmp_path,
):
    from dcc_mcp_renderdoc import cli, diagnostics

    command = tmp_path / "renderdoccmd"
    command.touch()
    command.with_name("qrenderdoc").touch()
    monkeypatch.setenv("DCC_MCP_RENDERDOC_CMD", str(command))
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(diagnostics.importlib.metadata, "version", lambda _name: "0.19.45")
    monkeypatch.setattr(
        diagnostics.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, "renderdoccmd x64 v1.45", ""),
    )

    exit_code = cli.run(["verify", "--json"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["directly_usable"] is True
    assert report["exit_code"] == 0
    assert report["versions"]["core"]["ok"] is True
    assert report["versions"]["renderdoc"]["ok"] is True
    assert all(item["ok"] for item in report["prerequisites"])


def test_verify_json_uses_verify_failure_exit(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli

    monkeypatch.setenv("DCC_MCP_RENDERDOC_CMD", str(tmp_path / "missing-renderdoccmd"))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_AUTO_DOWNLOAD", "0")

    assert cli.run(["verify", "--json"]) == 40
    assert json.loads(capsys.readouterr().out)["exit_code"] == 40
