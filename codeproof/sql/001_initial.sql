CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS codeproof_runs (
    id TEXT PRIMARY KEY,
    source JSONB NOT NULL,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'improved', 'reviewed', 'stopped', 'failed')),
    stage TEXT NOT NULL DEFAULT 'queued',
    detail TEXT NOT NULL DEFAULT 'Waiting for the review worker.',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    report JSONB
);

CREATE INDEX IF NOT EXISTS codeproof_runs_queue_idx
    ON codeproof_runs (created_at, id) WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS codeproof_source_chunks (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES codeproof_runs(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    start_line INTEGER NOT NULL CHECK (start_line > 0),
    end_line INTEGER NOT NULL CHECK (end_line >= start_line),
    content TEXT NOT NULL,
    embedding VECTOR(384) NOT NULL,
    embedding_method TEXT NOT NULL DEFAULT 'lexical-feature-hash-v1',
    UNIQUE (run_id, path, start_line)
);

CREATE INDEX IF NOT EXISTS codeproof_chunks_run_idx ON codeproof_source_chunks (run_id);
