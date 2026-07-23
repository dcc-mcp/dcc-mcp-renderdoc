"""Download and cache the official RenderDoc command-line bundle."""

from __future__ import annotations

import os
import re
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path


def _cache_root() -> Path:
    configured = os.environ.get("DCC_MCP_RUNTIME_CACHE")
    if configured:
        return Path(configured).expanduser().resolve() / "renderdoc"
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return root / "dcc-mcp/renderdoc"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "dcc-mcp/renderdoc"


def _safe_destination(root: Path, member_name: str) -> Path:
    destination = (root / member_name).resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Archive member escapes destination: {member_name}") from exc
    return destination


def _extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                _safe_destination(destination, member.filename)
            bundle.extractall(destination)
        return
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            _safe_destination(destination, member.name)
            if member.issym() or member.islnk():
                raise RuntimeError(f"Archive links are not accepted: {member.name}")
        bundle.extractall(destination)


def download_latest() -> Path:
    request = urllib.request.Request(
        "https://renderdoc.org/builds", headers={"User-Agent": "dcc-mcp-renderdoc"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8")
    versions = set(re.findall(r"/stable/([0-9]+\.[0-9]+)/", html))
    if not versions:
        raise RuntimeError("No stable RenderDoc versions found on the official builds page")
    version = max(versions, key=lambda value: tuple(int(part) for part in value.split(".")))
    if sys.platform == "win32":
        archive_name, command_name = f"RenderDoc_{version}_64.zip", "renderdoccmd.exe"
    elif sys.platform.startswith("linux"):
        archive_name, command_name = f"renderdoc_{version}.tar.gz", "renderdoccmd"
    else:
        raise RuntimeError(f"RenderDoc has no supported desktop bundle for {sys.platform}")
    destination = _cache_root() / version
    existing = next(destination.rglob(command_name), None) if destination.exists() else None
    if existing:
        return existing.resolve()
    archive = _cache_root() / archive_name
    _cache_root().mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(
            f"https://renderdoc.org/stable/{version}/{archive_name}", timeout=120
        ) as response, archive.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        _extract(archive, destination)
    finally:
        archive.unlink(missing_ok=True)
    command = next(destination.rglob(command_name), None)
    if command is None:
        raise RuntimeError(f"Downloaded bundle did not contain {command_name}")
    if sys.platform.startswith("linux"):
        command.chmod(command.stat().st_mode | 0o111)
    return command.resolve()
