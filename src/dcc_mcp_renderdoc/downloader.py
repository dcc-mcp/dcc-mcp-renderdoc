"""Download a pinned RenderDoc bundle into a verified, managed cache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 25_000
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class RenderDocBundle:
    """An operator-approved immutable RenderDoc bundle."""

    version: str
    url: str
    sha256: str
    command_name: str


PINNED_VERSION = "1.45"
# These digests were recorded only after the upstream detached signatures were verified with
# RenderDoc's GitHub-published signing key (fingerprint
# 1B039DB9A4718A2D699DE031AC612C3120C34695).
PINNED_BUNDLES = {
    "win32": RenderDocBundle(
        version=PINNED_VERSION,
        url=f"https://renderdoc.org/stable/{PINNED_VERSION}/RenderDoc_{PINNED_VERSION}_64.zip",
        sha256="bd665c348a8245d10a1f513e35b83603edc1a78006277583d09ec0769286eea4",
        command_name="renderdoccmd.exe",
    ),
    "linux": RenderDocBundle(
        version=PINNED_VERSION,
        url=f"https://renderdoc.org/stable/{PINNED_VERSION}/renderdoc_{PINNED_VERSION}.tar.gz",
        sha256="b0a7ee8ec78c4fa511eb44137380d99a748472e5fd24c877f8afcc860a172a42",
        command_name="renderdoccmd",
    ),
}


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


def _copy_archive_member(source, target: Path, declared_size: int, extracted: list[int]) -> None:
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with source, target.open("xb") as stream:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            extracted[0] += len(chunk)
            if written > MAX_MEMBER_BYTES:
                raise RuntimeError("RenderDoc archive member size exceeds the bounded limit")
            if extracted[0] > MAX_EXTRACTED_BYTES:
                raise RuntimeError("RenderDoc archive expanded size exceeds the bounded limit")
            stream.write(chunk)
    if written != declared_size:
        raise RuntimeError("RenderDoc archive member size does not match its declaration")


def _extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise RuntimeError("RenderDoc archive member count exceeds the bounded limit")
            total_size = sum(member.file_size for member in members)
            if any(member.file_size > MAX_MEMBER_BYTES for member in members):
                raise RuntimeError("RenderDoc archive member size exceeds the bounded limit")
            if total_size > MAX_EXTRACTED_BYTES:
                raise RuntimeError("RenderDoc archive expanded size exceeds the bounded limit")
            extracted = [0]
            targets: set[Path] = set()
            for member in members:
                target = _safe_destination(destination, member.filename)
                if target in targets:
                    raise RuntimeError(
                        f"Archive contains a duplicate destination: {member.filename}"
                    )
                targets.add(target)
                member_type = stat.S_IFMT(member.external_attr >> 16)
                if member_type == stat.S_IFLNK:
                    raise RuntimeError(f"Archive links are not accepted: {member.filename}")
                if member.flag_bits & 0x1:
                    raise RuntimeError(
                        f"Encrypted archive members are not accepted: {member.filename}"
                    )
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member_type not in {0, stat.S_IFREG}:
                    raise RuntimeError(
                        f"Archive special entries are not accepted: {member.filename}"
                    )
                _copy_archive_member(bundle.open(member), target, member.file_size, extracted)
        return
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise RuntimeError("RenderDoc archive member count exceeds the bounded limit")
        files = [member for member in members if member.isfile()]
        if any(member.size > MAX_MEMBER_BYTES for member in files):
            raise RuntimeError("RenderDoc archive member size exceeds the bounded limit")
        if sum(member.size for member in files) > MAX_EXTRACTED_BYTES:
            raise RuntimeError("RenderDoc archive expanded size exceeds the bounded limit")
        extracted = [0]
        targets: set[Path] = set()
        for member in members:
            target = _safe_destination(destination, member.name)
            if target in targets:
                raise RuntimeError(f"Archive contains a duplicate destination: {member.name}")
            targets.add(target)
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"Archive special entries are not accepted: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError(f"Archive file could not be read: {member.name}")
            _copy_archive_member(source, target, member.size, extracted)
            target.chmod(member.mode & 0o777)


def _platform_key(platform: str) -> str:
    if platform == "win32":
        return "win32"
    if platform.startswith("linux"):
        return "linux"
    raise RuntimeError(f"RenderDoc has no supported desktop bundle for {platform}")


def _validate_bundle(bundle: RenderDocBundle, platform: str) -> RenderDocBundle:
    platform_key = _platform_key(platform)
    if re.fullmatch(r"[0-9]+\.[0-9]+", bundle.version) is None:
        raise RuntimeError("Pinned RenderDoc version must use the major.minor form")
    checksum = bundle.sha256.strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise RuntimeError("Pinned RenderDoc checksum must be a SHA-256 hex digest")

    if platform_key == "win32":
        archive_name = f"RenderDoc_{bundle.version}_64.zip"
        command_name = "renderdoccmd.exe"
    else:
        archive_name = f"renderdoc_{bundle.version}.tar.gz"
        command_name = "renderdoccmd"
    expected_path = f"/stable/{bundle.version}/{archive_name}"
    parsed = urlparse(bundle.url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "renderdoc.org"
        or parsed.port is not None
        or parsed.path != expected_path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or bundle.command_name != command_name
    ):
        raise RuntimeError(
            "Pinned RenderDoc URL must be the exact official stable bundle for its version"
        )
    return RenderDocBundle(
        version=bundle.version,
        url=bundle.url,
        sha256=checksum,
        command_name=command_name,
    )


def _configured_bundle(platform: str | None = None) -> RenderDocBundle:
    platform = platform or sys.platform
    version = os.environ.get("DCC_MCP_RENDERDOC_VERSION", "").strip()
    url = os.environ.get("DCC_MCP_RENDERDOC_URL", "").strip()
    checksum = os.environ.get("DCC_MCP_RENDERDOC_SHA256", "").strip().casefold()
    configured = (version, url, checksum)
    if any(configured) and not all(configured):
        raise RuntimeError(
            "An automatic download override requires a pinned RenderDoc bundle; set "
            "DCC_MCP_RENDERDOC_VERSION, DCC_MCP_RENDERDOC_URL, and "
            "DCC_MCP_RENDERDOC_SHA256"
        )
    platform_key = _platform_key(platform)
    if not any(configured):
        return PINNED_BUNDLES[platform_key]
    command_name = "renderdoccmd.exe" if platform_key == "win32" else "renderdoccmd"
    return _validate_bundle(
        RenderDocBundle(
            version=version,
            url=url,
            sha256=checksum,
            command_name=command_name,
        ),
        platform,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parsed_version(output: str) -> str | None:
    match = re.search(r"(?<![0-9])v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)", output)
    return match.group(1) if match else None


def _require_native_executable(path: Path, label: str) -> None:
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
    except OSError as exc:
        raise RuntimeError(f"{label} probe failed: executable could not be read") from exc
    if path.name.casefold().endswith(".exe"):
        valid = magic.startswith(b"MZ")
        expected = "PE"
    else:
        valid = magic == b"\x7fELF"
        expected = "ELF"
    if not valid:
        raise RuntimeError(f"{label} probe failed: executable is not a native {expected} binary")


def probe_runtime(command: Path, *, expected_version: str | None = None) -> dict[str, str]:
    """Run bounded, read-only loadability probes for one paired RenderDoc runtime."""
    qrenderdoc_name = "qrenderdoc.exe" if command.name.casefold().endswith(".exe") else "qrenderdoc"
    qrenderdoc = command.with_name(qrenderdoc_name)
    if not command.is_file():
        raise RuntimeError("renderdoccmd probe failed: executable is missing")
    if not qrenderdoc.is_file():
        raise RuntimeError("qrenderdoc probe failed: paired executable is missing")
    _require_native_executable(command, "renderdoccmd")
    _require_native_executable(qrenderdoc, "qrenderdoc")

    qrenderdoc_argv = [str(qrenderdoc), "--version"]
    qrenderdoc_probe = "direct"
    if (
        sys.platform.startswith("linux")
        and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        and shutil.which("xvfb-run")
    ):
        qrenderdoc_argv = [str(shutil.which("xvfb-run")), "-a", *qrenderdoc_argv]
        qrenderdoc_probe = "xvfb"

    versions: dict[str, str] = {}
    for label, argv in (
        ("renderdoccmd", [str(command), "version"]),
        ("qrenderdoc", qrenderdoc_argv),
    ):
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"{label} probe failed: {type(exc).__name__}") from exc
        if completed.returncode != 0:
            raise RuntimeError(f"{label} probe failed with exit {completed.returncode}")
        output = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip()
        )
        version = _parsed_version(output)
        if version is None:
            raise RuntimeError(f"{label} probe returned no parseable version")
        versions[label] = version

    if (
        expected_version is not None
        and not versions["renderdoccmd"].startswith(f"{expected_version}.")
        and versions["renderdoccmd"] != expected_version
    ):
        raise RuntimeError("renderdoccmd probe version does not match the pinned bundle")
    if versions["qrenderdoc"].split(".")[:2] != versions["renderdoccmd"].split(".")[:2]:
        raise RuntimeError("qrenderdoc probe version does not match renderdoccmd")
    return {
        "renderdoccmd_version": versions["renderdoccmd"],
        "qrenderdoc_version": versions["qrenderdoc"],
        "qrenderdoc_probe": qrenderdoc_probe,
    }


def _owned_files(root: Path) -> dict[str, str]:
    receipt_name = ".dcc-mcp-renderdoc.json"
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"Managed RenderDoc payload contains a link: {path.name}")
        if path.is_file() and path.name != receipt_name:
            relative = path.relative_to(root).as_posix()
            files[relative] = _sha256_file(path)
    return files


def _receipt_command(destination: Path, bundle: RenderDocBundle) -> Path | None:
    receipt_path = destination / ".dcc-mcp-renderdoc.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expected = {
        "schema_version": 1,
        "version": bundle.version,
        "url": bundle.url,
        "sha256": bundle.sha256,
    }
    if not isinstance(receipt, dict) or any(
        receipt.get(key) != value for key, value in expected.items()
    ):
        return None
    command_relative = receipt.get("command")
    qrenderdoc_relative = receipt.get("qrenderdoc")
    files = receipt.get("files")
    if (
        not isinstance(command_relative, str)
        or not isinstance(qrenderdoc_relative, str)
        or not isinstance(files, dict)
        or not files
    ):
        return None
    command = _safe_destination(destination, command_relative)
    qrenderdoc = _safe_destination(destination, qrenderdoc_relative)
    if not command.is_file() or not qrenderdoc.is_file() or qrenderdoc.parent != command.parent:
        return None
    try:
        actual = _owned_files(destination)
    except (OSError, RuntimeError):
        return None
    if actual != files:
        return None
    return command.resolve()


def _download_verified(bundle: RenderDocBundle, archive: Path) -> None:
    request = urllib.request.Request(bundle.url, headers={"User-Agent": "dcc-mcp-renderdoc"})
    digest = hashlib.sha256()
    with (
        urllib.request.urlopen(request, timeout=120) as response,
        archive.open("xb") as stream,
    ):
        final_url = response.geturl() if hasattr(response, "geturl") else bundle.url
        requested = urlparse(bundle.url)
        final = urlparse(final_url)
        if (
            final.scheme != "https"
            or final.hostname != requested.hostname
            or final.port is not None
            or final.username is not None
            or final.password is not None
        ):
            raise RuntimeError(
                "RenderDoc final download origin must remain the pinned HTTPS origin"
            )
        content_length = getattr(response, "headers", {}).get("Content-Length")
        if content_length is not None:
            try:
                expected_bytes = int(content_length)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("RenderDoc Content-Length is invalid") from exc
            if expected_bytes < 0 or expected_bytes > MAX_DOWNLOAD_BYTES:
                raise RuntimeError("RenderDoc Content-Length exceeds the bounded download limit")
        downloaded = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            downloaded += len(chunk)
            if downloaded > MAX_DOWNLOAD_BYTES:
                raise RuntimeError("RenderDoc download exceeds the bounded byte limit")
            digest.update(chunk)
            stream.write(chunk)
        if content_length is not None and downloaded != expected_bytes:
            raise RuntimeError("RenderDoc download size does not match Content-Length")
    if digest.hexdigest() != bundle.sha256:
        raise RuntimeError("RenderDoc bundle checksum mismatch; refusing to populate the cache")


def _cleanup_superseded(root: Path, keep: Path) -> None:
    for candidate in root.iterdir():
        if candidate == keep or candidate.is_symlink() or not candidate.is_dir():
            continue
        receipt = candidate / ".dcc-mcp-renderdoc.json"
        try:
            metadata = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        version = metadata.get("version") if isinstance(metadata, dict) else None
        checksum = metadata.get("sha256") if isinstance(metadata, dict) else None
        if (
            isinstance(metadata, dict)
            and metadata.get("schema_version") == 1
            and isinstance(version, str)
            and isinstance(checksum, str)
            and re.fullmatch(r"[0-9]+\.[0-9]+", version) is not None
            and re.fullmatch(r"[0-9a-f]{64}", checksum) is not None
            and candidate.name == f"{version}-{checksum[:12]}"
        ):
            shutil.rmtree(candidate)


def download_pinned(bundle: RenderDocBundle | None = None) -> Path:
    """Download, verify, and atomically cache an explicitly pinned bundle."""
    selected = _validate_bundle(bundle, sys.platform) if bundle else _configured_bundle()
    root = _cache_root()
    destination = root / f"{selected.version}-{selected.sha256[:12]}"
    existing = _receipt_command(destination, selected) if destination.exists() else None
    if existing is not None:
        probe_runtime(existing, expected_version=selected.version)
        return existing
    if destination.exists():
        raise RuntimeError(
            "RenderDoc cache destination exists without a matching integrity receipt; "
            "remove it before retrying"
        )

    root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    archive = root / f".download-{token}.part"
    staging = root / f".install-{token}"
    try:
        _download_verified(selected, archive)
        _extract(archive, staging)
        command = next(staging.rglob(selected.command_name), None)
        if command is None:
            raise RuntimeError(f"Verified RenderDoc bundle did not contain {selected.command_name}")
        qrenderdoc_name = (
            "qrenderdoc.exe" if selected.command_name.endswith(".exe") else "qrenderdoc"
        )
        qrenderdoc = command.with_name(qrenderdoc_name)
        if not qrenderdoc.is_file():
            raise RuntimeError(
                f"Verified RenderDoc bundle did not contain matching {qrenderdoc_name}"
            )
        if sys.platform.startswith("linux"):
            command.chmod(command.stat().st_mode | 0o111)
            qrenderdoc.chmod(qrenderdoc.stat().st_mode | 0o111)
        probe = probe_runtime(command, expected_version=selected.version)
        command_relative = command.relative_to(staging).as_posix()
        qrenderdoc_relative = qrenderdoc.relative_to(staging).as_posix()
        receipt = {
            "schema_version": 1,
            "version": selected.version,
            "url": selected.url,
            "sha256": selected.sha256,
            "command": command_relative,
            "qrenderdoc": qrenderdoc_relative,
            "files": _owned_files(staging),
            "probe": probe,
        }
        (staging / ".dcc-mcp-renderdoc.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(destination)
        installed = destination / Path(command_relative)
        return installed.resolve()
    finally:
        archive.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging)
        try:
            root.rmdir()
        except OSError:
            pass
