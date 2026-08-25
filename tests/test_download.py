from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from dcc_mcp_renderdoc import downloader as runtime_downloader
from dcc_mcp_renderdoc._owned_process import OwnedProcessResult, OwnedProcessTimeoutError


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, *, url: str, content_length: int | None = None):
        super().__init__(payload)
        self._url = url
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def geturl(self) -> str:
        return self._url


def test_archive_member_must_remain_below_destination(tmp_path: Path):
    with pytest.raises(RuntimeError, match="escapes destination"):
        runtime_downloader._safe_destination(tmp_path, "../outside")


def test_runtime_downloader_rejects_unverified_latest_before_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    requested_urls: list[str] = []

    def fake_urlopen(request: object, timeout: int) -> io.BytesIO:
        del timeout
        requested_urls.append(str(getattr(request, "full_url", request)))
        return io.BytesIO(b"unverified payload")

    monkeypatch.setenv("DCC_MCP_RUNTIME_CACHE", str(cache))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_VERSION", "1.45")
    monkeypatch.setenv(
        "DCC_MCP_RENDERDOC_URL",
        "https://renderdoc.org/stable/1.45/renderdoc_1.45.tar.gz",
    )
    monkeypatch.setenv("DCC_MCP_RENDERDOC_SHA256", "0" * 64)
    monkeypatch.setattr(runtime_downloader.sys, "platform", "linux")
    monkeypatch.setattr(runtime_downloader.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="checksum"):
        runtime_downloader.download_pinned()

    assert "https://renderdoc.org/builds" not in requested_urls
    assert list(cache.rglob("*")) == []


def test_runtime_downloader_requires_complete_pin_before_network(monkeypatch, tmp_path):
    monkeypatch.setenv("DCC_MCP_RUNTIME_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_VERSION", "latest")
    monkeypatch.delenv("DCC_MCP_RENDERDOC_URL", raising=False)
    monkeypatch.delenv("DCC_MCP_RENDERDOC_SHA256", raising=False)
    monkeypatch.setattr(
        runtime_downloader.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("unpinned downloads must not reach the network"),
    )

    with pytest.raises(RuntimeError, match="requires a pinned RenderDoc bundle"):
        runtime_downloader.download_pinned()


def test_runtime_downloader_rejects_redirect_to_another_origin(monkeypatch, tmp_path):
    payload = b"archive"
    monkeypatch.setenv("DCC_MCP_RUNTIME_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_VERSION", "1.45")
    monkeypatch.setenv(
        "DCC_MCP_RENDERDOC_URL",
        "https://renderdoc.org/stable/1.45/renderdoc_1.45.tar.gz",
    )
    monkeypatch.setenv("DCC_MCP_RENDERDOC_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(runtime_downloader.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime_downloader.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            payload,
            url="https://downloads.example/renderdoc_1.45.tar.gz",
            content_length=len(payload),
        ),
    )

    with pytest.raises(RuntimeError, match="final download origin"):
        runtime_downloader.download_pinned()

    assert list((tmp_path / "cache").rglob("*")) == []


def test_runtime_downloader_rejects_oversized_content_length(monkeypatch, tmp_path):
    monkeypatch.setenv("DCC_MCP_RUNTIME_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_VERSION", "1.45")
    url = "https://renderdoc.org/stable/1.45/renderdoc_1.45.tar.gz"
    monkeypatch.setenv("DCC_MCP_RENDERDOC_URL", url)
    monkeypatch.setenv("DCC_MCP_RENDERDOC_SHA256", "0" * 64)
    monkeypatch.setattr(runtime_downloader.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime_downloader.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            b"",
            url=url,
            content_length=runtime_downloader.MAX_DOWNLOAD_BYTES + 1,
        ),
    )

    with pytest.raises(RuntimeError, match="Content-Length exceeds"):
        runtime_downloader.download_pinned()

    assert list((tmp_path / "cache").rglob("*")) == []


