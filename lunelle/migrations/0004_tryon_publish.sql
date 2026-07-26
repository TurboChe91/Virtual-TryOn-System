-- Lunelle Studio schema v4: link styles to the deployed try-on Worker
-- (api.finglow.cn), which identifies styles by a fixed 3-digit id.

ALTER TABLE styles ADD COLUMN tryon_style_id TEXT;

CREATE UNIQUE INDEX idx_styles_tryon_id ON styles(tryon_style_id)
    WHERE tryon_style_id IS NOT NULL;
