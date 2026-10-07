"""PostgreSQL persistence and run-scoped pgvector lexical context retrieval.

The 384-dimensional embeddings use deterministic feature hashing, not a neural
semantic model. They work offline and never send repository source to a service.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

from codeproof.config import Settings
from codeproof.ingestion import is_excluded_path, looks_like_secret

_DIMENSIONS = 384
_MAX_CHUNKS = 1000
_TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".kt",
        ".scala",
        ".sql",
        ".html",
        ".css",
        ".scss",
        ".md",
        ".txt",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
    }
)
_RUN_COLUMNS = "id, source, workspace, status, stage, detail, created_at, updated_at, report"


class StorageError(RuntimeError):
    """A deliberately sanitized database failure."""


def lexical_embedding(text: str) -> list[float]:
    """Stable normalized lexical vectors, with a signed hash to limit collisions."""
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,79}", text.casefold()[:20000])
    counts = Counter(words)
    values = [0.0] * _DIMENSIONS
    for token, count in counts.items():
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        position = int.from_bytes(digest[:4], "big") % _DIMENSIONS
        sign = 1 if digest[4] & 1 else -1
        values[position] += sign * (1 + math.log(count))
    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude:
        return [value / magnitude for value in values]
    # Avoid zero vectors, which have undefined cosine similarity in pgvector.
    values[0] = 1.0
    return values


def _vector_literal(text: str) -> str:
    return "[" + ",".join(format(value, ".9g") for value in lexical_embedding(text)) + "]"


def _run(row: dict | None) -> dict | None:
    if row is None:
        return None
    for field in ("created_at", "updated_at"):
        if row.get(field) is not None:
            row[field] = row[field].isoformat()
    return row


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: ConnectionPool | None = None
        self._worker_connection: psycopg.Connection | None = None
        self._worker_lock = threading.Lock()

    def initialize(self) -> None:
        """Fail readiness if PostgreSQL or its required vector extension is unavailable."""
        if self.pool is not None:
            return
        try:
            # Create the extension before opening pooled connections. Serializing schema
            # initialization also prevents concurrent CREATE TABLE startup races.
            with psycopg.connect(self.settings.database_url, connect_timeout=5) as connection:
                connection.execute("SET LOCAL statement_timeout = '15s'")
                connection.execute("SELECT pg_advisory_xact_lock(1129270866)")
                migration = Path(__file__).with_name("sql") / "001_initial.sql"
                connection.execute(migration.read_text(encoding="utf-8"))
            pool = ConnectionPool(
                self.settings.database_url,
                min_size=1,
                max_size=4,
                open=False,
                timeout=5,
                reconnect_timeout=5,
                max_waiting=20,
                kwargs={
                    "row_factory": dict_row,
                    "connect_timeout": 5,
                    "options": "-c statement_timeout=15000 -c idle_in_transaction_session_timeout=15000",
                },
            )
            pool.open(wait=True, timeout=5)
            self.pool = pool
        except (psycopg.Error, PoolTimeout, OSError, ValueError):
            if "pool" in locals():
                pool.close()
            raise StorageError(
                "PostgreSQL with pgvector is unavailable. Check the database service, "
                "connection settings, and vector-extension permissions."
            ) from None

    def close(self) -> None:
        with self._worker_lock:
            if self._worker_connection is not None:
                self._worker_connection.close()
                self._worker_connection = None
        if self.pool is not None:
            self.pool.close()
            self.pool = None

    def acquire_worker_lock(self) -> bool:
        """Hold a session lock outside the pool for the lifetime of the sole worker."""
        with self._worker_lock:
            if self._worker_connection is not None:
                return not self._worker_connection.closed
            connection = None
            try:
                connection = psycopg.connect(
                    self.settings.database_url,
                    connect_timeout=5,
                    autocommit=True,
                    options="-c statement_timeout=5000",
                    keepalives_idle=5,
                    keepalives_interval=5,
                    keepalives_count=2,
                    tcp_user_timeout=5000,
                )
                acquired = connection.execute("SELECT pg_try_advisory_lock(1129270868)").fetchone()[
                    0
                ]
                if acquired:
                    self._worker_connection = connection
                    return True
                connection.close()
                return False
            except psycopg.Error:
                if connection is not None:
                    connection.close()
                raise StorageError("Could not acquire the PostgreSQL worker lock.") from None

    def worker_lock_healthy(self) -> bool:
        """Detect dropped sessions before a stale worker performs further writes."""
        with self._worker_lock:
            connection = self._worker_connection
            if connection is None or connection.closed:
                return False
            try:
                return bool(
                    connection.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                        "AND pid = pg_backend_pid() AND classid = 0 AND objid = 1129270868 "
                        "AND objsubid = 1 AND granted)"
                    ).fetchone()[0]
                )
            except psycopg.Error:
                connection.close()
                self._worker_connection = None
                return False

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        if self.pool is None:
            raise StorageError("PostgreSQL storage has not been initialized.")
        try:
            with self.pool.connection(timeout=5) as connection:
                yield connection
        except (psycopg.Error, PoolTimeout):
            raise StorageError("PostgreSQL operation failed; check the database service.") from None

    def create_run(self, run_id: str, source: dict, workspace: Path) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            raise ValueError("Invalid run identifier.")
        with self._connection() as connection:
            # All submissions take the same transaction lock so simultaneous uploads
            # cannot both pass the queue-capacity check.
            connection.execute("SELECT pg_advisory_xact_lock(1129270867)")
            count = connection.execute(
                "SELECT count(*) AS count FROM codeproof_runs WHERE status IN ('queued', 'running')"
            ).fetchone()["count"]
            if count >= self.settings.max_queued_runs:
                raise ValueError("The review queue is full. Wait for an existing run to finish.")
            row = connection.execute(
                f"INSERT INTO codeproof_runs (id, source, workspace) VALUES (%s, %s, %s) "
                f"RETURNING {_RUN_COLUMNS}",
                (run_id, Jsonb(source), str(Path(workspace).resolve())),
            ).fetchone()
            return _run(row)

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {_RUN_COLUMNS} FROM codeproof_runs ORDER BY created_at DESC, id DESC LIMIT %s",
                (max(1, min(limit, 100)),),
            ).fetchall()
            return [_run(row) for row in rows]

    def get_run(self, run_id: str) -> dict | None:
        with self._connection() as connection:
            return _run(
                connection.execute(
                    f"SELECT {_RUN_COLUMNS} FROM codeproof_runs WHERE id = %s", (run_id,)
                ).fetchone()
            )

    def claim_run(self) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "WITH candidate AS (SELECT id FROM codeproof_runs WHERE status = 'queued' "
                "ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE codeproof_runs SET status = 'running', stage = 'indexing', "
                "detail = 'Building bounded repository context.', updated_at = now() "
                "FROM candidate WHERE codeproof_runs.id = candidate.id RETURNING "
                + ", ".join("codeproof_runs." + name.strip() for name in _RUN_COLUMNS.split(","))
            ).fetchone()
            return _run(row)

    def update_progress(self, run_id: str, stage: str, detail: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE codeproof_runs SET stage = %s, detail = %s, updated_at = now() "
                "WHERE id = %s AND status = 'running'",
                (stage[:100], detail[:2000], run_id),
            )

    def finish_run(self, run_id: str, report: dict) -> None:
        status = report.get("status", "reviewed")
        if status not in {"improved", "reviewed", "stopped"}:
            status = "stopped"
        with self._connection() as connection:
            connection.execute(
                "UPDATE codeproof_runs SET status = %s, stage = 'finished', "
                "detail = %s, report = %s, updated_at = now() WHERE id = %s AND status = 'running'",
                (
                    status,
                    str(report.get("summary", "Review finished."))[:2000],
                    Jsonb(report),
                    run_id,
                ),
            )

    def fail_run(self, run_id: str, message: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE codeproof_runs SET status = 'failed', stage = 'failed', detail = %s, "
                "updated_at = now() WHERE id = %s AND status IN ('queued', 'running')",
                (message[:2000], run_id),
            )

    def recover_interrupted(self) -> int:
        """Never silently restart a partly applied review after a worker restart."""
        with self._connection() as connection:
            return connection.execute(
                "UPDATE codeproof_runs SET status = 'failed', stage = 'interrupted', "
                "detail = 'Worker stopped during this run. Submit the source again to retry.', "
                "updated_at = now() WHERE status = 'running'"
            ).rowcount

    def index_workspace(self, run_id: str, workspace: Path) -> int:
        root = Path(workspace).resolve()
        chunks = []
        for path in sorted(root.rglob("*")):
            if len(chunks) >= _MAX_CHUNKS:
                break
            relative = path.relative_to(root).as_posix()
            if (
                path.is_symlink()
                or not path.is_file()
                or is_excluded_path(relative)
                or path.suffix.casefold() not in _TEXT_SUFFIXES
                or not path.resolve().is_relative_to(root)
            ):
                continue
            try:
                if path.stat().st_size > self.settings.max_file_bytes:
                    continue
                with path.open("rb") as reader:
                    raw = reader.read(self.settings.max_file_bytes + 1)
                if len(raw) > self.settings.max_file_bytes or b"\x00" in raw:
                    continue
                content = raw.decode("utf-8")
            except (OSError, UnicodeError):
                continue
            if looks_like_secret(content):
                continue
            lines = content.splitlines()
            for start in range(0, len(lines), 70):
                end = min(start + 80, len(lines))
                chunk = "\n".join(lines[start:end])[:8000]
                if chunk.strip():
                    chunks.append(
                        (
                            run_id,
                            relative,
                            start + 1,
                            end,
                            chunk,
                            _vector_literal(relative + "\n" + chunk),
                        )
                    )
                if len(chunks) >= _MAX_CHUNKS:
                    break
        with self._connection() as connection:
            connection.execute("DELETE FROM codeproof_source_chunks WHERE run_id = %s", (run_id,))
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO codeproof_source_chunks "
                    "(run_id, path, start_line, end_line, content, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s::vector)",
                    chunks,
                )
        return len(chunks)

    def search_context(self, run_id: str, query: str, limit: int = 4) -> list[dict]:
        with self._connection() as connection:
            return connection.execute(
                "SELECT path, start_line, end_line, content, embedding_method, "
                "(embedding <=> %s::vector) AS distance "
                "FROM codeproof_source_chunks WHERE run_id = %s "
                "ORDER BY embedding <=> %s::vector, id LIMIT %s",
                (_vector_literal(query), run_id, _vector_literal(query), max(1, min(limit, 10))),
            ).fetchall()
