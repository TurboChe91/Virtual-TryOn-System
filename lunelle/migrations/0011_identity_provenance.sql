-- Per-nail identity needs a provenance flag.
--
-- identity_text is the design contract every matrix cell is generated against:
-- it names each nail's base colour, motif and decoration counts. It is written by
-- a vision model reading the plan image, and that model makes mistakes -- on the
-- first real style it derived, it reversed the wave direction on nail-01 and
-- nail-03 and got nail-03's rhinestone count wrong. Those errors were then
-- faithfully executed by the image model, which looks exactly like a generation
-- fault and is very hard to attribute.
--
-- Without this column there is no way to distinguish "a model guessed this" from
-- "a human checked it", so a guess silently carries the authority of a fact.

ALTER TABLE styles ADD COLUMN identity_source TEXT
    CHECK (identity_source IN ('vision-llm', 'human', 'imported'));

-- Set when a human has actually reviewed the text against the plan image.
-- NULL means unverified, whatever the source.
ALTER TABLE styles ADD COLUMN identity_verified_at TEXT;

ALTER TABLE styles ADD COLUMN identity_verified_by TEXT;

-- Existing rows: the one style with identity text got it from the vision model,
-- and nobody has checked it. Recording that honestly is the point of the column --
-- backfilling it as verified would defeat it.
UPDATE styles SET identity_source = 'vision-llm'
    WHERE identity_text IS NOT NULL AND identity_source IS NULL;
