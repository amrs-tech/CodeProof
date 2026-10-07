import math
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from codeproof.config import Settings
from codeproof.storage import StorageError, Store, lexical_embedding


def test_lexical_vectors_are_stable_normalized_and_finite():
    vector = lexical_embedding("repository source fragile fragile context")
    assert vector == lexical_embedding("repository source fragile fragile context")
    assert len(vector) == 384
    assert sum(value * value for value in vector) == pytest.approx(1)
    assert all(math.isfinite(value) for value in lexical_embedding(""))


def test_storage_requires_initialization():
    with pytest.raises(StorageError, match="not been initialized"):
        Store(Settings()).list_runs()


def test_invalid_connection_errors_never_expose_connection_secrets():
    store = Store(Settings(database_url="invalid connection with password=private-password"))
    with pytest.raises(StorageError) as failure:
        store.initialize()
    assert "private-password" not in str(failure.value)


@pytest.fixture
def store():
    database_url = os.getenv("CODEPROOF_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set CODEPROOF_TEST_DATABASE_URL to test PostgreSQL and pgvector.")
    storage = Store(Settings(database_url=database_url, max_queued_runs=100))
    storage.initialize()
    # The URL must point to a dedicated disposable test database.
    with storage.pool.connection() as connection:
        connection.execute("TRUNCATE codeproof_runs CASCADE")
    yield storage
    with storage.pool.connection() as connection:
        connection.execute("TRUNCATE codeproof_runs CASCADE")
    storage.close()


@pytest.mark.integration
def test_persists_report_patch_and_pgvector_context(store, tmp_path):
    run_id = uuid.uuid4().hex
    source = {"type": "zip", "name": "source.zip", "files": 3}
    row = store.create_run(run_id, source, tmp_path)
    assert row["status"] == "queued"
    assert store.get_run(run_id)["source"] == source
    assert store.list_runs()[0]["id"] == run_id
    (tmp_path / "payments.py").write_text(
        "def calculate_invoice_total(amount):\n    return amount\n"
    )
    (tmp_path / "images.py").write_text("def resize_image_pixels(image):\n    return image\n")
    (tmp_path / ".env").write_text("PASSWORD=private")
    assert store.index_workspace(run_id, tmp_path) == 2
    context = store.search_context(run_id, "calculate_invoice_total invoice amount", 1)
    assert context[0]["path"] == "payments.py"
    assert context[0]["embedding_method"] == "lexical-feature-hash-v1"
    assert store.search_context("different-run", "invoice") == []
    with store.pool.connection() as connection:
        assert connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        ).fetchone()
        assert (
            connection.execute(
                "SELECT vector_dims(embedding) AS dimensions FROM codeproof_source_chunks LIMIT 1"
            ).fetchone()["dimensions"]
            == 384
        )
    assert store.claim_run()["id"] == run_id
    store.update_progress(run_id, "verification", "Checking the candidate fix.")
    assert store.get_run(run_id)["stage"] == "verification"
    report = {
        "status": "improved",
        "summary": "Verified fix applied.",
        "patch": "--- a/file\n+++ b/file\n",
    }
    store.finish_run(run_id, report)
    assert store.get_run(run_id)["report"] == report
    assert store.get_run(run_id)["status"] == "improved"


@pytest.mark.integration
def test_atomic_claims_do_not_duplicate_jobs(store, tmp_path):
    run_ids = {uuid.uuid4().hex for _ in range(12)}
    for run_id in run_ids:
        store.create_run(run_id, {"type": "zip"}, tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        claimed = list(executor.map(lambda _: store.claim_run(), range(16)))
    claimed_ids = [row["id"] for row in claimed if row]
    assert len(claimed_ids) == len(set(claimed_ids)) == len(run_ids)
    assert set(claimed_ids) == run_ids


@pytest.mark.integration
def test_running_jobs_are_marked_interrupted_and_terminal_jobs_stay_terminal(store, tmp_path):
    run_id = uuid.uuid4().hex
    store.create_run(run_id, {"type": "zip"}, tmp_path)
    store.claim_run()
    assert store.recover_interrupted() == 1
    row = store.get_run(run_id)
    assert row["status"] == "failed"
    assert row["stage"] == "interrupted"
    store.finish_run(run_id, {"status": "improved"})
    assert store.get_run(run_id)["status"] == "failed"


@pytest.mark.integration
def test_queue_capacity_is_atomic(store, tmp_path):
    store.settings.max_queued_runs = 2

    def submit(_):
        try:
            store.create_run(uuid.uuid4().hex, {"type": "zip"}, tmp_path)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        accepted = list(executor.map(submit, range(8)))
    assert accepted.count(True) == 2
    assert len(store.list_runs()) == 2


@pytest.mark.integration
def test_worker_lock_has_one_owner_and_releases_on_close(store):
    other = Store(store.settings)
    other.initialize()
    try:
        assert store.acquire_worker_lock()
        assert store.acquire_worker_lock()
        assert store.worker_lock_healthy()
        assert not other.worker_lock_healthy()
        assert not other.acquire_worker_lock()
        store.close()
        assert not store.worker_lock_healthy()
        assert other.acquire_worker_lock()
        assert other.worker_lock_healthy()
    finally:
        other.close()
        store.initialize()


@pytest.mark.integration
def test_index_excludes_symlinks_and_obvious_source_credentials(store, tmp_path):
    run_id = uuid.uuid4().hex
    store.create_run(run_id, {"type": "zip"}, tmp_path)
    (tmp_path / "good.py").write_text("def validated_source():\n    return 1\n")
    (tmp_path / "secret.py").write_text("password = 'a-real-secret-password'\n")
    (tmp_path / "binary.py").write_bytes(b"a\x00b")
    (tmp_path / "huge.py").write_bytes(b"x" * (store.settings.max_file_bytes + 1))
    assert store.index_workspace(run_id, tmp_path) == 1
    assert [item["path"] for item in store.search_context(run_id, "source", 10)] == ["good.py"]