def test_runtime_downloader_enforces_stream_limit_without_content_length(monkeypatch, tmp_path):
    payload = b"12345"
    url = "https://renderdoc.org/stable/1.45/renderdoc_1.45.tar.gz"
    monkeypatch.setenv("DCC_MCP_RUNTIME_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_VERSION", "1.45")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_URL", url)
    monkeypatch.setenv("DCC_MCP_RENDERDOC_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(runtime_downloader.sys, "platform", "linux")
    monkeypatch.setattr(runtime_downloader, "MAX_DOWNLOAD_BYTES", 4)
    monkeypatch.setattr(
        runtime_downloader.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload, url=url),
    )

    with pytest.raises(RuntimeError, match="bounded byte limit"):
        runtime_downloader.download_pinned()

    assert list((tmp_path / "cache").rglob("*")) == []


def test_zip_extraction_rejects_too_many_members(monkeypatch, tmp_path):
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("one", b"1")
        bundle.writestr("two", b"2")
    monkeypatch.setattr(runtime_downloader, "MAX_ARCHIVE_MEMBERS", 1)

    with pytest.raises(RuntimeError, match="member count"):
        runtime_downloader._extract(archive, tmp_path / "out")


def test_zip_extraction_rejects_oversized_member(monkeypatch, tmp_path):
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("huge", b"12345")
    monkeypatch.setattr(runtime_downloader, "MAX_MEMBER_BYTES", 4)

    with pytest.raises(RuntimeError, match="member size"):
        runtime_downloader._extract(archive, tmp_path / "out")


def test_zip_extraction_rejects_oversized_total(monkeypatch, tmp_path):
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("one", b"123")
        bundle.writestr("two", b"456")
    monkeypatch.setattr(runtime_downloader, "MAX_EXTRACTED_BYTES", 5)

    with pytest.raises(RuntimeError, match="expanded size"):
        runtime_downloader._extract(archive, tmp_path / "out")


