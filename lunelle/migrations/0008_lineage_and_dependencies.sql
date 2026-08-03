-- Lunelle Studio schema v8: explicit ancestry, and dependencies that fail loudly.
--
-- Problem 1 (lineage): root_task_id (v6) says which lineage a task belongs to but
-- not where in it. Given a repair five versions deep there was no way to walk back
-- through the chain that produced it — "what was this a repair OF" needed a scan of
-- metadata JSON, and the answer was only as good as whichever key happened to be
-- stamped.
--
-- Problem 2 (dependencies): claim_next treated a dependency as satisfied when it
-- was merely no longer ACTIVE:
--
--   AND (wait_for_task_id IS NULL OR wait_for_task_id NOT IN
--        (SELECT task_id FROM tasks WHERE status IN ('pending','running','retrying')))
--
-- A FAILED or CANCELLED grid therefore released the wearing shot that was waiting
-- on it, which then ran and silently degraded to a text-only render — billed, and
-- indistinguishable from an intended one without reading the logs. A dependent
-- whose dependency died must fail as dependency_failed, not proceed degraded.

-- Direct parent. NULL for a root. root_task_id (v6) stays: parent gives the step,
-- root gives the budget scope, and both are needed.
ALTER TABLE tasks ADD COLUMN parent_task_id TEXT REFERENCES tasks(task_id);

-- Distance from the root, so depth is a query rather than a graph walk.
ALTER TABLE tasks ADD COLUMN lineage_depth INTEGER NOT NULL DEFAULT 0;

-- Why this task exists: what created it, and from what.
ALTER TABLE tasks ADD COLUMN lineage_reason TEXT;

CREATE INDEX idx_tasks_parent ON tasks(parent_task_id);

CREATE INDEX idx_tasks_wait_for ON tasks(wait_for_task_id);

-- ---- Backfill ---------------------------------------------------------------
-- Corrections already recorded their source in metadata, so that ancestry is
-- recoverable and worth backfilling; depth follows from it. Anything else is left
-- as a root at depth 0 rather than guessed at.
UPDATE tasks SET parent_task_id = json_extract(metadata_json, '$.correction_of'),
                 lineage_reason = 'correction'
    WHERE json_extract(metadata_json, '$.correction_of') IS NOT NULL;

UPDATE tasks SET lineage_depth = 1
    WHERE parent_task_id IS NOT NULL AND parent_task_id != task_id;
