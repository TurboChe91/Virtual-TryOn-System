-- Lunelle Studio schema v9: a local ledger of publish attempts.
--
-- Publishing writes to two systems we do not own — R2 objects and D1 rows — over
-- several HTTP calls. The old flow uploaded to R2, then ran DELETE FROM
-- tryon_assets followed by one INSERT per cell as separate requests. A failure
-- after the DELETE left the style with its assets removed and not re-inserted, so
-- the customer-facing manifest went EMPTY while R2 still held the images. That is
-- the exact D1/R2 drift the Worker's docs already describe.
--
-- Two changes address it. The write pattern becomes upsert-in-place, so a cell's
-- row is replaced rather than deleted-then-recreated and the manifest is never
-- empty mid-publish. And this table records the intended end state before any
-- remote call, so a half-finished publish is visible and can be re-run to
-- completion instead of being guessed at.
--
-- Idempotence is what makes re-running safe: versioned R2 keys never overwrite,
-- and D1 rows upsert on a stable per-cell id.

CREATE TABLE publish_versions (
    publish_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    style_id        TEXT NOT NULL REFERENCES styles(style_id),
    tryon_style_id  TEXT NOT NULL,
    -- Monotonic per style. Also the R2 key suffix, so a republish cannot be
    -- served from a browser's year-long immutable cache of the previous image.
    version         INTEGER NOT NULL,
    -- planned  : intent recorded, nothing sent yet
    -- uploading: R2 writes in progress
    -- uploaded : every object is in R2, D1 not yet updated
    -- committed: D1 matches R2; the publish is complete
    -- failed   : gave up; `error` says where, and a re-run resumes
    state           TEXT NOT NULL CHECK (state IN
                        ('planned', 'uploading', 'uploaded', 'committed', 'failed')),
    -- The full intended end state: cells, task ids, R2 keys, D1 row ids. Recorded
    -- BEFORE the first remote call so recovery never has to infer it.
    plan_json       TEXT NOT NULL,
    -- What actually landed, appended as it goes.
    progress_json   TEXT NOT NULL DEFAULT '{}',
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    committed_at    TEXT,
    UNIQUE (style_id, version)
);

CREATE INDEX idx_publish_style ON publish_versions(style_id);

CREATE INDEX idx_publish_state ON publish_versions(state);

-- Which version each task actually reached production in, so `published` is not
-- the only local record of a publish.
ALTER TABLE tasks ADD COLUMN published_version INTEGER;