def test_verified_download_replaces_only_superseded_managed_cache(monkeypatch, tmp_path):
    command_bytes = b"\x7fELFrenderdoc"
    qrenderdoc_bytes = b"\x7fELFqrenderdoc"
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as bundle:
        command = tarfile.TarInfo("renderdoc_1.45/bin/renderdoccmd")
        command.mode = 0o755
        command.size = len(command_bytes)
        bundle.addfile(command, io.BytesIO(command_bytes))
        qrenderdoc = tarfile.TarInfo("renderdoc_1.45/bin/qrenderdoc")
        qrenderdoc.mode = 0o755
        qrenderdoc.size = len(qrenderdoc_bytes)
        bundle.addfile(qrenderdoc, io.BytesIO(qrenderdoc_bytes))
        nested_receipt_bytes = b"nested archive payload"
        nested_receipt = tarfile.TarInfo("renderdoc_1.45/share/renderdoc/.dcc-mcp-renderdoc.json")
        nested_receipt.size = len(nested_receipt_bytes)
        bundle.addfile(nested_receipt, io.BytesIO(nested_receipt_bytes))
    archive = payload.getvalue()
    checksum = hashlib.sha256(archive).hexdigest()
    cache = tmp_path / "cache"
    root = cache / "renderdoc"
    old_checksum = "1" * 64
    old_managed = root / f"1.44-{old_checksum[:12]}"
    old_command = old_managed / "bin/renderdoccmd"
    old_qrenderdoc = old_command.with_name("qrenderdoc")
    old_command.parent.mkdir(parents=True)
    old_command.write_bytes(b"old-renderdoc")
    old_qrenderdoc.write_bytes(b"old-qrenderdoc")
    (old_managed / ".dcc-mcp-renderdoc.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "1.44",
                "url": "https://renderdoc.org/stable/1.44/renderdoc_1.44.tar.gz",
                "sha256": old_checksum,
                "command": "bin/renderdoccmd",
                "qrenderdoc": "bin/qrenderdoc",
                "files": {
                    "bin/renderdoccmd": hashlib.sha256(b"old-renderdoc").hexdigest(),
                    "bin/qrenderdoc": hashlib.sha256(b"old-qrenderdoc").hexdigest(),
                },
                "probe": {"qrenderdoc_python_probe": "loaded"},
            }
        ),
        encoding="utf-8",
    )
    unmanaged = root / "operator-files"
    unmanaged.mkdir()

    monkeypatch.setenv("DCC_MCP_RUNTIME_CACHE", str(cache))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_VERSION", "1.45")
    monkeypatch.setenv(
        "DCC_MCP_RENDERDOC_URL",
        "https://renderdoc.org/stable/1.45/renderdoc_1.45.tar.gz",
    )
    monkeypatch.setenv("DCC_MCP_RENDERDOC_SHA256", checksum)
    monkeypatch.setattr(runtime_downloader.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime_downloader.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(archive),
    )
    probes = []

    def run_probe(args, **kwargs):
        probes.append(args)
        if "--python" in args:
            qrenderdoc = next(
                Path(argument) for argument in args if Path(argument).name == "qrenderdoc"
            )
            python_cache = (
                qrenderdoc.parents[1]
                / "share/renderdoc/pylibs/lib/python3.6/__pycache__/json.cpython-36.pyc"
            )
            python_cache.parent.mkdir(parents=True)
            python_cache.write_bytes(b"managed-bytecode")
            Path(kwargs["env"]["DCC_MCP_RENDERDOC_PROBE_STATUS"]).write_text(
                "dcc-mcp-renderdoc-python-probe-ok", encoding="utf-8"
            )
            return OwnedProcessResult(0, "", "", False, False)
        output = "qrenderdoc v1.45" if "qrenderdoc" in str(args[0]) else "renderdoccmd v1.45"
        return OwnedProcessResult(0, output, "", False, False)

    monkeypatch.setattr(runtime_downloader, "run_owned_process", run_probe)

    installed = runtime_downloader.download_pinned()
    receipt = json.loads((installed.parents[2] / ".dcc-mcp-renderdoc.json").read_text())

    assert installed.read_bytes() == command_bytes
    assert receipt["sha256"] == checksum
    assert receipt["files"] == {
        "renderdoc_1.45/bin/qrenderdoc": hashlib.sha256(qrenderdoc_bytes).hexdigest(),
        "renderdoc_1.45/bin/renderdoccmd": hashlib.sha256(command_bytes).hexdigest(),
        "renderdoc_1.45/share/renderdoc/pylibs/lib/python3.6/__pycache__/json.cpython-36.pyc": (
            hashlib.sha256(b"managed-bytecode").hexdigest()
        ),
        "renderdoc_1.45/share/renderdoc/.dcc-mcp-renderdoc.json": hashlib.sha256(
            nested_receipt_bytes
        ).hexdigest(),
    }
    assert any(Path(arguments[-1]).name == "_runtime_probe.py" for arguments in probes)
    assert receipt["qrenderdoc"] == "renderdoc_1.45/bin/qrenderdoc"
    assert old_managed.is_dir()
    assert unmanaged.is_dir()

    runtime_downloader._cleanup_superseded(root, installed.parents[2])
    assert old_managed.exists() is False

    monkeypatch.setattr(
        runtime_downloader.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("verified cache should be reused"),
    )
    assert runtime_downloader.download_pinned() == installed

    nested = installed.parents[1] / "share/renderdoc/.dcc-mcp-renderdoc.json"
    nested.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="integrity receipt"):
        runtime_downloader.download_pinned()


