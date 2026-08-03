-- Lunelle Studio schema v7: immutable input snapshots, content-addressed
-- assets, and an execution record.
--
-- The problem: a task recorded WHERE its inputs lived, not WHAT they were. The
-- style spec and identity text could be edited afterwards, the active API profile
-- switched, contracts changed by a deploy, and hand models were written to a fixed
-- filename so re-uploading one silently replaced the bytes a past matrix cell had
-- been generated from. "What produced this image" was therefore unrecoverable, and
-- re-running a task could not reproduce it.
--
-- Three tables, three distinct jobs:
--   assets          - content-addressed registry; a digest names bytes forever
--   task_snapshots  - the frozen PLAN, written with the task, never updated
--   task_executions - what was ACTUALLY sent per attempt (an observation)
--
-- Plan and execution are kept apart because they legitimately differ: when a
-- reference is missing at execution time the prompt has its reference block
-- stripped, so what reached the provider is not what was planned. Auditing needs
-- the former, reproducing needs the latter.

CREATE TABLE assets (
    digest      TEXT PRIMARY KEY,          -- full sha256 of the bytes
    kind        TEXT NOT NULL,
    mime_type   TEXT NOT NULL,
    byte_size   INTEGER NOT NULL,
    path        TEXT NOT NULL,             -- where these bytes live now
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_assets_kind ON assets(kind);

CREATE TABLE task_snapshots (
    task_id           TEXT PRIMARY KEY REFERENCES tasks(task_id),
    -- sha256 over the canonical input structure. Equal fingerprints mean equal
    -- inputs, which is what makes reproducibility checkable rather than assumed.
    input_fingerprint TEXT NOT NULL,
    snapshot_version  INTEGER NOT NULL,
    snapshot_json     TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE INDEX idx_snapshots_fingerprint ON task_snapshots(input_fingerprint);

CREATE TABLE task_executions (
    task_id        TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_no     INTEGER NOT NULL,
    execution_json TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (task_id, attempt_no)
);

-- Denormalized onto tasks so listing and filtering need no join.
ALTER TABLE tasks ADD COLUMN input_fingerprint TEXT;

CREATE INDEX idx_tasks_fingerprint ON tasks(input_fingerprint);

-- ---- Backfill ---------------------------------------------------------------
-- Historical tasks get NO synthesized snapshot. A fabricated one would be
-- indistinguishable from a real one while being a guess: the spec, contracts, and
-- profile in force at the time are simply not recoverable. NULL
-- input_fingerprint honestly means "created before snapshots existed", and the
-- API reports exactly that.
