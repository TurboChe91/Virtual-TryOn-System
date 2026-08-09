-- Which code produced an image, recorded with the image.
--
-- On 2026-08-06 four matrix cells were rendered for $0.76 by a worker started two
-- hours before the code it was supposed to be testing existed. Python does not
-- hot-reload, so the process sent the old Image 1 while the prompt -- frozen at
-- queue time by the new code -- described a view-plan that was never sent. In this
-- table that run is indistinguishable from a correct one: prompt_version reads
-- 'mx-2+8176b704', exactly as a real mx-2 render would.
--
-- prompt_version cannot close this. It is stamped when a task is QUEUED, so it
-- describes the code that planned the work, never the code that ran it.
--
-- Nor can a git SHA read at execution time: the commit landed at 17:16 UTC and the
-- renders happened at 17:42, so it would have returned the new SHA and certified
-- the invalid run. runtime_tree_sha is the authority instead -- a digest over the
-- .py/.json/.sql tree taken when the process imported lunelle.build, which is a
-- property of the bytes that were loaded rather than of the repository's opinion.

ALTER TABLE task_executions ADD COLUMN build_id TEXT;

-- Full digest over the runtime tree at import. The authority for "which code".
ALTER TABLE task_executions ADD COLUMN runtime_tree_sha TEXT;

-- Human-readable correlation with history. May be NULL (installed wheel with no
-- .git) or ahead of the loaded tree, which is exactly the 2026-08-06 case.
ALTER TABLE task_executions ADD COLUMN git_sha TEXT;

ALTER TABLE task_executions ADD COLUMN git_dirty INTEGER;

-- sha256 of `git diff HEAD` plus the untracked runtime-file list. Identifies an
-- uncommitted working tree without storing the diff itself.
ALTER TABLE task_executions ADD COLUMN diff_sha256 TEXT;

-- Interpreter and Pillow/httpx/pydantic versions. A Pillow upgrade under a running
-- process changes rendered output with no source change at all.
ALTER TABLE task_executions ADD COLUMN dependency_sha256 TEXT;

ALTER TABLE task_executions ADD COLUMN process_id INTEGER;

-- Worker thread that ran this attempt (lunelle-worker-N). Tells concurrent
-- renders apart within one process.
ALTER TABLE task_executions ADD COLUMN worker_instance TEXT;

-- 1 when the tree on disk had already diverged from the loaded build at execution
-- time. The worker refuses to claim under drift unless LUNELLE_ALLOW_CODE_DRIFT=1,
-- so this is normally 0 -- but when it is 1 the row says so instead of looking
-- like a clean render.
ALTER TABLE task_executions ADD COLUMN runtime_drift_detected INTEGER;

-- "Every execution on this build" is the question this table could not answer.
CREATE INDEX idx_executions_build ON task_executions(build_id);

-- No backfill, deliberately. The build identity of past executions is not
-- knowable: the tree those processes loaded is gone, and the current tree is not
-- it. NULL reads as "this execution's code identity was never recorded", which is
-- the true state of all 36 historical rows -- including the four cells from
-- 2026-08-06. Writing today's SHA into them would manufacture the exact false
-- certainty this migration exists to remove.
