"""Download the latest stable RenderDoc bundle from the official builds page."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

BUILDS_URL = "https://renderdoc.org/builds"


def latest_version(html: str) -> str:
    versions = set(re.findall(r"/stable/([0-9]+\.[0-9]+)/", html))
    if not versions:
        raise RuntimeError("No stable RenderDoc versions found on the official builds page")
    return max(versions, key=lambda value: tuple(int(part) for part in value.split(".")))


def _safe_destination(root: Path, member_name: str) -> Path:
    destination = (root / member_name).resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Archive member escapes destination: {member_name}") from exc
    return destination


def extract_archive(archive: Path, destination: Path) -> None:
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


def download_latest(output: Path, platform: str = sys.platform) -> Path:
    request = urllib.request.Request(BUILDS_URL, headers={"User-Agent": "dcc-mcp-renderdoc"})
    with urllib.request.urlopen(request, timeout=30) as response:
        version = latest_version(response.read().decode("utf-8"))
    if platform == "win32":
        archive_name = f"RenderDoc_{version}_64.zip"
    elif platform.startswith("linux"):
        archive_name = f"renderdoc_{version}.tar.gz"
    else:
        raise RuntimeError(f"RenderDoc has no supported desktop bundle for {platform}")

    version_dir = output.resolve() / version
    command_name = "renderdoccmd.exe" if platform == "win32" else "renderdoccmd"
    existing = next(version_dir.rglob(command_name), None) if version_dir.exists() else None
    if existing:
        return existing.resolve()

    output.mkdir(parents=True, exist_ok=True)
    archive = output / archive_name
    url = f"https://renderdoc.org/stable/{version}/{archive_name}"
    with urllib.request.urlopen(url, timeout=120) as response, archive.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    extract_archive(archive, version_dir)
    archive.unlink()
    command = next(version_dir.rglob(command_name), None)
    if command is None:
        raise RuntimeError(f"Downloaded bundle did not contain {command_name}")
    if platform.startswith("linux"):
        command.chmod(command.stat().st_mode | 0o111)
    return command.resolve()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(".renderdoc-bin"))
    args = parser.parse_args()
    print(download_latest(args.output))


if __name__ == "__main__":
    main()
