import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from dcc_mcp_renderdoc import downloader as runtime_downloader


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


def test_verified_download_replaces_only_superseded_managed_cache(monkeypatch, tmp_path):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as bundle:
        command = tarfile.TarInfo("renderdoc_1.45/bin/renderdoccmd")
        command.mode = 0o755
        command.size = len(b"renderdoc")
        bundle.addfile(command, io.BytesIO(b"renderdoc"))
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

    installed = runtime_downloader.download_pinned()
    receipt = json.loads((installed.parents[2] / ".dcc-mcp-renderdoc.json").read_text())

    assert installed.read_bytes() == b"renderdoc"
    assert receipt["sha256"] == checksum
    assert old_managed.exists() is False
    assert unmanaged.is_dir()

    monkeypatch.setattr(
        runtime_downloader.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("verified cache should be reused"),
    )
    assert runtime_downloader.download_pinned() == installed
