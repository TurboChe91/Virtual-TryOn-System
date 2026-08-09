-- What was actually sent to the provider, measured at the call boundary.
--
-- The snapshot froze a plan; the worker re-derived its inputs at execution time
-- from the current style row, app_settings and the viewplan cache. Nothing compared
-- the two, so for one matrix cell "what was Image 1" had four answers: the raw plan
-- in the snapshot's input_assets, a prompt in the SAME snapshot describing a
-- captioned view-plan, the raw plan actually sent, and the raw plan again re-read by
-- QA. The four cells of 2026-08-06 were rendered against a prompt/image combination
-- that existed in no version of the code.
--
-- These columns record the measured request -- each image re-hashed as it goes out,
-- not the snapshot's digests copied forward, which would only prove the snapshot
-- equals itself.

-- sha256 of the prompt text as sent. Promoted from execution_json so a drift is a
-- query rather than a JSON scan.
ALTER TABLE task_executions ADD COLUMN prompt_sha256 TEXT;

-- "2224x1664" as requested. The worker used to recompute size at execution time
-- from the current app_settings hand model, so it could differ from the snapshot's.
ALTER TABLE task_executions ADD COLUMN size_requested TEXT;

-- Fingerprint over profile_id + base_url + key (never the key itself). A profile
-- row is edited in place, so the same profile_id can point at a different endpoint
-- with a different key tomorrow.
ALTER TABLE task_executions ADD COLUMN channel_fingerprint TEXT;

-- The input_fingerprint of the snapshot this execution was checked against, so an
-- execution row can be joined back to the exact plan it claims to implement.
ALTER TABLE task_executions ADD COLUMN snapshot_fingerprint TEXT;

-- 1 when the measured request matched the snapshot. The provider is not called
-- when it does not, so a stored 0 means a blocked attempt -- which is a record
-- worth keeping, not an absence.
--
-- This replaces prompt_differs_from_snapshot in execution_json, which was declared
-- with the comment "filled by the caller if known" and never filled by any caller:
-- it read as null on every row ever written.
ALTER TABLE task_executions ADD COLUMN matches_snapshot INTEGER;

-- "view_plan:abc123def456 base_hand:789abc012def" in send order. Role AND digest:
-- a digest alone does not say what an image was used AS, and that conflation is
-- exactly how a raw plan was sent where a view-plan was promised.
ALTER TABLE task_executions ADD COLUMN input_digests TEXT;

-- "which cells were sent this exact view-plan" -- the question that would have
-- exposed the bad batch in one query.
CREATE INDEX idx_executions_inputs ON task_executions(input_digests);

CREATE INDEX idx_executions_snapshot ON task_executions(snapshot_fingerprint);

-- No backfill. The historical rows' resolved_assets recorded paths with kind
-- 'reference' for every input, so which one was the design authority and which the
-- base hand is not recoverable from them. NULL says "not measured", which is true.