@pytest.mark.parametrize("invalid_kind", ["weak", "forged", "escaped", "tampered", "unowned"])
def test_cleanup_preserves_unproven_superseded_candidate(tmp_path, invalid_kind):
    root = tmp_path / "renderdoc"
    checksum = "1" * 64
    victim = root / f"1.44-{checksum[:12]}"
    command = victim / "bin/renderdoccmd"
    qrenderdoc = command.with_name("qrenderdoc")
    command.parent.mkdir(parents=True)
    command.write_bytes(b"operator-renderdoc")
    qrenderdoc.write_bytes(b"operator-qrenderdoc")
    metadata = {
        "schema_version": 1,
        "version": "1.44",
        "url": "https://renderdoc.org/stable/1.44/renderdoc_1.44.tar.gz",
        "sha256": checksum,
        "command": "bin/renderdoccmd",
        "qrenderdoc": "bin/qrenderdoc",
        "files": {
            "bin/renderdoccmd": hashlib.sha256(b"operator-renderdoc").hexdigest(),
            "bin/qrenderdoc": hashlib.sha256(b"operator-qrenderdoc").hexdigest(),
        },
        "probe": {"qrenderdoc_python_probe": "loaded"},
    }
    if invalid_kind == "weak":
        metadata = {key: metadata[key] for key in ("schema_version", "version", "sha256")}
    elif invalid_kind == "forged":
        metadata["url"] = "https://example.invalid/operator.tar.gz"
    elif invalid_kind == "escaped":
        metadata["command"] = "../renderdoccmd"
        metadata["qrenderdoc"] = "../qrenderdoc"
    elif invalid_kind == "tampered":
        metadata["files"]["bin/renderdoccmd"] = "0" * 64
    else:
        (victim / "operator-notes.txt").write_text("keep me", encoding="utf-8")
    (victim / ".dcc-mcp-renderdoc.json").write_text(json.dumps(metadata), encoding="utf-8")
    keep = root / "1.45-222222222222"
    keep.mkdir()

    runtime_downloader._cleanup_superseded(root, keep)

    assert victim.is_dir()
    assert command.read_bytes() == b"operator-renderdoc"


def test_failed_qrenderdoc_python_probe_preserves_prior_managed_version(monkeypatch, tmp_path):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as bundle:
        for relative in ("renderdoc_1.45/bin/renderdoccmd", "renderdoc_1.45/bin/qrenderdoc"):
            member = tarfile.TarInfo(relative)
            member.mode = 0o755
            member.size = len(b"\x7fELF")
            bundle.addfile(member, io.BytesIO(b"\x7fELF"))
    archive = payload.getvalue()
    checksum = hashlib.sha256(archive).hexdigest()
    cache = tmp_path / "cache"
    root = cache / "renderdoc"
    old_checksum = "1" * 64
    old_managed = root / f"1.44-{old_checksum[:12]}"
    old_managed.mkdir(parents=True)
    (old_managed / ".dcc-mcp-renderdoc.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "1.44",
                "sha256": old_checksum,
            }
        ),
        encoding="utf-8",
    )
    url = "https://renderdoc.org/stable/1.45/renderdoc_1.45.tar.gz"
    monkeypatch.setenv("DCC_MCP_RUNTIME_CACHE", str(cache))
    monkeypatch.setenv("DCC_MCP_RENDERDOC_VERSION", "1.45")
    monkeypatch.setenv("DCC_MCP_RENDERDOC_URL", url)
    monkeypatch.setenv("DCC_MCP_RENDERDOC_SHA256", checksum)
    monkeypatch.setattr(runtime_downloader.sys, "platform", "linux")
    monkeypatch.setattr(
        runtime_downloader.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(archive, url=url, content_length=len(archive)),
    )
    monkeypatch.setattr(
        runtime_downloader,
        "run_owned_process",
        lambda args, **_kwargs: OwnedProcessResult(
            1 if "--python" in args else 0,
            (
                "qrenderdoc v1.45"
                if any(Path(argument).name == "qrenderdoc" for argument in args)
                else "renderdoccmd v1.45"
            ),
            "embedded Python failed" if "--python" in args else "",
            False,
            False,
        ),
    )

    with pytest.raises(RuntimeError, match="qrenderdoc embedded-Python probe"):
        runtime_downloader.download_pinned()

    assert old_managed.is_dir()
    assert not (root / f"1.45-{checksum[:12]}").exists()


