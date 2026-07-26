-- Lunelle Studio schema v2: runtime API profiles, style imagegen assets,
-- and the widened task output_type set (hero / matrix_cell / repair).
--
-- The tasks table must be rebuilt because SQLite cannot alter a CHECK
-- constraint. The marker below makes the migration runner disable
-- foreign-key enforcement around this file (SQLite's documented rebuild
-- procedure) and run PRAGMA foreign_key_check before committing.
-- lunelle:foreign_keys=off

CREATE TABLE api_profiles (
    profile_id          TEXT PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    base_url            TEXT NOT NULL,
    api_key             TEXT NOT NULL,
    model               TEXT NOT NULL,
    reference_mode      TEXT NOT NULL DEFAULT 'auto' CHECK (reference_mode IN ('auto', 'seedream', 'openai-edits', 'off')),
    supports_mask       INTEGER NOT NULL DEFAULT 0,
    price_per_image_usd REAL,
    is_active           INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

ALTER TABLE styles ADD COLUMN plan_image_path TEXT;

ALTER TABLE styles ADD COLUMN identity_text TEXT;

CREATE TABLE tasks_new (
    task_id             TEXT PRIMARY KEY,
    batch_id            TEXT REFERENCES batches(batch_id),
    style_id            TEXT NOT NULL REFERENCES styles(style_id),
    sku                 TEXT NOT NULL,
    output_type         TEXT NOT NULL CHECK (output_type IN ('grid', 'wearing', 'hero', 'matrix_cell', 'repair')),
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

INSERT INTO tasks_new SELECT * FROM tasks;

DROP TABLE tasks;

ALTER TABLE tasks_new RENAME TO tasks;

CREATE INDEX idx_tasks_status ON tasks(status);

CREATE INDEX idx_tasks_sku ON tasks(sku);

CREATE INDEX idx_tasks_batch ON tasks(batch_id);

CREATE INDEX idx_tasks_style ON tasks(style_id);
