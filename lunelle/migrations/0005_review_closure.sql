-- Lunelle Studio schema v5: explicit QA / human-review state machine.
--
-- Why this exists: a task used to be published as `success` before its QA row
-- was written, so callers saw a completed asset with no QA result (a real race,
-- not just a test artifact), and an absent QA row read as NULL -> falsy, which
-- let never-reviewed assets slip through the "reviewed only" export filter.
--
-- Two orthogonal columns: qa_state = where the machine QA pipeline is,
-- review_state = where human review is. Every export/publish query gates on
-- both (default-deny, see lunelle/gating.py).
--
-- No table rebuild needed: SQLite accepts ADD COLUMN with NOT NULL DEFAULT and
-- a CHECK, and enforces the CHECK on subsequent writes.

ALTER TABLE tasks ADD COLUMN qa_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (qa_state IN ('pending', 'running', 'done', 'error', 'skipped'));

ALTER TABLE tasks ADD COLUMN review_state TEXT NOT NULL DEFAULT 'generated'
    CHECK (review_state IN ('generated', 'waiting_human_review', 'approved',
                            'rejected', 'publish_ready', 'published'));

ALTER TABLE tasks ADD COLUMN reviewed_at TEXT;

ALTER TABLE tasks ADD COLUMN reviewed_by TEXT;

-- Audit trail: every human verdict, kept even when a later verdict overrides it.
CREATE TABLE asset_reviews (
    review_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(task_id),
    qa_id       INTEGER REFERENCES qa_results(qa_id),
    decision    TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    note        TEXT NOT NULL DEFAULT '',
    reviewer    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_asset_reviews_task ON asset_reviews(task_id);

-- Distinguish heuristic QA from advisory LLM verdicts and manual entries. Only
-- heuristic rows feed the gate; llm rows never clear human review.
ALTER TABLE qa_results ADD COLUMN source TEXT NOT NULL DEFAULT 'heuristic'
    CHECK (source IN ('heuristic', 'llm', 'manual'));

CREATE INDEX idx_tasks_review_state ON tasks(review_state);

CREATE INDEX idx_tasks_qa_state ON tasks(qa_state);

-- ---- Backfill (conservative / default-deny) --------------------------------
-- Successful tasks that already have a QA row have finished machine QA.
UPDATE tasks SET qa_state = 'done'
    WHERE status = 'success'
      AND EXISTS (SELECT 1 FROM qa_results q WHERE q.task_id = tasks.task_id);

-- Successful tasks with no QA row never got one (QA crashed or predates QA).
UPDATE tasks SET qa_state = 'error'
    WHERE status = 'success'
      AND NOT EXISTS (SELECT 1 FROM qa_results q WHERE q.task_id = tasks.task_id);

-- Every historical success re-enters human review. We deliberately do NOT treat
-- needs_human_review = 0 as "a human approved it": the LLM gate also wrote 0,
-- and automatic QA passing is not human approval.
UPDATE tasks SET review_state = 'waiting_human_review' WHERE status = 'success';

-- Historical rows written by the LLM gate are advisory; mark them as such and
-- restore the human-review flag they cleared.
UPDATE qa_results SET source = 'llm', needs_human_review = 1
    WHERE recommended_action IN ('publish', 'correct')
       OR checks_json LIKE '%llm_identity_qa%';
