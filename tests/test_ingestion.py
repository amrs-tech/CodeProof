import io
import json
import stat
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from codeproof.config import Settings
from codeproof.ingestion import (
    IngestionError,
    ingest_github,
    ingest_zip,
    parse_github_reference,
)


def make_zip(path: Path, files: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name)
            # ZipInfo ordinarily rewrites native Windows separators. Preserve the
            # adversarial archive name so this test exercises the actual ZIP input.
            info.filename = name
            info.orig_filename = name
            archive.writestr(info, content)
    return path


def test_zip_normalizes_wrapper_and_excludes_sensitive_and_generated_files(tmp_path):
    archive = make_zip(
        tmp_path / "source.zip",
        {
            "repo/main.py": "print('hello')\n",
            "repo/.git/config": "git metadata",
            "repo/.env": "TOKEN=secret",
            "repo/nested/credentials.json": "{}",
            "repo/node_modules/library.js": "build content",
            "repo/server.pem": "private key",
        },
    )
    destination = tmp_path / "workspace"
    metadata = ingest_zip(archive, destination, Settings())
    assert metadata["files"] == 1
    assert metadata["bytes"] == len("print('hello')\n")
    assert len(metadata["excluded"]) == 5
    assert (destination / "main.py").read_text() == "print('hello')\n"
    assert not (destination / "repo").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../escape.py",
        "/absolute.py",
        "C:/escape.py",
        "nested/../../escape.py",
        "..\\escape.py",
        "nested\\main.py",
        "foo//main.py",
        "./main.py",
        "foo/CON.py",
        "LPT1",
        "COM².txt",
        "file.py.",
        "folder /main.py",
        "file:stream",
        "bad\x01.py",
    ],
)
def test_rejects_unsafe_paths_without_partial_destination(tmp_path, name):
    archive = make_zip(tmp_path / "source.zip", {"safe.py": "safe", name: "unsafe"})
    destination = tmp_path / "workspace"
    with pytest.raises(IngestionError, match="unsafe"):
        ingest_zip(archive, destination, Settings())
    assert not destination.exists()
    assert not list(tmp_path.glob(".ingest-*"))


@pytest.mark.parametrize(
    "names",
    [
        ("main.py", "MAIN.py"),
        ("é.py", "e\u0301.py"),
        ("source", "source/main.py"),
        ("a/main.py", "A/main.py"),
        ("a/one.py", "A/two.py"),
    ],
)
def test_rejects_portability_collisions(tmp_path, names):
    archive = make_zip(tmp_path / "source.zip", dict.fromkeys(names, "source"))
    with pytest.raises(IngestionError, match="conflict|duplicate"):
        ingest_zip(archive, tmp_path / "workspace", Settings())


def test_rejects_symlink(tmp_path):
    archive = tmp_path / "source.zip"
    info = zipfile.ZipInfo("link.py")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr(info, "../../outside.py")
    with pytest.raises(IngestionError, match="links"):
        ingest_zip(archive, tmp_path / "workspace", Settings())


def test_rejects_compression_bomb(tmp_path):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as source:
        source.writestr("zeros.py", b"0" * 100_000)
    with pytest.raises(IngestionError, match="compression-ratio"):
        ingest_zip(archive, tmp_path / "workspace", Settings())


@pytest.mark.parametrize(
    "settings,files,reason",
    [
        (Settings(max_file_bytes=1024), {"main.py": "x" * 1025}, "per-file"),
        (Settings(max_extracted_bytes=1024), {"a.py": "a" * 600, "b.py": "b" * 600}, "extracted"),
        (Settings(max_files=1), {"a.py": "a", "b.py": "b"}, "file-count"),
        (Settings(max_upload_bytes=1024), {"a.py": "a" * 2000}, "upload-size"),
    ],
)
def test_rejects_resource_limits(tmp_path, settings, files, reason):
    archive = make_zip(tmp_path / "source.zip", files)
    with pytest.raises(IngestionError, match=reason):
        ingest_zip(archive, tmp_path / "workspace", settings)


def test_never_overwrites_existing_workspace(tmp_path):
    destination = tmp_path / "workspace"
    destination.mkdir()
    (destination / "original.py").write_text("original")
    archive = make_zip(tmp_path / "source.zip", {"new.py": "new"})
    with pytest.raises(IngestionError, match="already exists"):
        ingest_zip(archive, destination, Settings())
    assert (destination / "original.py").read_text() == "original"


def test_competing_ingestions_do_not_delete_the_successful_workspace(monkeypatch, tmp_path):
    archive = make_zip(tmp_path / "source.zip", {"main.py": "original"})
    destination = tmp_path / "workspace"
    original = __import__("tempfile").mkdtemp
    synchronized = threading.Barrier(2)

    def simultaneous_staging(**kwargs):
        staging = original(**kwargs)
        synchronized.wait(timeout=5)
        return staging

    monkeypatch.setattr("codeproof.ingestion.tempfile.mkdtemp", simultaneous_staging)

    def ingest(_):
        try:
            ingest_zip(archive, destination, Settings())
            return True
        except IngestionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(ingest, range(2)))
    assert results.count(True) == 1
    assert (destination / "main.py").read_text() == "original"
    assert not list(tmp_path.glob(".ingest-*"))


def test_excluded_root_is_not_stripped_to_reintroduce_secrets(tmp_path):
    archive = make_zip(tmp_path / "source.zip", {".git/config": "private metadata"})
    with pytest.raises(IngestionError, match="no eligible"):
        ingest_zip(archive, tmp_path / "workspace", Settings())


