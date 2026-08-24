from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


def _validate_install_result(report):
    from dcc_mcp_renderdoc.install_contract import load_install_sop_schema

    schema = load_install_sop_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(report)


def _select_linux_managed_bundle(monkeypatch, lifecycle):
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")


def test_compatibility_schema_matches_core_2320_canonical_contract():
    from dcc_mcp_renderdoc.install_contract import load_install_sop_schema

    canonical = json.dumps(
        load_install_sop_schema(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert hashlib.sha256(canonical).hexdigest() == (
        "c0bcb6f8fa7228d43a4b4c6d20d7e4618a21c09eb6b2d3c2018ee56f647fccc1"
    )


def test_current_target_python_probe_reuses_proven_import_context(monkeypatch):
    from dcc_mcp_renderdoc import lifecycle

    imported = []
    monkeypatch.setattr(
        lifecycle.importlib,
        "import_module",
        lambda name: imported.append(name),
    )
    monkeypatch.setattr(
        lifecycle.importlib.metadata,
        "version",
        lambda name: lifecycle.__version__ if name == "dcc-mcp-renderdoc" else "0.20.0",
    )
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("current interpreter must not spawn a redundant probe")
        ),
    )

    result = lifecycle._probe_python(Path(sys.executable))

    assert result["python"] == str(Path(sys.executable).resolve())
    assert result["core_version"] == "0.20.0"
    assert result["resolution_source"] == "argument"
    assert imported == ["dcc_mcp_renderdoc", "dcc_mcp_core"]


@pytest.mark.parametrize("adapter_version", [None, "0.0.1"])
def test_cross_target_python_rejects_missing_or_mismatched_adapter(
    monkeypatch, tmp_path, adapter_version
):
    from dcc_mcp_renderdoc import lifecycle

    python = tmp_path / "target-python.exe"
    python.touch()
    payload = {
        "python": str(python),
        "python_version": "3.12.0",
        "core_version": "0.20.0",
    }
    if adapter_version is not None:
        payload["adapter_version"] = adapter_version
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *args, **_kwargs: lifecycle.subprocess.CompletedProcess(
            args, 0, json.dumps(payload), ""
        ),
    )

    with pytest.raises(lifecycle.LifecycleError) as caught:
        lifecycle._probe_python(python)

    assert caught.value.exit_code == 10
    assert caught.value.reason == "adapter_version_mismatch"


def test_cross_target_python_accepts_exact_adapter(monkeypatch, tmp_path):
    from dcc_mcp_renderdoc import lifecycle

    python = tmp_path / "target-python.exe"
    python.touch()
    payload = {
        "python": str(python),
        "python_version": "3.12.0",
        "adapter_version": lifecycle.__version__,
        "core_version": "0.20.0",
    }
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *args, **_kwargs: lifecycle.subprocess.CompletedProcess(
            args, 0, json.dumps(payload), ""
        ),
    )

    result = lifecycle._probe_python(python)

    assert result["adapter_version"] == lifecycle.__version__
    assert result["resolution_source"] == "argument"


def test_current_target_python_rejects_metadata_version_mismatch(monkeypatch):
    from dcc_mcp_renderdoc import lifecycle

    monkeypatch.setattr(lifecycle.importlib, "import_module", lambda _name: None)
    monkeypatch.setattr(
        lifecycle.importlib.metadata,
        "version",
        lambda name: "0.0.1" if name == "dcc-mcp-renderdoc" else "0.20.0",
    )

    with pytest.raises(lifecycle.LifecycleError) as caught:
        lifecycle._probe_python(Path(sys.executable))

    assert caught.value.exit_code == 10
    assert caught.value.reason == "adapter_version_mismatch"


