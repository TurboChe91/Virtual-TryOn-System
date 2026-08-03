-- Lunelle Studio schema v6: one shared budget per generation lineage, and an
-- atomic spend ledger.
--
-- Problem 1 (lineage): auto-regeneration and automatic correction each tracked
-- their own depth in task metadata, and neither copied the other's counter to
-- the child it created. A task could therefore alternate regen -> correct ->
-- regen -> ... forever, each hop resetting the counter the other path checked,
-- so the advertised worst-case cost was not a bound at all. Both paths now draw
-- from ONE per-root ledger: a shared descendant count and a shared dollar cap.
--
-- Problem 2 (reservations): the worker's budget check read spend, then made a
-- paid call. Two workers could both read "under budget" and both spend. Spend is
-- now claimed inside a transaction BEFORE the provider call and settled at the
-- real cost afterwards, so the cap holds no matter how many workers run.

-- Every task belongs to exactly one lineage: a root points at itself.
ALTER TABLE tasks ADD COLUMN root_task_id TEXT;

CREATE TABLE generation_lineages (
    root_task_id      TEXT PRIMARY KEY REFERENCES tasks(task_id),
    style_id          TEXT NOT NULL,
    output_type       TEXT NOT NULL,
    -- Automatic descendants created so far (regenerations AND corrections).
    descendant_count  INTEGER NOT NULL DEFAULT 0,
    max_descendants   INTEGER NOT NULL,
    -- Settled + reserved spend across the whole lineage, root included.
    lineage_spent_usd REAL NOT NULL DEFAULT 0,
    lineage_max_usd   REAL NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX idx_tasks_root ON tasks(root_task_id);

-- One row per provider attempt. `reserved` is an in-flight claim on the budget;
-- `settled` carries the final cost; `released` means the call did not bill.
CREATE TABLE spend_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_no     INTEGER NOT NULL,
    root_task_id   TEXT,
    estimated_usd  REAL NOT NULL,
    actual_usd     REAL,
    state          TEXT NOT NULL CHECK (state IN ('reserved', 'settled', 'released')),
    created_at     TEXT NOT NULL,
    settled_at     TEXT,
    UNIQUE (task_id, attempt_no)
);

CREATE INDEX idx_reservations_state ON spend_reservations(state);

CREATE INDEX idx_reservations_created ON spend_reservations(created_at);

CREATE INDEX idx_reservations_root ON spend_reservations(root_task_id);

-- ---- Backfill ---------------------------------------------------------------
-- Existing tasks are their own lineage root. Historical metadata depths are not
-- migrated: the ledger starts fresh, and a lineage that already spent its budget
-- under the old counters simply gets one clean allowance.
UPDATE tasks SET root_task_id = task_id WHERE root_task_id IS NULL;

-- Every attempt that reached the provider becomes a settled reservation, so
-- rolling-window spend is continuous across the migration rather than resetting
-- to zero (which would briefly hand out a second budget for the same day).
INSERT INTO spend_reservations
    (task_id, attempt_no, root_task_id, estimated_usd, actual_usd, state,
     created_at, settled_at)
SELECT
    a.task_id,
    a.attempt_no,
    t.task_id,
    COALESCE(a.cost_usd, t.estimated_cost_usd, 0),
    CASE WHEN a.outcome = 'started' THEN NULL
         ELSE COALESCE(a.cost_usd, t.actual_cost_usd, t.estimated_cost_usd, 0) END,
    CASE WHEN a.outcome = 'started' THEN 'released' ELSE 'settled' END,
    a.started_at,
    a.finished_at
FROM attempts a JOIN tasks t ON t.task_id = a.task_id;
