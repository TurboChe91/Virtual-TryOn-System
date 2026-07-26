-- Lunelle Studio schema v1
-- Applied automatically by lunelle.db.migrate(); tracked in schema_migrations.

CREATE TABLE styles (
    style_id            TEXT PRIMARY KEY,
    sku                 TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    spec_json           TEXT NOT NULL,
    source_type         TEXT NOT NULL CHECK (source_type IN ('structured', 'natural_language', 'hybrid')),
    source_input_json   TEXT NOT NULL,
    reference_image_path TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE batches (
    batch_id    TEXT PRIMARY KEY,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE tasks (
    task_id             TEXT PRIMARY KEY,
    batch_id            TEXT REFERENCES batches(batch_id),
    style_id            TEXT NOT NULL REFERENCES styles(style_id),
    sku                 TEXT NOT NULL,
    output_type         TEXT NOT NULL CHECK (output_type IN ('grid', 'wearing')),
    prompt              TEXT NOT NULL,
    negative_prompt     TEXT NOT NULL DEFAULT '',
    prompt_version      TEXT NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('pending', 'running', 'success', 'failed', 'retrying', 'cancelled')),
    retry_count         INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 2,
    estimated_cost_usd  REAL,
    actual_cost_usd     REAL,
    external_request_id TEXT,
    idempotency_key     TEXT UNIQUE,
    wait_for_task_id    TEXT REFERENCES tasks(task_id),
    next_attempt_at     TEXT,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    updated_at          TEXT NOT NULL,
    output_path         TEXT,
    output_url          TEXT,
    error_code          TEXT,
    error_message       TEXT,
    metadata_json       TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_sku ON tasks(sku);
CREATE INDEX idx_tasks_batch ON tasks(batch_id);
CREATE INDEX idx_tasks_style ON tasks(style_id);

CREATE TABLE attempts (
    attempt_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_no          INTEGER NOT NULL,
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    request_fingerprint TEXT,
    external_request_id TEXT,
    http_status         INTEGER,
    reference_used      INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    duration_ms         INTEGER,
    outcome             TEXT NOT NULL CHECK (outcome IN ('success', 'error', 'started')),
    error_code          TEXT,
    error_message       TEXT,
    cost_usd            REAL,
    UNIQUE (task_id, attempt_no)
);

CREATE TABLE qa_results (
    qa_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL REFERENCES tasks(task_id),
    passed              INTEGER NOT NULL,
    score               INTEGER NOT NULL,
    issues_json         TEXT NOT NULL DEFAULT '[]',
    checks_json         TEXT NOT NULL DEFAULT '{}',
    recommended_action  TEXT NOT NULL,
    needs_human_review  INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL
);

CREATE INDEX idx_qa_task ON qa_results(task_id);
