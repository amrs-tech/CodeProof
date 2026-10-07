"""Bounded ingestion of untrusted archives and public GitHub repositories.

Source is data: no Git checkout, hooks, submodules, installers, or source commands run here.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import time
import unicodedata
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urljoin, urlsplit

import httpx

from codeproof.config import Settings

_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".aws",
        ".ssh",
        ".azure",
        ".gnupg",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "dist",
        "target",
        ".tox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "coverage",
        ".next",
    }
)
_SECRET_NAMES = frozenset(
    {
        "credentials",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        ".npmrc",
        ".pypirc",
        ".netrc",
    }
)
_DEVICE_NAME = re.compile(r"^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³]|conin\$|conout\$)$", re.I)
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_GITHUB_HOSTS = frozenset({"github.com", "api.github.com", "codeload.github.com"})
_MAX_RATIO = 100
_DOWNLOAD_SECONDS = 30
_SECRET_CONTENT = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b|"
    r"\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*['\"]"
    r"(?![<${]|example|placeholder|changeme|test|dummy)[^'\"\r\n]{8,}['\"]",
    re.I,
)


class IngestionError(ValueError):
    """A source cannot be ingested safely, with a safe user-facing message."""


def is_excluded_path(path: str | PurePosixPath) -> bool:
    """Exclude source-control metadata, generated content, and credential files."""
    parts = PurePosixPath(path).parts
    if any(part.casefold() in _EXCLUDED_DIRS for part in parts):
        return True
    name = parts[-1].casefold() if parts else ""
    return (
        name in _SECRET_NAMES
        or name.startswith(".env")
        or name.endswith((".pem", ".key", ".p12", ".pfx", ".keystore"))
    )


def looks_like_secret(text: str) -> bool:
    """Conservatively keep obvious credentials out of the context index."""
    return bool(_SECRET_CONTENT.search(text))


def _safe_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    name = info.orig_filename
    if not name or "\\" in name or name.startswith("/") or "\x00" in name:
        raise IngestionError("Archive contains an unsafe file path.")
    name = unicodedata.normalize("NFC", name)
    relative = name[:-1] if name.endswith("/") else name
    parts = tuple(relative.split("/"))
    if len(relative) > 240 or len(parts) > 30:
        raise IngestionError("Archive contains an excessively long file path.")
    for part in parts:
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or any(unicodedata.category(char) == "Cc" or char in ':<>"|?*' for char in part)
            or _DEVICE_NAME.fullmatch(part.split(".", 1)[0])
        ):
            raise IngestionError("Archive contains an unsafe file path.")
    return parts


def _validate_entries(
    archive: zipfile.ZipFile, settings: Settings
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
    infos = archive.infolist()
    if not infos or len(infos) > settings.max_files * 3:
        raise IngestionError("Archive is empty or contains too many entries.")
    entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    seen: set[tuple[str, ...]] = set()
    spellings: dict[tuple[str, ...], tuple[str, ...]] = {}
    file_paths: set[tuple[str, ...]] = set()
    total_bytes = 0
    for info in infos:
        parts = _safe_parts(info)
        folded = tuple(part.casefold() for part in parts)
        if folded in seen:
            raise IngestionError("Archive contains duplicate or case-conflicting paths.")
        seen.add(folded)
        for index in range(1, len(parts) + 1):
            prefix, spelling = folded[:index], parts[:index]
            if prefix in spellings and spellings[prefix] != spelling:
                raise IngestionError("Archive contains case-conflicting directory paths.")
            spellings[prefix] = spelling
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise IngestionError("Archive links and special files are not allowed.")
        if info.flag_bits & 1:
            raise IngestionError("Encrypted archive entries are not supported.")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise IngestionError("Archive uses an unsupported compression method.")
        if not info.is_dir():
            file_paths.add(folded)
            if len(file_paths) > settings.max_files:
                raise IngestionError("Archive exceeds the file-count limit.")
            if info.file_size > settings.max_file_bytes:
                raise IngestionError("Archive contains a file exceeding the per-file size limit.")
            total_bytes += info.file_size
            if total_bytes > settings.max_extracted_bytes:
                raise IngestionError("Archive exceeds the extracted-size limit.")
            if info.file_size > max(1, info.compress_size) * _MAX_RATIO:
                raise IngestionError("Archive exceeds the safe compression-ratio limit.")
        entries.append((info, parts))
    for _, parts in entries:
        folded = tuple(part.casefold() for part in parts)
        if any(folded[:index] in file_paths for index in range(1, len(folded))):
            raise IngestionError("Archive contains conflicting file and directory paths.")
    if not file_paths:
        raise IngestionError("Archive contains no source files.")
    first_root = entries[0][1][0]
    if (
        not is_excluded_path(first_root)
        and all(parts[0] == first_root for _, parts in entries)
        and all(info.is_dir() or len(parts) > 1 for info, parts in entries)
    ):
        entries = [(info, parts[1:]) for info, parts in entries if len(parts) > 1]
    return entries


def ingest_zip(archive: Path, destination: Path, settings: Settings) -> dict:
    """Validate everything, stream privately, then publish to an exclusive destination."""
    archive = Path(archive)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise IngestionError("The destination already exists; choose a new isolated workspace.")
    if not archive.is_file() or archive.stat().st_size > settings.max_upload_bytes:
        raise IngestionError("Source archive is missing or exceeds the upload-size limit.")
    staging: Path | None = None
    publication: Path | None = None
    try:
        with zipfile.ZipFile(archive) as source:
            entries = _validate_entries(source, settings)
            destination.parent.mkdir(parents=True, exist_ok=True)
            parent = destination.parent.resolve(strict=True)
            # A private sibling makes partially extracted runs invisible to the worker.
            staging = Path(tempfile.mkdtemp(prefix=".ingest-", dir=parent))
            written_bytes = 0
            files = 0
            excluded: list[str] = []
            for info, parts in entries:
                relative = PurePosixPath(*parts)
                if is_excluded_path(relative):
                    if not info.is_dir():
                        excluded.append(relative.as_posix())
                    continue
                target = staging.joinpath(*parts)
                if not target.resolve().is_relative_to(staging.resolve()):
                    raise IngestionError("Archive path escapes its isolated workspace.")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                file_bytes = 0
                with source.open(info) as reader, target.open("xb") as writer:
                    while block := reader.read(64 * 1024):
                        file_bytes += len(block)
                        written_bytes += len(block)
                        if (
                            file_bytes > settings.max_file_bytes
                            or written_bytes > settings.max_extracted_bytes
                            or file_bytes > info.file_size
                        ):
                            raise IngestionError("Archive exceeds a streamed extraction limit.")
                        writer.write(block)
                if file_bytes != info.file_size:
                    raise IngestionError("Archive entry size does not match its metadata.")
                files += 1
            if not files:
                raise IngestionError("Archive contains no eligible source files after exclusions.")
            # mkdir is an exclusive claim on every supported OS. A directory rename
            # alone could overwrite a competing empty destination on POSIX systems.
            publication_target = parent / destination.name
            publication_target.mkdir(exist_ok=False)
            publication = publication_target
            for child in staging.iterdir():
                os.rename(child, publication / child.name)
            staging.rmdir()
            staging = None
            publication = None
            return {"files": files, "bytes": written_bytes, "excluded": excluded}
    except IngestionError:
        raise
    except (
        OSError,
        zipfile.BadZipFile,
        RuntimeError,
        EOFError,
        NotImplementedError,
        UnicodeError,
        zlib.error,
    ) as exc:
        raise IngestionError(
            "Archive is corrupt, unreadable, or could not be safely extracted."
        ) from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if publication is not None:
            shutil.rmtree(publication, ignore_errors=True)


def parse_github_reference(reference: str) -> tuple[str, str]:
    """Accept exactly owner/repository or its public HTTPS GitHub URL."""
    reference = reference.strip()
    if "://" in reference:
        parsed = urlsplit(reference)
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "github.com"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise IngestionError(
                "Use an owner/repository name or a public HTTPS GitHub repository URL."
            )
        reference = parsed.path.strip("/")
    if reference.endswith(".git"):
        reference = reference[:-4]
    parts = reference.split("/")
    if (
        len(parts) != 2
        or not _OWNER.fullmatch(parts[0])
        or not _REPO.fullmatch(parts[1])
        or parts[1] in {".", ".."}
    ):
        raise IngestionError("GitHub repository must be identified as owner/repository.")
    return parts[0], parts[1]


def _check_github_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _GITHUB_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise IngestionError("GitHub download redirected outside the approved HTTPS hosts.")


def _download(
    client: httpx.Client,
    url: str,
    destination: Path,
    maximum: int,
    deadline: float,
    accept: str | None = None,
) -> None:
    """Bound redirects, decompressed bytes, per-operation waits, and overall duration."""
    for _ in range(4):
        _check_github_url(url)
        if time.monotonic() >= deadline:
            raise IngestionError("GitHub download exceeded the total time limit.")
        with client.stream("GET", url, headers={"Accept": accept} if accept else None) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise IngestionError("GitHub returned an invalid redirect.")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise IngestionError("Public GitHub repository could not be downloaded.")
            content_length = response.headers.get("content-length")
            if content_length and (not content_length.isdecimal() or int(content_length) > maximum):
                raise IngestionError("GitHub response exceeds the allowed download size.")
            size = 0
            with destination.open("wb") as writer:
                for block in response.iter_bytes(chunk_size=64 * 1024):
                    size += len(block)
                    if size > maximum or time.monotonic() >= deadline:
                        raise IngestionError("GitHub download exceeded the size or time limit.")
                    writer.write(block)
            return
    raise IngestionError("GitHub download exceeded the redirect limit.")


def ingest_github(reference: str, destination: Path, settings: Settings) -> dict:
    """Download the default-branch archive without credentials or Git execution."""
    owner, repository = parse_github_reference(reference)
    import json

    try:
        with tempfile.TemporaryDirectory(prefix="codeproof-download-") as temporary:
            download_dir = Path(temporary)
            metadata_path = download_dir / "repository.json"
            revision_path = download_dir / "revision.txt"
            archive_path = download_dir / "source.zip"
            deadline = time.monotonic() + _DOWNLOAD_SECONDS
            # trust_env=False prevents environment proxies and ambient credentials.
            with httpx.Client(
                timeout=httpx.Timeout(5.0, connect=5.0),
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "CodeProof/0.1", "Accept": "application/vnd.github+json"},
            ) as client:
                _download(
                    client,
                    f"https://api.github.com/repos/{owner}/{repository}",
                    metadata_path,
                    256 * 1024,
                    deadline,
                )
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict):
                    raise IngestionError("GitHub returned invalid repository metadata.")
                branch = metadata.get("default_branch")
                if (
                    metadata.get("private") is not False
                    or not isinstance(branch, str)
                    or not branch
                    or len(branch) > 200
                    or any(ord(character) < 32 for character in branch)
                ):
                    raise IngestionError(
                        "Repository must be public and expose a valid default branch."
                    )
                # The SHA media type avoids downloading a potentially enormous
                # commit's file diffs simply to determine its immutable revision.
                _download(
                    client,
                    f"https://api.github.com/repos/{owner}/{repository}/commits/{quote(branch, safe='')}",
                    revision_path,
                    1024,
                    deadline,
                    accept="application/vnd.github.sha",
                )
                revision = revision_path.read_text(encoding="ascii").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{40}", revision):
                    raise IngestionError("GitHub returned an invalid immutable commit revision.")
                _download(
                    client,
                    f"https://codeload.github.com/{owner}/{repository}/zip/{revision}",
                    archive_path,
                    settings.max_upload_bytes,
                    deadline,
                )
            result = ingest_zip(archive_path, destination, settings)
            result.update(
                {
                    "type": "github",
                    "repository": f"{owner}/{repository}",
                    "reference": f"{owner}/{repository}",
                    "revision": revision,
                    "branch": branch,
                }
            )
            return result
    except IngestionError:
        raise
    except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
        raise IngestionError(
            "GitHub source could not be retrieved within the configured limits."
        ) from exc
