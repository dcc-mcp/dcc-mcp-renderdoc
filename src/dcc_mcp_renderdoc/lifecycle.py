"""Agent-first RenderDoc install lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .__version__ import __version__
from .diagnostics import MIN_CORE_VERSION, MIN_RENDERDOC_VERSION, _meets_floor
from .downloader import (
    _cache_root,
    _cleanup_superseded,
    _configured_bundle,
    download_pinned,
    probe_qrenderdoc_python,
    probe_runtime,
)
from .install_contract import (
    INSTALL_EXIT_ACQUIRE,
    INSTALL_EXIT_INSTALL,
    INSTALL_EXIT_OK,
    INSTALL_EXIT_PREFLIGHT,
    INSTALL_EXIT_REQUIRES_RESTART,
    INSTALL_EXIT_VERIFY,
)

DCC_TYPE = "renderdoc"
DEFAULT_RECEIPT_PATH = Path.home() / ".dcc-mcp" / "receipts" / "renderdoc.json"


class LifecycleError(RuntimeError):
    def __init__(self, exit_code: int, stage: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stage = stage
        self.reason = reason


def _distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _receipt_path(value: Optional[Path]) -> Path:
    configured = value or Path(
        os.environ.get("DCC_MCP_RENDERDOC_RECEIPT", str(DEFAULT_RECEIPT_PATH))
    )
    return configured.expanduser().resolve()


def _base_report(operation: str, receipt_path: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "running",
        "dcc_type": DCC_TYPE,
        "adapter_version": __version__,
        "core_version": _distribution_version("dcc-mcp-core"),
        "operation": operation,
        "steps": [],
        "next_steps": [],
        "receipt_path": str(receipt_path),
        "verify": {
            "directly_usable": False,
            "failure_stage": None,
            "failure_reason": None,
        },
    }


def _next_step(identifier: str, description: str, why: str, command: list[str]) -> dict[str, Any]:
    return {
        "id": identifier,
        "description": description,
        "why": why,
        "command": command,
    }


def _command_for(args: argparse.Namespace, operation: str, *, execute: bool = False) -> list[str]:
    command = ["dcc-mcp-renderdoc", operation, "--json"]
    if execute:
        command.append("--yes")
    for flag, value in (
        ("--dcc-path", args.dcc_path),
        ("--python", args.python),
        ("--receipt-path", args.receipt_path),
    ):
        if value is not None:
            command.extend([flag, str(value)])
    return command


_PYTHON_PROBE = """
import importlib.metadata
import json
import sys
import dcc_mcp_renderdoc
import dcc_mcp_core
print(json.dumps({
    "python": sys.executable,
    "python_version": sys.version.split()[0],
    "adapter_version": importlib.metadata.version("dcc-mcp-renderdoc"),
    "core_version": importlib.metadata.version("dcc-mcp-core"),
}))
"""


def _probe_python(value: Optional[Path]) -> dict[str, str]:
    configured = os.environ.get("DCC_MCP_INSTALL_PYTHON")
    if value is not None:
        python = value
        resolution_source = "argument"
    elif configured:
        python = Path(configured)
        resolution_source = "environment"
    else:
        python = Path(sys.executable)
        resolution_source = "adapter_runtime"
    python = python.expanduser().resolve()
    if not python.is_file():
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "preflight",
            "python_missing",
            "The selected target interpreter does not exist.",
        )
    if python == Path(sys.executable).resolve():
        try:
            importlib.import_module("dcc_mcp_renderdoc")
            importlib.import_module("dcc_mcp_core")
            result = {
                "python": str(python),
                "python_version": sys.version.split()[0],
                "adapter_version": importlib.metadata.version("dcc-mcp-renderdoc"),
                "core_version": importlib.metadata.version("dcc-mcp-core"),
            }
        except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "preflight",
                "target_import_failed",
                "The target interpreter cannot import this adapter and dcc-mcp-core.",
            ) from exc
    else:
        try:
            completed = subprocess.run(
                [str(python), "-c", _PYTHON_PROBE],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "preflight",
                "python_probe_failed",
                f"The selected target interpreter could not be probed: {type(exc).__name__}.",
            ) from exc
        if completed.returncode != 0:
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "preflight",
                "target_import_failed",
                "The target interpreter cannot import this adapter and dcc-mcp-core.",
            )
        try:
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "preflight",
                "python_probe_invalid",
                "The selected target interpreter returned invalid probe data.",
            ) from exc
    if not isinstance(result, dict) or not _meets_floor(
        str(result.get("core_version") or ""), MIN_CORE_VERSION
    ):
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "preflight",
            "core_version_unsupported",
            f"dcc-mcp-core {MIN_CORE_VERSION}+ is required in the target interpreter.",
        )
    normalized = {str(key): str(value) for key, value in result.items()}
    normalized["resolution_source"] = resolution_source
    return normalized


def _resolve_explicit_command(value: Optional[Path]) -> Optional[Path]:
    if value is None:
        return None
    command = value.expanduser().resolve()
    if not command.is_file():
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "preflight",
            "renderdoccmd_missing",
            "--dcc-path must identify the exact renderdoccmd executable.",
        )
    return command


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _probe_runtime_checked(command: Path) -> dict[str, str]:
    try:
        return probe_runtime(command)
    except RuntimeError as exc:
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "runtime",
            "runtime_probe_failed",
            "The paired RenderDoc executables failed the bounded read-only loadability probe.",
        ) from exc


def _probe_manual_embedded_python(qrenderdoc: Path) -> None:
    try:
        probe_qrenderdoc_python(qrenderdoc)
    except RuntimeError as exc:
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "runtime",
            "manual_runtime_unverified",
            "The manual RenderDoc runtime did not prove bounded embedded-Python "
            "loadability; use the managed pinned runtime.",
        ) from exc


def _managed_receipt(command: Path) -> tuple[Path, dict[str, Any]]:
    destination = next(
        (parent for parent in command.parents if (parent / ".dcc-mcp-renderdoc.json").is_file()),
        command.parent,
    )
    path = destination / ".dcc-mcp-renderdoc.json"
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "receipt",
            "managed_receipt_invalid",
            "The managed RenderDoc cache receipt is missing or unreadable.",
        ) from exc
    files = receipt.get("files") if isinstance(receipt, dict) else None
    probe = receipt.get("probe") if isinstance(receipt, dict) else None
    if not isinstance(files, dict) or not files:
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "receipt",
            "managed_receipt_manifest_missing",
            "The managed RenderDoc cache receipt has no owned-file digest manifest.",
        )
    if not isinstance(probe, dict) or probe.get("qrenderdoc_python_probe") != "loaded":
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "runtime",
            "managed_runtime_unverified",
            "The managed cache receipt does not prove embedded-Python loadability.",
        )
    command_relative = receipt.get("command")
    qrenderdoc_relative = receipt.get("qrenderdoc")
    if not isinstance(command_relative, str) or not isinstance(qrenderdoc_relative, str):
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "receipt",
            "managed_receipt_paths_invalid",
            "The managed RenderDoc cache receipt has invalid executable paths.",
        )
    if (destination / command_relative).resolve() != command.resolve():
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "receipt",
            "managed_receipt_command_mismatch",
            "The managed receipt does not bind the selected renderdoccmd.",
        )
    qrenderdoc = (destination / qrenderdoc_relative).resolve()
    if qrenderdoc.parent != command.parent or not qrenderdoc.is_file():
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "receipt",
            "managed_receipt_pair_mismatch",
            "The managed receipt does not bind the paired qrenderdoc.",
        )
    for relative, expected in files.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "receipt",
                "managed_receipt_manifest_invalid",
                "The managed receipt file manifest is malformed.",
            )
        candidate = (destination / relative).resolve()
        try:
            candidate.relative_to(destination.resolve())
        except ValueError as exc:
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "receipt",
                "managed_receipt_path_unsafe",
                "The managed receipt contains a path outside its cache destination.",
            ) from exc
        if not candidate.is_file() or _sha256(candidate) != expected:
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "receipt",
                "managed_receipt_digest_mismatch",
                "The managed RenderDoc cache no longer matches its receipt.",
            )
    return destination, receipt


def _set_failure(report: dict[str, Any], exc: LifecycleError) -> tuple[dict[str, Any], int]:
    report["status"] = (
        "requires_restart" if exc.exit_code == INSTALL_EXIT_REQUIRES_RESTART else "failed"
    )
    report["verify"] = {
        "directly_usable": False,
        "failure_stage": exc.stage,
        "failure_reason": exc.reason,
    }
    report["message"] = str(exc)
    if not report["next_steps"]:
        operation = "install" if exc.reason == "receipt_missing" else "status"
        report["next_steps"] = [
            _next_step(
                "recover",
                "Inspect or rebuild the receipted RenderDoc lifecycle state.",
                "The current lifecycle command did not prove a usable state.",
                [
                    "dcc-mcp-renderdoc",
                    operation,
                    "--json",
                    "--receipt-path",
                    report["receipt_path"],
                ],
            )
        ]
    return report, exc.exit_code


def _read_receipt(path: Path, *, required: bool) -> Optional[dict[str, Any]]:
    if not path.is_file():
        if required:
            raise LifecycleError(
                INSTALL_EXIT_VERIFY,
                "receipt",
                "receipt_missing",
                "No RenderDoc lifecycle receipt exists at the selected path.",
            )
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "receipt",
            "receipt_invalid",
            "The RenderDoc lifecycle receipt is unreadable.",
        ) from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("receipt_version") != 1
        or receipt.get("dcc_type") != DCC_TYPE
    ):
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "receipt",
            "receipt_invalid",
            "The lifecycle receipt is not a supported RenderDoc receipt.",
        )
    return receipt


def _path_from_receipt(receipt: dict[str, Any], key: str) -> Path:
    value = receipt.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "receipt",
            "receipt_path_invalid",
            f"The lifecycle receipt has no valid {key} path.",
        )
    return Path(value).expanduser().resolve()


def _verify_owned_files(destination: Path, files: Any) -> None:
    if not isinstance(files, dict) or not files:
        raise LifecycleError(
            INSTALL_EXIT_VERIFY,
            "artifact",
            "owned_file_manifest_missing",
            "The managed receipt has no owned-file digest manifest.",
        )
    if not all(isinstance(path, str) and isinstance(digest, str) for path, digest in files.items()):
        raise LifecycleError(
            INSTALL_EXIT_VERIFY,
            "artifact",
            "owned_file_manifest_invalid",
            "The managed receipt owned-file manifest is malformed.",
        )
    actual: dict[str, str] = {}
    for path in sorted(destination.rglob("*")):
        if path.is_symlink():
            raise LifecycleError(
                INSTALL_EXIT_VERIFY,
                "artifact",
                "owned_file_link_unsafe",
                "The managed RenderDoc cache contains an unexpected link.",
            )
        if path.is_file() and path != destination / ".dcc-mcp-renderdoc.json":
            actual[path.relative_to(destination).as_posix()] = _sha256(path)
    if actual != files:
        expected_paths = set(files)
        actual_paths = set(actual)
        missing = sorted(expected_paths - actual_paths)
        unexpected = sorted(actual_paths - expected_paths)
        changed = sorted(
            path for path in expected_paths & actual_paths if files[path] != actual[path]
        )
        summaries = []
        for label, paths in (
            ("missing", missing),
            ("unexpected", unexpected),
            ("changed", changed),
        ):
            if paths:
                summaries.append(f"{label}={','.join(paths[:5])}")
        raise LifecycleError(
            INSTALL_EXIT_VERIFY,
            "artifact",
            "owned_file_digest_mismatch",
            "The managed RenderDoc files do not match their receipt digests"
            f" ({'; '.join(summaries)}).",
        )


def _verify_receipt(
    args: argparse.Namespace,
    receipt: dict[str, Any],
    *,
    allow_manual_unverified: bool = False,
    manual_probe_already_verified: bool = False,
) -> dict[str, Any]:
    command = _path_from_receipt(receipt, "command")
    qrenderdoc = _path_from_receipt(receipt, "qrenderdoc")
    if args.dcc_path is not None and args.dcc_path.expanduser().resolve() != command:
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "preflight",
            "dcc_path_mismatch",
            "--dcc-path does not match the runtime recorded by the receipt.",
        )
    recorded_python = _path_from_receipt(receipt, "python")
    if args.python is not None and args.python.expanduser().resolve() != recorded_python:
        raise LifecycleError(
            INSTALL_EXIT_PREFLIGHT,
            "preflight",
            "python_mismatch",
            "--python does not match the interpreter recorded by the receipt.",
        )
    python = _probe_python(recorded_python)
    if receipt.get("adapter_version") != __version__:
        raise LifecycleError(
            INSTALL_EXIT_VERIFY,
            "package",
            "adapter_version_mismatch",
            "The lifecycle receipt records another adapter version.",
        )
    if not command.is_file() or not qrenderdoc.is_file() or qrenderdoc.parent != command.parent:
        raise LifecycleError(
            INSTALL_EXIT_VERIFY,
            "artifact",
            "runtime_pair_missing",
            "The receipted renderdoccmd/qrenderdoc pair is missing.",
        )
    binary_digests = receipt.get("binary_digests")
    if not isinstance(binary_digests, dict) or binary_digests != {
        "renderdoccmd": _sha256(command),
        "qrenderdoc": _sha256(qrenderdoc),
    }:
        raise LifecycleError(
            INSTALL_EXIT_VERIFY,
            "artifact",
            "runtime_digest_mismatch",
            "The receipted RenderDoc executable bytes changed.",
        )
    managed = receipt.get("managed") is True
    destination: Optional[Path] = None
    if managed:
        destination = _path_from_receipt(receipt, "managed_destination")
        bundle = receipt.get("bundle")
        if not isinstance(bundle, dict):
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "receipt",
                "bundle_receipt_invalid",
                "The managed lifecycle receipt has no bundle identity.",
            )
        version = bundle.get("version")
        checksum = bundle.get("sha256")
        if (
            not isinstance(version, str)
            or not isinstance(checksum, str)
            or destination.name != f"{version}-{checksum[:12]}"
            or destination.parent != _cache_root().resolve()
        ):
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "receipt",
                "managed_destination_unsafe",
                "The managed lifecycle receipt is outside the configured RenderDoc cache.",
            )
        try:
            command.relative_to(destination)
            qrenderdoc.relative_to(destination)
        except ValueError as exc:
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "receipt",
                "managed_runtime_path_unsafe",
                "The managed executable paths are outside their receipted destination.",
            ) from exc
        _verify_owned_files(destination, receipt.get("owned_files"))
    try:
        runtime = probe_runtime(command)
    except RuntimeError as exc:
        raise LifecycleError(
            INSTALL_EXIT_VERIFY,
            "runtime",
            "runtime_probe_failed",
            "The paired RenderDoc executables failed the bounded read-only loadability probe.",
        ) from exc
    if not _meets_floor(runtime.get("renderdoccmd_version"), MIN_RENDERDOC_VERSION):
        raise LifecycleError(
            INSTALL_EXIT_VERIFY,
            "runtime",
            "renderdoc_version_unsupported",
            f"RenderDoc {MIN_RENDERDOC_VERSION}+ is required.",
        )
    recorded_runtime = receipt.get("runtime")
    if managed:
        if (
            not isinstance(recorded_runtime, dict)
            or recorded_runtime.get("qrenderdoc_python_probe") != "loaded"
        ):
            raise LifecycleError(
                INSTALL_EXIT_VERIFY,
                "runtime",
                "managed_runtime_unverified",
                "The managed lifecycle receipt does not prove embedded-Python loadability.",
            )
        runtime["qrenderdoc_python_probe"] = "loaded"
    elif not allow_manual_unverified and not manual_probe_already_verified:
        _probe_manual_embedded_python(qrenderdoc)
        runtime["qrenderdoc_python_probe"] = "loaded"
    return {
        "command": str(command),
        "qrenderdoc": str(qrenderdoc),
        "managed": managed,
        "managed_destination": str(destination) if destination else None,
        "python": python,
        "runtime": runtime,
    }


def _handle_status(args: argparse.Namespace, *, verify: bool) -> tuple[dict[str, Any], int]:
    receipt_path = _receipt_path(args.receipt_path)
    report = _base_report(args.operation, receipt_path)
    try:
        receipt = _read_receipt(receipt_path, required=verify)
        if receipt is None:
            report.update({"status": "ok", "install_state": "fresh"})
            report["steps"] = [{"id": "receipt", "status": "absent"}]
            report["next_steps"] = [
                _next_step(
                    "install",
                    "Plan the pinned RenderDoc runtime installation.",
                    "No lifecycle receipt exists.",
                    _command_for(args, "install"),
                )
            ]
            return report, INSTALL_EXIT_OK
        details = _verify_receipt(args, receipt)
        report.update(
            {
                "status": "ok",
                "install_state": "current",
                "core_version": details["python"]["core_version"],
                "runtime": details,
            }
        )
        report["verify"] = {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
        }
        report["steps"] = [
            {"id": "receipt", "status": "ok"},
            {"id": "artifact", "status": "ok"},
            {"id": "package", "status": "ok"},
            {"id": "runtime_probe", "status": "ok"},
        ]
        return report, INSTALL_EXIT_OK
    except LifecycleError as exc:
        return _set_failure(report, exc)


def _restore_uninstall(
    destination: Path,
    tombstone: Path,
    restore_copy: Path,
    receipt_path: Path,
    receipt_backup: Path,
) -> list[str]:
    failures: list[str] = []
    try:
        if destination.exists():
            shutil.rmtree(destination)
        if restore_copy.exists():
            os.replace(restore_copy, destination)
        elif tombstone.exists():
            os.replace(tombstone, destination)
    except OSError as exc:
        failures.append(f"runtime: {exc}")
    try:
        if receipt_backup.exists() and not receipt_path.exists():
            os.replace(receipt_backup, receipt_path)
    except OSError as exc:
        failures.append(f"receipt: {exc}")
    return failures


def _uninstall_error(
    exc: OSError,
    *,
    cleanup: bool = False,
) -> LifecycleError:
    restart = sys.platform == "win32" and isinstance(exc, PermissionError)
    if cleanup:
        return LifecycleError(
            INSTALL_EXIT_REQUIRES_RESTART if restart else INSTALL_EXIT_INSTALL,
            "cleanup",
            "cleanup_requires_restart" if restart else "cleanup_incomplete",
            (
                "Uninstall completed, but rollback-copy cleanup is locked and requires a restart."
                if restart
                else "Uninstall completed, but its rollback copy could not be removed."
            ),
        )
    return LifecycleError(
        INSTALL_EXIT_REQUIRES_RESTART if restart else INSTALL_EXIT_INSTALL,
        "uninstall",
        "runtime_locked" if restart else "uninstall_failed",
        "Uninstall failed; the prior runtime and receipt remain active.",
    )


def _handle_uninstall(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    receipt_path = _receipt_path(args.receipt_path)
    report = _base_report(args.operation, receipt_path)
    try:
        receipt = _read_receipt(receipt_path, required=False)
        if receipt is None:
            report.update({"status": "ok", "install_state": "fresh"})
            report["steps"] = [{"id": "uninstall", "status": "skipped"}]
            return report, INSTALL_EXIT_OK
        details = _verify_receipt(args, receipt, allow_manual_unverified=True)
        report["steps"] = [
            {"id": "receipt", "status": "ok"},
            {"id": "remove", "status": "planned"},
        ]
        if args.dry_run or not args.yes:
            report["status"] = "planned"
            report["next_steps"] = [
                _next_step(
                    "execute",
                    "Remove only the receipted RenderDoc state.",
                    "Planning does not remove binaries or receipts.",
                    _command_for(args, "uninstall", execute=True),
                )
            ]
            return report, INSTALL_EXIT_OK
        token = uuid.uuid4().hex
        receipt_backup = receipt_path.with_name(f".{receipt_path.name}.uninstall-{token}")
        destination_text = details.get("managed_destination")
        if destination_text is None:
            try:
                os.replace(receipt_path, receipt_backup)
                receipt_backup.unlink()
            except OSError as exc:
                try:
                    if receipt_backup.exists() and not receipt_path.exists():
                        os.replace(receipt_backup, receipt_path)
                except OSError as rollback_exc:
                    raise LifecycleError(
                        INSTALL_EXIT_INSTALL,
                        "rollback",
                        "rollback_failed",
                        "Manual-runtime receipt removal failed and its receipt could not "
                        "be restored.",
                    ) from rollback_exc
                raise _uninstall_error(exc) from exc
        else:
            destination = Path(destination_text)
            tombstone = destination.with_name(f".{destination.name}.uninstall-{token}")
            restore_copy = destination.with_name(f".{destination.name}.restore-{token}")
            try:
                shutil.copytree(destination, restore_copy)
            except OSError as exc:
                if restore_copy.exists():
                    try:
                        shutil.rmtree(restore_copy, ignore_errors=True)
                    except OSError:
                        pass
                raise _uninstall_error(exc) from exc
            try:
                os.replace(destination, tombstone)
                os.replace(receipt_path, receipt_backup)
                shutil.rmtree(tombstone)
                receipt_backup.unlink(missing_ok=True)
            except OSError as exc:
                failures = _restore_uninstall(
                    destination,
                    tombstone,
                    restore_copy,
                    receipt_path,
                    receipt_backup,
                )
                if failures:
                    raise LifecycleError(
                        INSTALL_EXIT_INSTALL,
                        "rollback",
                        "rollback_failed",
                        "Uninstall failed and the prior managed runtime could not be "
                        "fully restored.",
                    ) from exc
                raise _uninstall_error(exc) from exc
            finally:
                if tombstone.exists():
                    try:
                        shutil.rmtree(tombstone, ignore_errors=True)
                    except OSError:
                        pass
            if restore_copy.exists():
                try:
                    shutil.rmtree(restore_copy)
                except OSError as exc:
                    raise _uninstall_error(exc, cleanup=True) from exc
        report["steps"][1]["status"] = "ok"
        report.update({"status": "ok", "install_state": "fresh"})
        return report, INSTALL_EXIT_OK
    except LifecycleError as exc:
        return _set_failure(report, exc)


def _handle_install(args: argparse.Namespace, *, upgrade: bool) -> tuple[dict[str, Any], int]:
    receipt_path = _receipt_path(args.receipt_path)
    report = _base_report(args.operation, receipt_path)
    try:
        existing_receipt = _read_receipt(receipt_path, required=False)
        prior_receipt: Optional[dict[str, Any]] = None
        if existing_receipt is not None:
            prior_receipt = existing_receipt
            prior_args = argparse.Namespace(**vars(args))
            prior_args.dcc_path = None
            prior_args.python = None
            report["prior_runtime"] = _verify_receipt(prior_args, prior_receipt)
        elif upgrade:
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "preflight",
                "receipt_missing",
                "Upgrade requires a prior usable RenderDoc lifecycle receipt.",
            )
        python = _probe_python(args.python)
        command = _resolve_explicit_command(args.dcc_path)
        managed = command is None
        selected_bundle = None
        if managed:
            if os.environ.get("DCC_MCP_RENDERDOC_AUTO_DOWNLOAD", "1").lower() in {
                "0",
                "false",
                "no",
            }:
                raise LifecycleError(
                    INSTALL_EXIT_PREFLIGHT,
                    "preflight",
                    "managed_acquisition_disabled",
                    "Managed acquisition is disabled; pass the exact RenderDoc command with "
                    "--dcc-path.",
                )
            try:
                selected_bundle = _configured_bundle()
            except RuntimeError as exc:
                raise LifecycleError(
                    INSTALL_EXIT_PREFLIGHT,
                    "preflight",
                    "bundle_configuration_invalid",
                    "The managed RenderDoc bundle pin is invalid for this platform.",
                ) from exc
        if managed and (args.dry_run or not args.yes):
            report.update(
                {
                    "core_version": python["core_version"],
                    "install_state": "upgrade" if prior_receipt else "fresh",
                    "python": python,
                    "managed_runtime": True,
                    "selected_bundle": {
                        "version": selected_bundle.version,
                        "url": selected_bundle.url,
                        "sha256": selected_bundle.sha256,
                    },
                    "status": "planned",
                    "steps": [
                        {"id": "preflight", "status": "ok"},
                        {"id": "acquire", "status": "planned"},
                        {"id": "receipt", "status": "planned"},
                        {"id": "verify", "status": "planned"},
                    ],
                }
            )
            report["next_steps"] = [
                _next_step(
                    "execute",
                    "Acquire and verify the pinned managed RenderDoc runtime.",
                    "Planning does not download or write a lifecycle receipt.",
                    _command_for(args, args.operation, execute=True),
                )
            ]
            return report, INSTALL_EXIT_OK
        if managed:
            try:
                command = download_pinned(selected_bundle)
            except (OSError, RuntimeError) as exc:
                raise LifecycleError(
                    INSTALL_EXIT_ACQUIRE,
                    "acquire",
                    "pinned_acquisition_failed",
                    "The pinned RenderDoc runtime could not be acquired and verified.",
                ) from exc
        assert command is not None
        runtime = _probe_runtime_checked(command)
        version = runtime["renderdoccmd_version"]
        if not _meets_floor(version, MIN_RENDERDOC_VERSION):
            raise LifecycleError(
                INSTALL_EXIT_PREFLIGHT,
                "preflight",
                "renderdoc_version_unsupported",
                f"RenderDoc {MIN_RENDERDOC_VERSION}+ is required.",
            )
        qrenderdoc = command.with_name(
            "qrenderdoc.exe" if command.name.casefold().endswith(".exe") else "qrenderdoc"
        )
        managed_destination: Optional[Path] = None
        managed_cache_receipt: Optional[dict[str, Any]] = None
        if managed:
            managed_destination, managed_cache_receipt = _managed_receipt(command)
            runtime["qrenderdoc_python_probe"] = "loaded"
        elif args.yes and not args.dry_run:
            _probe_manual_embedded_python(qrenderdoc)
            runtime["qrenderdoc_python_probe"] = "loaded"
        state = "upgrade" if upgrade else ("current" if receipt_path.is_file() else "fresh")
        report.update(
            {
                "core_version": python["core_version"],
                "install_state": state,
                "python": python,
                "runtime": runtime,
                "selected_command": str(command),
                "selected_qrenderdoc": str(qrenderdoc),
                "managed_runtime": managed,
            }
        )
        report["steps"] = [
            {"id": "preflight", "status": "ok"},
            {"id": "install", "status": "planned"},
            {"id": "receipt", "status": "planned"},
            {"id": "verify", "status": "planned"},
        ]
        if args.dry_run or not args.yes:
            report["status"] = "planned"
            report["verify"] = {
                "directly_usable": False,
                "failure_stage": "receipt",
                "failure_reason": "receipt_not_committed",
            }
            report["next_steps"] = [
                _next_step(
                    "execute",
                    "Record the verified operator-managed RenderDoc runtime.",
                    "Planning does not write the lifecycle receipt.",
                    _command_for(args, args.operation, execute=True),
                )
            ]
            return report, INSTALL_EXIT_OK
        receipt = {
            "receipt_version": 1,
            "dcc_type": DCC_TYPE,
            "adapter_version": __version__,
            "core_version": python["core_version"],
            "python_version": python["python_version"],
            "python": python["python"],
            "python_resolution_source": python["resolution_source"],
            "managed": managed,
            "command": str(command),
            "qrenderdoc": str(qrenderdoc),
            "owned_files": (dict(managed_cache_receipt["files"]) if managed_cache_receipt else {}),
            "binary_digests": {
                "renderdoccmd": _sha256(command),
                "qrenderdoc": _sha256(qrenderdoc),
            },
            "runtime": runtime,
            "dcc_version": runtime["renderdoccmd_version"],
            "selected_host_path": str(command),
            "profile_path": None,
            "registration_files": [],
            "host_configuration": [],
            "server_binding": {"kind": "external-mcp-server", "transport": "loopback"},
        }
        if managed_destination is not None:
            receipt["managed_destination"] = str(managed_destination)
            receipt["bundle"] = {
                key: managed_cache_receipt[key]
                for key in ("version", "url", "sha256")
                if key in managed_cache_receipt
            }
        stable_fields = (
            "receipt_version",
            "dcc_type",
            "adapter_version",
            "core_version",
            "python_version",
            "python",
            "python_resolution_source",
            "managed",
            "command",
            "qrenderdoc",
            "owned_files",
            "binary_digests",
            "runtime",
            "dcc_version",
            "selected_host_path",
            "profile_path",
            "registration_files",
            "host_configuration",
            "server_binding",
            "managed_destination",
            "bundle",
        )
        if existing_receipt is not None and all(
            existing_receipt.get(key) == receipt.get(key) for key in stable_fields
        ):
            transaction = existing_receipt.get("transaction")
        else:
            transaction = None
        if not isinstance(transaction, dict):
            transaction = {
                "committed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "prior_adapter_version": (
                    existing_receipt.get("adapter_version") if existing_receipt else None
                ),
                "prior_runtime_version": (
                    (existing_receipt.get("runtime") or {}).get("renderdoccmd_version")
                    if existing_receipt and isinstance(existing_receipt.get("runtime"), dict)
                    else None
                ),
            }
        receipt["transaction"] = transaction
        previous_receipt_bytes = receipt_path.read_bytes() if receipt_path.is_file() else None
        temporary = receipt_path.with_name(f".{receipt_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, receipt_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise LifecycleError(
                INSTALL_EXIT_INSTALL,
                "receipt",
                "receipt_commit_failed",
                "The verified runtime was not committed because its lifecycle receipt "
                "could not be written.",
            ) from exc
        try:
            report["verification"] = _verify_receipt(
                args,
                receipt,
                manual_probe_already_verified=not managed,
            )
        except LifecycleError as verify_exc:
            try:
                if previous_receipt_bytes is None:
                    receipt_path.unlink(missing_ok=True)
                else:
                    rollback_receipt = receipt_path.with_name(
                        f".{receipt_path.name}.{uuid.uuid4().hex}.rollback"
                    )
                    rollback_receipt.write_bytes(previous_receipt_bytes)
                    os.replace(rollback_receipt, receipt_path)
            except OSError as rollback_exc:
                raise LifecycleError(
                    INSTALL_EXIT_INSTALL,
                    "rollback",
                    "rollback_failed",
                    "The new receipt failed verification and the prior receipt could not "
                    "be restored.",
                ) from rollback_exc
            raise verify_exc
        if managed_destination is not None:
            try:
                _cleanup_superseded(managed_destination.parent, managed_destination)
            except OSError as exc:
                report["status"] = "partial"
                report["cleanup_error"] = type(exc).__name__
                report["steps"][1]["status"] = "ok"
                report["steps"][2]["status"] = "ok"
                report["steps"][3]["status"] = "ok"
                report["verify"] = {
                    "directly_usable": True,
                    "failure_stage": None,
                    "failure_reason": None,
                }
                report["next_steps"] = [
                    _next_step(
                        "inspect-cleanup",
                        "Inspect the remaining receipted managed cache versions.",
                        "The new verified runtime is active, but old-cache cleanup did not "
                        "complete.",
                        _command_for(args, "status"),
                    )
                ]
                return report, INSTALL_EXIT_INSTALL
        report["steps"][1]["status"] = "ok"
        report["steps"][2]["status"] = "ok"
        report["steps"][3]["status"] = "ok"
        report["status"] = "ok"
        report["verify"] = {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
        }
        return report, INSTALL_EXIT_OK
    except LifecycleError as exc:
        return _set_failure(report, exc)


def handle(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.operation == "install":
        return _handle_install(args, upgrade=False)
    if args.operation == "upgrade":
        return _handle_install(args, upgrade=True)
    if args.operation == "status":
        return _handle_status(args, verify=False)
    if args.operation == "verify":
        return _handle_status(args, verify=True)
    if args.operation == "uninstall":
        return _handle_uninstall(args)
    raise AssertionError(f"unsupported lifecycle operation: {args.operation}")