def test_runtime_probe_rejects_version_echo_scripts_before_execution(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd"
    qrenderdoc = tmp_path / "qrenderdoc"
    command.write_text("#!/bin/sh\necho renderdoccmd v1.45\n", encoding="utf-8")
    qrenderdoc.write_text("#!/bin/sh\necho qrenderdoc v1.45\n", encoding="utf-8")
    monkeypatch.setattr(
        runtime_downloader,
        "run_owned_process",
        lambda *_args, **_kwargs: pytest.fail("non-native placeholders must not execute"),
    )

    with pytest.raises(RuntimeError, match="not a native ELF binary"):
        runtime_downloader.probe_runtime(command)


def test_runtime_version_probes_share_owned_process_runner(monkeypatch, tmp_path):
    command = tmp_path / "renderdoccmd.exe"
    qrenderdoc = tmp_path / "qrenderdoc.exe"
    command.write_bytes(b"MZrenderdoccmd")
    qrenderdoc.write_bytes(b"MZqrenderdoc")
    calls = []

    def owned_probe(argv, *, timeout_secs, env=None):
        calls.append((tuple(argv), timeout_secs, env))
        label = Path(argv[0]).stem
        output = "{} v1.45".format(label)
        return OwnedProcessResult(0, output, "", False, False)

    monkeypatch.setattr(runtime_downloader.sys, "platform", "win32")
    monkeypatch.setattr(runtime_downloader, "run_owned_process", owned_probe, raising=False)
    result = runtime_downloader.probe_runtime(command, expected_version="1.45")

    assert result["renderdoccmd_version"] == "1.45"
    assert result["qrenderdoc_version"] == "1.45"
    assert [Path(call[0][0]).name for call in calls] == ["renderdoccmd.exe", "qrenderdoc.exe"]
    assert [call[1] for call in calls] == [15, 15]


def test_embedded_python_probe_uses_owned_runner_and_status_marker(monkeypatch, tmp_path):
    qrenderdoc = tmp_path / "qrenderdoc.exe"
    qrenderdoc.write_bytes(b"MZqrenderdoc")
    calls = []

    def owned_probe(argv, *, timeout_secs, env=None):
        calls.append((tuple(argv), timeout_secs, env))
        Path(env["DCC_MCP_RENDERDOC_PROBE_STATUS"]).write_text(
            runtime_downloader.QRENDERDOC_PYTHON_PROBE_MARKER,
            encoding="utf-8",
        )
        return OwnedProcessResult(0, "", "", False, False)

    monkeypatch.setattr(runtime_downloader.sys, "platform", "win32")
    monkeypatch.setattr(runtime_downloader, "run_owned_process", owned_probe)

    runtime_downloader.probe_qrenderdoc_python(qrenderdoc)

    assert len(calls) == 1
    assert calls[0][0][:2] == (str(qrenderdoc), "--python")
    assert Path(calls[0][0][2]).name == "_runtime_probe.py"
    assert calls[0][1] == 30
    assert calls[0][2]["APPDATA"]
    assert calls[0][2]["LOCALAPPDATA"]


def test_owned_probe_timeout_errors_are_stable_and_sanitized(monkeypatch, tmp_path):
    command = tmp_path / "operator-secret-renderdoccmd.exe"
    qrenderdoc = command.with_name("qrenderdoc.exe")
    command.write_bytes(b"MZrenderdoccmd")
    qrenderdoc.write_bytes(b"MZqrenderdoc")
    secret = str(tmp_path / "private-stderr.txt")

    def timeout_probe(*_args, **_kwargs):
        raise OwnedProcessTimeoutError("timeout while reading {}".format(secret))

    monkeypatch.setattr(runtime_downloader.sys, "platform", "win32")
    monkeypatch.setattr(runtime_downloader, "run_owned_process", timeout_probe)

    with pytest.raises(RuntimeError) as runtime_error:
        runtime_downloader.probe_runtime(command)
    with pytest.raises(RuntimeError) as python_error:
        runtime_downloader.probe_qrenderdoc_python(qrenderdoc)

    assert str(runtime_error.value) == "renderdoccmd probe failed: process_timeout"
    assert str(python_error.value) == ("qrenderdoc embedded-Python probe failed: process_timeout")
    assert secret not in str(runtime_error.value)
    assert secret not in str(python_error.value)
