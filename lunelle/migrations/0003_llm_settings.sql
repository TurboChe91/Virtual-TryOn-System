-- Lunelle Studio schema v3: LLM channel profiles and app-level settings
-- (per-skin-tone hand model images live in app_settings + upload_dir).

ALTER TABLE api_profiles ADD COLUMN kind TEXT NOT NULL DEFAULT 'image' CHECK (kind IN ('image', 'llm'));

CREATE TABLE app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