def test_cross_target_python_timeout_is_stable_install_json(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli, lifecycle

    python = tmp_path / "target-python.exe"
    python.touch()

    def timeout(*_args, **_kwargs):
        raise lifecycle.subprocess.TimeoutExpired([str(python)], 20)

    monkeypatch.setattr(lifecycle.subprocess, "run", timeout)

    exit_code = cli.run(["install", "--python", str(python), "--json", "--dry-run"])
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 10
    assert report["verify"]["failure_reason"] == "python_probe_failed"


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

    _validate_install_result(report)

    assert exit_code == 10
    assert report["schema_version"] == 1
    assert report["operation"] == "doctor"
    assert report["directly_usable"] is False
    assert report["exit_code"] == 10
    assert report["versions"]["core"]["minimum"]
    assert report["versions"]["renderdoc"]["minimum"]
    assert report["next_steps"]
    assert {"id", "description", "why", "command"} <= set(report["next_steps"][0])
    assert any(
        step["command"] == ["dcc-mcp-renderdoc", "verify", "--json"]
        for step in report["next_steps"]
    )


def test_doctor_never_accepts_empty_executable_pair(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli

    command = tmp_path / "renderdoccmd.exe"
    command.write_bytes(b"")
    command.with_name("qrenderdoc.exe").write_bytes(b"")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_CMD", str(command))

    assert cli.run(["doctor", "--json"]) == 10
    report = json.loads(capsys.readouterr().out)
    _validate_install_result(report)
    assert report["directly_usable"] is False
    assert report["config"]["runtime_probe"] is None
    assert (
        next(step for step in report["prerequisites"] if step["id"] == "qrenderdoc")["ok"] is False
    )


def test_doctor_version_only_pair_requires_embedded_python_marker(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli, diagnostics

    command = tmp_path / "renderdoccmd.exe"
    command.write_bytes(b"renderdoc")
    command.with_name("qrenderdoc.exe").write_bytes(b"qrenderdoc")
    monkeypatch.setattr(diagnostics.sys, "platform", "win32")
    monkeypatch.setattr(diagnostics, "_core_version", lambda: "0.20.0")
    monkeypatch.setattr(
        diagnostics,
        "probe_runtime",
        lambda *_args, **_kwargs: {
            "renderdoccmd_version": "1.45",
            "qrenderdoc_version": "1.45",
        },
    )

    def fail_embedded_probe(_qrenderdoc):
        raise RuntimeError("injected embedded-Python load failure")

    monkeypatch.setattr(diagnostics, "probe_qrenderdoc_python", fail_embedded_probe)

    exit_code = cli.run(["doctor", "--command", str(command), "--json"])
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 10
    assert report["directly_usable"] is False
    assert report["verify"]["directly_usable"] is False
    qrenderdoc = next(step for step in report["prerequisites"] if step["id"] == "qrenderdoc")
    assert qrenderdoc["ok"] is False
    assert qrenderdoc["probe_error"] == "embedded_python_probe_failed"


def test_verify_json_requires_a_receipt_before_direct_usability(
    monkeypatch,
    capsys,
    tmp_path,
):
    from dcc_mcp_renderdoc import cli

    command = tmp_path / "renderdoccmd"
    command.touch()
    command.with_name("qrenderdoc").touch()
    exit_code = cli.run(
        [
            "verify",
            "--dcc-path",
            str(command),
            "--receipt-path",
            str(tmp_path / "missing-receipt.json"),
            "--json",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 40
    assert report["verify"]["directly_usable"] is False
    assert report["verify"]["failure_stage"] == "receipt"


def test_verify_json_uses_verify_failure_exit(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli

    monkeypatch.setenv("DCC_MCP_RENDERDOC_CMD", str(tmp_path / "missing-renderdoccmd"))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_AUTO_DOWNLOAD", "0")

    assert cli.run(["verify", "--json"]) == 40
    assert json.loads(capsys.readouterr().out)["exit_code"] == 40


def test_install_dry_run_uses_standard_surface_and_does_not_write_receipt(
    monkeypatch,
    capsys,
    tmp_path,
):
    from dcc_mcp_renderdoc import cli, lifecycle

    command = tmp_path / "renderdoccmd.exe"
    command.touch()
    command.with_name("qrenderdoc.exe").touch()
    receipt = tmp_path / "receipts" / "renderdoc.json"
    monkeypatch.setattr(
        lifecycle,
        "probe_runtime",
        lambda *_args, **_kwargs: {
            "renderdoccmd_version": "1.45",
            "qrenderdoc_version": "1.45",
        },
    )

    exit_code = cli.run(
        [
            "install",
            "--dcc-path",
            str(command),
            "--python",
            sys.executable,
            "--receipt-path",
            str(receipt),
            "--json",
            "--dry-run",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 0
    assert report["status"] == "planned"
    assert report["dcc_type"] == "renderdoc"
    assert report["verify"]["directly_usable"] is False
    assert report["verify"]["failure_reason"] == "receipt_not_committed"
    assert report["receipt_path"] == str(receipt.resolve())
    assert report["next_steps"][0]["command"][:2] == ["dcc-mcp-renderdoc", "install"]
    assert "--yes" in report["next_steps"][0]["command"]
    assert not receipt.exists()


def test_managed_install_writes_digest_receipt_and_converges(
    monkeypatch,
    capsys,
    tmp_path,
):
    from dcc_mcp_renderdoc import cli, lifecycle

    _select_linux_managed_bundle(monkeypatch, lifecycle)

    destination = tmp_path / "cache" / "renderdoc" / "1.45-dddddddddddd"
    command = destination / "bin" / "renderdoccmd.exe"
    qrenderdoc = command.with_name("qrenderdoc.exe")
    command.parent.mkdir(parents=True)
    command.write_bytes(b"renderdoc")
    qrenderdoc.write_bytes(b"qrenderdoc")
    files = {
        "bin/qrenderdoc.exe": __import__("hashlib").sha256(b"qrenderdoc").hexdigest(),
        "bin/renderdoccmd.exe": __import__("hashlib").sha256(b"renderdoc").hexdigest(),
    }
    (destination / ".dcc-mcp-renderdoc.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "1.45",
                "url": "https://renderdoc.org/stable/1.45/RenderDoc_1.45_64.zip",
                "sha256": "d" * 64,
                "command": "bin/renderdoccmd.exe",
                "qrenderdoc": "bin/qrenderdoc.exe",
                "files": files,
                "probe": {"qrenderdoc_python_probe": "loaded"},
            }
        ),
        encoding="utf-8",
    )
    receipt = tmp_path / "receipts" / "renderdoc.json"
    monkeypatch.setenv("DCC_MCP_RUNTIME_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(lifecycle, "download_pinned", lambda *_args: command)
    monkeypatch.setattr(
        lifecycle,
        "probe_runtime",
        lambda *_args, **_kwargs: {
            "renderdoccmd_version": "1.45",
            "qrenderdoc_version": "1.45",
        },
    )
    monkeypatch.setattr(lifecycle, "probe_qrenderdoc_python", lambda *_args: None)

    argv = [
        "install",
        "--python",
        sys.executable,
        "--receipt-path",
        str(receipt),
        "--json",
        "--yes",
    ]
    assert cli.run(argv) == 0
    first = json.loads(capsys.readouterr().out)
    _validate_install_result(first)
    stored = json.loads(receipt.read_text(encoding="utf-8"))

    assert first["status"] == "ok"
    assert first["verify"]["directly_usable"] is True
    assert stored["managed"] is True
    assert stored["owned_files"] == files
    assert stored["command"] == str(command.resolve())
    assert stored["transaction"]["committed_at"].endswith("Z")
    assert stored["transaction"]["prior_adapter_version"] is None
    first_receipt_bytes = receipt.read_bytes()

    assert cli.run(argv) == 0
    second = json.loads(capsys.readouterr().out)
    _validate_install_result(second)
    assert receipt.read_bytes() == first_receipt_bytes

    for operation in ("status", "verify"):
        assert (
            cli.run(
                [
                    operation,
                    "--python",
                    sys.executable,
                    "--receipt-path",
                    str(receipt),
                    "--json",
                ]
            )
            == 0
        )
        verified = json.loads(capsys.readouterr().out)
        _validate_install_result(verified)
        assert verified["verify"]["directly_usable"] is True

    assert (
        cli.run(
            [
                "uninstall",
                "--python",
                sys.executable,
                "--receipt-path",
                str(receipt),
                "--json",
                "--yes",
            ]
        )
        == 0
    )
    uninstalled = json.loads(capsys.readouterr().out)
    _validate_install_result(uninstalled)
    assert uninstalled["status"] == "ok"
    assert not destination.exists()
    assert not receipt.exists()


def test_manual_runtime_round_trip_status_verify_and_idempotent_uninstall(
    monkeypatch,
    capsys,
    tmp_path,
):
    from dcc_mcp_renderdoc import cli, lifecycle

    command = tmp_path / "renderdoccmd.exe"
    qrenderdoc = tmp_path / "qrenderdoc.exe"
    command.write_bytes(b"renderdoc")
    qrenderdoc.write_bytes(b"qrenderdoc")
    receipt = tmp_path / "receipts" / "renderdoc.json"
    monkeypatch.setattr(
        lifecycle,
        "probe_runtime",
        lambda *_args, **_kwargs: {
            "renderdoccmd_version": "1.45",
            "qrenderdoc_version": "1.45",
        },
    )
    monkeypatch.setattr(lifecycle, "probe_qrenderdoc_python", lambda *_args: None)
    common = [
        "--dcc-path",
        str(command),
        "--python",
        sys.executable,
        "--receipt-path",
        str(receipt),
        "--json",
    ]

    assert cli.run(["install", *common, "--yes"]) == 0
    install = json.loads(capsys.readouterr().out)
    _validate_install_result(install)

    for operation in ("status", "verify"):
        assert cli.run([operation, *common]) == 0
        report = json.loads(capsys.readouterr().out)
        _validate_install_result(report)
        assert report["status"] == "ok"
        assert report["verify"]["directly_usable"] is True

    assert cli.run(["uninstall", *common, "--dry-run"]) == 0
    planned = json.loads(capsys.readouterr().out)
    _validate_install_result(planned)
    assert planned["status"] == "planned"
    assert receipt.is_file()

    assert cli.run(["uninstall", *common, "--yes"]) == 0
    removed = json.loads(capsys.readouterr().out)
    _validate_install_result(removed)
    assert removed["status"] == "ok"
    assert not receipt.exists()
    assert command.read_bytes() == b"renderdoc"
    assert qrenderdoc.read_bytes() == b"qrenderdoc"

    assert cli.run(["uninstall", *common, "--yes"]) == 0
    absent = json.loads(capsys.readouterr().out)
    _validate_install_result(absent)
    assert absent["install_state"] == "fresh"


def test_manual_runtime_version_only_probe_fails_closed(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli, lifecycle

    command = tmp_path / "renderdoccmd.exe"
    command.write_bytes(b"renderdoc")
    command.with_name("qrenderdoc.exe").write_bytes(b"qrenderdoc")
    receipt = tmp_path / "renderdoc.json"
    monkeypatch.setattr(
        lifecycle,
        "probe_runtime",
        lambda *_args, **_kwargs: {
            "renderdoccmd_version": "1.45",
            "qrenderdoc_version": "1.45",
        },
    )

    exit_code = cli.run(
        [
            "install",
            "--dcc-path",
            str(command),
            "--python",
            sys.executable,
            "--receipt-path",
            str(receipt),
            "--json",
            "--yes",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 10
    assert report["verify"]["directly_usable"] is False
    assert report["verify"]["failure_reason"] == "manual_runtime_unverified"
    assert not receipt.exists()


def test_empty_qrenderdoc_file_never_proves_direct_usability(capsys, tmp_path):
    from dcc_mcp_renderdoc import cli

    command = tmp_path / "renderdoccmd.exe"
    command.write_bytes(b"")
    command.with_name("qrenderdoc.exe").write_bytes(b"")

    exit_code = cli.run(
        [
            "install",
            "--dcc-path",
            str(command),
            "--python",
            sys.executable,
            "--receipt-path",
            str(tmp_path / "receipt.json"),
            "--json",
            "--dry-run",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 10
    assert report["verify"]["directly_usable"] is False
    assert report["verify"]["failure_stage"] == "runtime"
    assert report["verify"]["failure_reason"] == "runtime_probe_failed"


def test_owned_file_mismatch_reports_bounded_relative_diff(tmp_path):
    from dcc_mcp_renderdoc import lifecycle

    destination = tmp_path / "managed"
    destination.mkdir()
    (destination / "changed.bin").write_bytes(b"new")
    (destination / "unexpected.bin").write_bytes(b"extra")
    expected = {
        "changed.bin": hashlib.sha256(b"old").hexdigest(),
        "missing.bin": hashlib.sha256(b"missing").hexdigest(),
    }

    try:
        lifecycle._verify_owned_files(destination, expected)
    except lifecycle.LifecycleError as exc:
        assert exc.reason == "owned_file_digest_mismatch"
        assert "missing=missing.bin" in str(exc)
        assert "unexpected=unexpected.bin" in str(exc)
        assert "changed=changed.bin" in str(exc)
        assert str(destination) not in str(exc)
    else:
        raise AssertionError("digest mismatch must fail")


def test_nested_cache_receipt_name_is_owned_and_tamper_checked(tmp_path):
    from dcc_mcp_renderdoc import lifecycle

    destination = tmp_path / "managed"
    nested = destination / "payload" / ".dcc-mcp-renderdoc.json"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"owned nested payload")
    expected = {
        "payload/.dcc-mcp-renderdoc.json": hashlib.sha256(b"owned nested payload").hexdigest()
    }

    lifecycle._verify_owned_files(destination, expected)
    nested.write_bytes(b"tampered")

    try:
        lifecycle._verify_owned_files(destination, expected)
    except lifecycle.LifecycleError as exc:
        assert exc.reason == "owned_file_digest_mismatch"
        assert "changed=payload/.dcc-mcp-renderdoc.json" in str(exc)
    else:
        raise AssertionError("nested receipt-name tamper must fail")


def test_upgrade_requires_prior_receipt_before_acquisition(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli, lifecycle

    monkeypatch.setattr(
        lifecycle,
        "download_pinned",
        lambda: (_ for _ in ()).throw(AssertionError("acquisition must not start")),
    )
    exit_code = cli.run(
        [
            "upgrade",
            "--python",
            sys.executable,
            "--receipt-path",
            str(tmp_path / "missing.json"),
            "--json",
            "--yes",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 10
    assert report["verify"]["failure_reason"] == "receipt_missing"


def test_default_managed_install_is_plan_only(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli, lifecycle

    _select_linux_managed_bundle(monkeypatch, lifecycle)

    monkeypatch.setattr(
        lifecycle,
        "download_pinned",
        lambda: (_ for _ in ()).throw(AssertionError("planning must not download")),
    )
    receipt = tmp_path / "renderdoc.json"
    exit_code = cli.run(
        [
            "install",
            "--python",
            sys.executable,
            "--receipt-path",
            str(receipt),
            "--json",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 0
    assert report["status"] == "planned"
    assert report["verify"]["directly_usable"] is False
    assert not receipt.exists()


def test_managed_plan_validates_bundle_override_and_records_python_source(
    monkeypatch, capsys, tmp_path
):
    from dcc_mcp_renderdoc import cli, lifecycle

    _select_linux_managed_bundle(monkeypatch, lifecycle)
    monkeypatch.setenv("DCC_MCP_INSTALL_PYTHON", sys.executable)
    receipt = tmp_path / "renderdoc.json"
    assert cli.run(["install", "--receipt-path", str(receipt), "--json"]) == 0
    planned = json.loads(capsys.readouterr().out)

    _validate_install_result(planned)
    assert planned["python"]["resolution_source"] == "environment"
    assert planned["selected_bundle"]["version"] == "1.45"

    monkeypatch.setenv("DCC_MCP_RENDERDOC_VERSION", "1.45")
    assert cli.run(["install", "--receipt-path", str(receipt), "--json"]) == 10
    failed = json.loads(capsys.readouterr().out)
    _validate_install_result(failed)
    assert failed["verify"]["failure_reason"] == "bundle_configuration_invalid"


def test_managed_plan_respects_disabled_acquisition(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli

    monkeypatch.setenv("DCC_MCP_RENDERDOC_AUTO_DOWNLOAD", "0")
    assert (
        cli.run(
            [
                "install",
                "--python",
                sys.executable,
                "--receipt-path",
                str(tmp_path / "renderdoc.json"),
                "--json",
            ]
        )
        == 10
    )
    report = json.loads(capsys.readouterr().out)
    _validate_install_result(report)
    assert report["verify"]["failure_reason"] == "managed_acquisition_disabled"


def test_text_status_without_receipt_is_machine_safe(capsys, tmp_path):
    from dcc_mcp_renderdoc import cli

    assert cli.run(["status", "--receipt-path", str(tmp_path / "missing.json")]) == 0
    assert "RenderDoc adapter is not ready." in capsys.readouterr().out


def test_failed_upgrade_probe_preserves_prior_working_receipt(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli, lifecycle

    old_command = tmp_path / "old" / "renderdoccmd.exe"
    old_command.parent.mkdir()
    old_command.write_bytes(b"old-renderdoc")
    old_command.with_name("qrenderdoc.exe").write_bytes(b"old-qrenderdoc")
    new_command = tmp_path / "new" / "renderdoccmd.exe"
    new_command.parent.mkdir()
    new_command.write_bytes(b"new-renderdoc")
    new_command.with_name("qrenderdoc.exe").write_bytes(b"new-qrenderdoc")
    receipt = tmp_path / "renderdoc.json"

    def probe(command, **_kwargs):
        if command == new_command.resolve():
            raise RuntimeError("qrenderdoc probe failed")
        return {"renderdoccmd_version": "1.44", "qrenderdoc_version": "1.44"}

    monkeypatch.setattr(lifecycle, "probe_runtime", probe)
    monkeypatch.setattr(lifecycle, "probe_qrenderdoc_python", lambda *_args: None)
    common = [
        "--python",
        sys.executable,
        "--receipt-path",
        str(receipt),
        "--json",
    ]
    assert cli.run(["install", "--dcc-path", str(old_command), *common, "--yes"]) == 0
    capsys.readouterr()
    prior_bytes = receipt.read_bytes()

    exit_code = cli.run(["upgrade", "--dcc-path", str(new_command), *common, "--yes"])
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 10
    assert report["verify"]["failure_reason"] == "runtime_probe_failed"
    assert receipt.read_bytes() == prior_bytes
    assert old_command.read_bytes() == b"old-renderdoc"
    assert old_command.with_name("qrenderdoc.exe").read_bytes() == b"old-qrenderdoc"


def test_failed_managed_uninstall_restores_runtime_and_receipt(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli, lifecycle

    _select_linux_managed_bundle(monkeypatch, lifecycle)

    destination = tmp_path / "cache" / "renderdoc" / "1.45-dddddddddddd"
    command = destination / "bin" / "renderdoccmd.exe"
    qrenderdoc = command.with_name("qrenderdoc.exe")
    command.parent.mkdir(parents=True)
    command.write_bytes(b"renderdoc")
    qrenderdoc.write_bytes(b"qrenderdoc")
    files = {
        "bin/qrenderdoc.exe": __import__("hashlib").sha256(b"qrenderdoc").hexdigest(),
        "bin/renderdoccmd.exe": __import__("hashlib").sha256(b"renderdoc").hexdigest(),
    }
    (destination / ".dcc-mcp-renderdoc.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "1.45",
                "url": "https://renderdoc.org/stable/1.45/RenderDoc_1.45_64.zip",
                "sha256": "d" * 64,
                "command": "bin/renderdoccmd.exe",
                "qrenderdoc": "bin/qrenderdoc.exe",
                "files": files,
                "probe": {"qrenderdoc_python_probe": "loaded"},
            }
        ),
        encoding="utf-8",
    )
    receipt = tmp_path / "receipts" / "renderdoc.json"
    monkeypatch.setenv("DCC_MCP_RUNTIME_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(lifecycle, "download_pinned", lambda *_args: command)
    monkeypatch.setattr(
        lifecycle,
        "probe_runtime",
        lambda *_args, **_kwargs: {
            "renderdoccmd_version": "1.45",
            "qrenderdoc_version": "1.45",
        },
    )
    common = [
        "--python",
        sys.executable,
        "--receipt-path",
        str(receipt),
        "--json",
    ]
    assert cli.run(["install", *common, "--yes"]) == 0
    capsys.readouterr()
    prior_receipt = receipt.read_bytes()

    real_rmtree = lifecycle.shutil.rmtree
    failed = False

    def fail_first_tombstone(path, *args, **kwargs):
        nonlocal failed
        if ".uninstall-" in str(path) and not failed:
            failed = True
            raise OSError("injected removal failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(lifecycle.shutil, "rmtree", fail_first_tombstone)
    exit_code = cli.run(["uninstall", *common, "--yes"])
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 30
    assert report["verify"]["failure_reason"] == "uninstall_failed"
    assert receipt.read_bytes() == prior_receipt
    assert command.read_bytes() == b"renderdoc"
    assert qrenderdoc.read_bytes() == b"qrenderdoc"


def test_windows_uninstall_copy_failure_is_stable_and_preserves_state(
    monkeypatch, capsys, tmp_path
):
    from dcc_mcp_renderdoc import cli, lifecycle

    destination = tmp_path / "managed"
    destination.mkdir()
    (destination / "runtime.bin").write_bytes(b"runtime")
    receipt = tmp_path / "renderdoc.json"
    receipt_bytes = json.dumps(
        {"receipt_version": 1, "dcc_type": "renderdoc"}, sort_keys=True
    ).encode("utf-8")
    receipt.write_bytes(receipt_bytes)
    monkeypatch.setattr(
        lifecycle,
        "_verify_receipt",
        lambda *_args, **_kwargs: {"managed_destination": str(destination)},
    )
    monkeypatch.setattr(lifecycle.sys, "platform", "win32")

    def fail_copy(_source, restore_copy):
        restore_copy.mkdir()
        (restore_copy / "partial.bin").write_bytes(b"partial")
        raise PermissionError("injected locked copy")

    monkeypatch.setattr(lifecycle.shutil, "copytree", fail_copy)

    exit_code = cli.run(["uninstall", "--receipt-path", str(receipt), "--json", "--yes"])
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 50
    assert report["verify"]["failure_reason"] == "runtime_locked"
    assert destination.joinpath("runtime.bin").read_bytes() == b"runtime"
    assert receipt.read_bytes() == receipt_bytes
    assert not list(tmp_path.glob(".managed.restore-*"))
    assert not list(tmp_path.glob(".renderdoc.json.uninstall-*"))


def test_windows_uninstall_backup_cleanup_failure_is_stable_after_commit(
    monkeypatch, capsys, tmp_path
):
    from dcc_mcp_renderdoc import cli, lifecycle

    destination = tmp_path / "managed"
    destination.mkdir()
    (destination / "runtime.bin").write_bytes(b"runtime")
    receipt = tmp_path / "renderdoc.json"
    receipt.write_text(
        json.dumps({"receipt_version": 1, "dcc_type": "renderdoc"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        lifecycle,
        "_verify_receipt",
        lambda *_args, **_kwargs: {"managed_destination": str(destination)},
    )
    monkeypatch.setattr(lifecycle.sys, "platform", "win32")
    real_rmtree = lifecycle.shutil.rmtree

    def fail_restore_cleanup(path, *args, **kwargs):
        if ".restore-" in Path(path).name:
            raise PermissionError("injected locked backup")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(lifecycle.shutil, "rmtree", fail_restore_cleanup)

    exit_code = cli.run(["uninstall", "--receipt-path", str(receipt), "--json", "--yes"])
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 50
    assert report["verify"]["failure_reason"] == "cleanup_requires_restart"
    assert not destination.exists()
    assert not receipt.exists()
    backups = list(tmp_path.glob(".managed.restore-*"))
    assert len(backups) == 1
    assert backups[0].joinpath("runtime.bin").read_bytes() == b"runtime"


def test_failed_upgrade_receipt_commit_preserves_previous_receipt(monkeypatch, capsys, tmp_path):
    from dcc_mcp_renderdoc import cli, lifecycle

    old_command = tmp_path / "old" / "renderdoccmd.exe"
    new_command = tmp_path / "new" / "renderdoccmd.exe"
    for command, payload in ((old_command, b"old"), (new_command, b"new")):
        command.parent.mkdir()
        command.write_bytes(payload)
        command.with_name("qrenderdoc.exe").write_bytes(payload + b"-q")
    receipt = tmp_path / "renderdoc.json"
    monkeypatch.setattr(
        lifecycle,
        "probe_runtime",
        lambda command, **_kwargs: {
            "renderdoccmd_version": "1.45" if command == new_command.resolve() else "1.44",
            "qrenderdoc_version": "1.45" if command == new_command.resolve() else "1.44",
        },
    )
    monkeypatch.setattr(lifecycle, "probe_qrenderdoc_python", lambda *_args: None)
    common = ["--python", sys.executable, "--receipt-path", str(receipt), "--json"]
    assert cli.run(["install", "--dcc-path", str(old_command), *common, "--yes"]) == 0
    capsys.readouterr()
    prior_receipt = receipt.read_bytes()

    real_replace = lifecycle.os.replace

    def fail_receipt_commit(source, destination):
        if Path(destination) == receipt:
            raise OSError("injected receipt commit failure")
        return real_replace(source, destination)

    monkeypatch.setattr(lifecycle.os, "replace", fail_receipt_commit)
    exit_code = cli.run(["upgrade", "--dcc-path", str(new_command), *common, "--yes"])
    report = json.loads(capsys.readouterr().out)

    _validate_install_result(report)
    assert exit_code == 30
    assert report["verify"]["failure_reason"] == "receipt_commit_failed"
    assert receipt.read_bytes() == prior_receipt
    assert not list(tmp_path.glob(".renderdoc.json.*.tmp"))