def test_corrupt_archive_cleans_staging(tmp_path):
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"not a zip")
    with pytest.raises(IngestionError, match="corrupt"):
        ingest_zip(archive, tmp_path / "workspace", Settings())
    assert not (tmp_path / "workspace").exists()
    assert not list(tmp_path.glob(".ingest-*"))


def test_corrupt_entry_crc_removes_partially_extracted_content(tmp_path):
    archive = make_zip(tmp_path / "source.zip", {"first.py": "safe", "last.py": "original"})
    archive.write_bytes(archive.read_bytes().replace(b"original", b"tampered"))
    with pytest.raises(IngestionError, match="corrupt"):
        ingest_zip(archive, tmp_path / "workspace", Settings())
    assert not (tmp_path / "workspace").exists()
    assert not list(tmp_path.glob(".ingest-*"))


def test_rejects_encrypted_entries_before_extraction(tmp_path):
    archive = make_zip(tmp_path / "source.zip", {"main.py": "safe"})
    content = bytearray(archive.read_bytes())
    local = content.index(b"PK\x03\x04")
    central = content.index(b"PK\x01\x02")
    content[local + 6] |= 1
    content[central + 8] |= 1
    archive.write_bytes(content)
    with pytest.raises(IngestionError, match="Encrypted"):
        ingest_zip(archive, tmp_path / "workspace", Settings())
    assert not (tmp_path / "workspace").exists()


@pytest.mark.parametrize(
    "reference",
    [
        "owner/repo",
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git/",
    ],
)
def test_parses_public_github_reference(reference):
    assert parse_github_reference(reference) == ("owner", "repo")


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.com/owner/repo",
        "http://github.com/owner/repo",
        "file:///tmp/source",
        "https://github.com:443/owner/repo",
        "https://user:pass@github.com/owner/repo",
        "https://github.com/owner/repo/tree/main",
        "owner/repo?token=secret",
        "owner/..",
        "https://github.com/owner/repo?ref=main",
        "https://github.com/owner/repo#readme",
        "https://github.com/owner%2Frepo",
        "-owner/repo",
        "owner/repo/extra",
    ],
)
def test_rejects_non_github_or_ambiguous_references(reference):
    with pytest.raises(IngestionError):
        parse_github_reference(reference)


def mocked_client(monkeypatch, handler):
    original = httpx.Client

    def factory(**kwargs):
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("codeproof.ingestion.httpx.Client", factory)


def test_github_resolves_public_default_branch_and_streams_archive(monkeypatch, tmp_path):
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("repo-feature-testing/main.py", "print('test')")
    requests = []
    revision = "abcdef0123456789abcdef0123456789abcdef01"

    def handler(request):
        requests.append(request)
        if "/commits/" in request.url.path:
            assert request.headers["accept"] == "application/vnd.github.sha"
            return httpx.Response(200, content=revision.encode("ascii"))
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"private": False, "default_branch": "feature/testing"})
        return httpx.Response(200, content=zipped.getvalue())

    mocked_client(monkeypatch, handler)
    result = ingest_github("owner/repo", tmp_path / "workspace", Settings())
    assert result["revision"] == revision
    assert result["branch"] == "feature/testing"
    assert result["repository"] == "owner/repo"
    assert (tmp_path / "workspace/main.py").exists()
    assert requests[1].url.raw_path.endswith(b"feature%2Ftesting")
    assert requests[2].url.path.endswith("/zip/" + revision)
    assert all("authorization" not in request.headers for request in requests)


def test_github_denies_offsite_redirect_before_request(monkeypatch, tmp_path):
    requested = []

    def handler(request):
        requested.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://attacker.example/source.zip"})

    mocked_client(monkeypatch, handler)
    with pytest.raises(IngestionError, match="approved HTTPS"):
        ingest_github("owner/repo", tmp_path / "workspace", Settings())
    assert requested == ["api.github.com"]


def test_github_rejects_private_repo(monkeypatch, tmp_path):
    mocked_client(
        monkeypatch,
        lambda _: httpx.Response(
            200, content=json.dumps({"private": True, "default_branch": "main"}).encode()
        ),
    )
    with pytest.raises(IngestionError, match="must be public"):
        ingest_github("owner/repo", tmp_path / "workspace", Settings())


def test_github_rejects_oversized_response(monkeypatch, tmp_path):
    mocked_client(
        monkeypatch,
        lambda _: httpx.Response(200, content=b"{}", headers={"Content-Length": str(300 * 1024)}),
    )
    with pytest.raises(IngestionError, match="download size"):
        ingest_github("owner/repo", tmp_path / "workspace", Settings())


@pytest.mark.parametrize("revision", ["main", "../other", "a" * 39, "a" * 41, "g" * 40])
def test_github_rejects_invalid_immutable_revision(monkeypatch, tmp_path, revision):
    requested = []

    def handler(request):
        requested.append(request)
        if "/commits/" in request.url.path:
            return httpx.Response(200, content=revision.encode())
        return httpx.Response(200, json={"private": False, "default_branch": "main"})

    mocked_client(monkeypatch, handler)
    with pytest.raises(IngestionError, match="immutable commit"):
        ingest_github("owner/repo", tmp_path / "workspace", Settings())
    assert len(requested) == 2
    assert not (tmp_path / "workspace").exists()


def test_github_rejects_malformed_repository_metadata(monkeypatch, tmp_path):
    mocked_client(monkeypatch, lambda _: httpx.Response(200, json=[]))
    with pytest.raises(IngestionError, match="invalid repository metadata"):
        ingest_github("owner/repo", tmp_path / "workspace", Settings())
