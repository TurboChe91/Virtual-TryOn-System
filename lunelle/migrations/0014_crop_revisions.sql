-- Versioned 2x5 plan splits. Automatic CV output is evidence, not mutable state:
-- every generated task records the exact revision whose boxes produced Image 1.

CREATE TABLE crop_revisions (
    crop_revision_id       TEXT PRIMARY KEY,
    style_id               TEXT NOT NULL REFERENCES styles(style_id),
    revision_number        INTEGER NOT NULL,
    source_plan_digest     TEXT NOT NULL REFERENCES assets(digest),
    source                  TEXT NOT NULL CHECK (source IN ('auto', 'manual')),
    confidence              REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    gate_status             TEXT NOT NULL CHECK (gate_status IN ('pass', 'review_required')),
    gate_reasons_json       TEXT NOT NULL DEFAULT '[]',
    review_state            TEXT NOT NULL CHECK (
                                review_state IN ('auto_approved', 'waiting_review',
                                                 'approved', 'rejected')),
    boxes_json              TEXT NOT NULL,
    preview_digest          TEXT REFERENCES assets(digest),
    contact_sheet_digest    TEXT REFERENCES assets(digest),
    created_by              TEXT NOT NULL,
    approved_by             TEXT,
    created_at              TEXT NOT NULL,
    approved_at             TEXT,
    UNIQUE (style_id, revision_number)
);

CREATE INDEX idx_crop_revisions_style
    ON crop_revisions(style_id, revision_number DESC);
CREATE INDEX idx_crop_revisions_plan
    ON crop_revisions(style_id, source_plan_digest, review_state);

CREATE TABLE nail_crops (
    crop_revision_id   TEXT NOT NULL REFERENCES crop_revisions(crop_revision_id),
    nail_id            TEXT NOT NULL,
    bbox_json          TEXT NOT NULL,
    rotation_degrees   REAL NOT NULL DEFAULT 0,
    asset_digest       TEXT NOT NULL REFERENCES assets(digest),
    PRIMARY KEY (crop_revision_id, nail_id),
    CHECK (nail_id GLOB 'nail-0[1-9]' OR nail_id = 'nail-10')
);

CREATE INDEX idx_nail_crops_digest ON nail_crops(asset_digest);
