-- Nail Slot assets: per-nail masks derived from the colour annotation set.
--
-- Why these tables exist: a matrix cell is a local edit of one base hand photo,
-- and until now "which nail is which" lived only in prompt prose ("LEFT upper
-- SCREEN LEFT-to-RIGHT = nail-05, nail-04, ..."). That asks the model to count
-- fingers, which it gets wrong. A per-nail mask binds identity to geometry, so
-- there is nothing left to miscount.
--
-- The masks are DERIVED, never uploaded: `scripts/import_hand_models.py` reads a
-- colour-annotated photo, maps each exact RGB to a nail id via the code-side
-- contract in `lunelle/nailslots.py`, and emits one mask per nail. The colour ->
-- nail-id mapping is a versioned code constant rather than an uploaded asset,
-- so changing it is a reviewable code change and not a silent asset swap.

CREATE TABLE hand_models (
    hand_model_id   TEXT PRIMARY KEY,
    tone            TEXT NOT NULL,
    view            TEXT NOT NULL,
    -- Bumped when the underlying photo changes. Snapshots reference a specific
    -- revision, so re-importing a corrected base cannot rewrite what a past task
    -- was generated from (the Phase 3 immutability rule).
    revision        INTEGER NOT NULL DEFAULT 1,

    -- The clean photo actually sent to the provider. Content-addressed.
    base_digest     TEXT NOT NULL REFERENCES assets(digest),
    -- The colour-annotated source the masks were derived from. Kept for
    -- provenance and re-derivation; NEVER sent to a provider.
    annotation_digest TEXT REFERENCES assets(digest),

    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    -- Free-form note: where the files came from, who checked them.
    source_note     TEXT,
    created_at      TEXT NOT NULL,
    -- Set when superseded by a newer revision; retired rows stay readable so old
    -- snapshots continue to resolve.
    retired_at      TEXT,
    UNIQUE (tone, view, revision)
);

CREATE INDEX idx_hand_models_lookup ON hand_models(tone, view, retired_at);

CREATE TABLE hand_model_slots (
    hand_model_id   TEXT NOT NULL REFERENCES hand_models(hand_model_id),
    nail_id         TEXT NOT NULL,
    -- Physiological binding, stored rather than inferred: nail-01 is the LEFT
    -- thumb in every view. Screen position varies by pose (p2's left hand runs
    -- 05,04,03,02 while p4's runs 02,03,04,05 — both verified from pixels), so
    -- screen order can never define identity.
    hand            TEXT NOT NULL CHECK (hand IN ('left', 'right')),
    finger          TEXT NOT NULL CHECK (finger IN
                        ('thumb', 'index', 'middle', 'ring', 'pinky')),

    -- The derived mask, content-addressed like every other input asset.
    mask_digest     TEXT NOT NULL REFERENCES assets(digest),

    -- Geometry of the mask region, so callers can crop a single nail for QA
    -- without decoding the mask.
    bbox_x          INTEGER NOT NULL,
    bbox_y          INTEGER NOT NULL,
    bbox_w          INTEGER NOT NULL,
    bbox_h          INTEGER NOT NULL,
    area_px         INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (hand_model_id, nail_id)
);

CREATE INDEX idx_slots_model ON hand_model_slots(hand_model_id);
