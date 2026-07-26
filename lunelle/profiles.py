"""Runtime API channel profiles: stored in SQLite, editable from the admin UI.

A profile bundles one image-API channel (base URL + key + model + capabilities).
Exactly one profile may be active; the worker resolves the provider per task so
switching channels never needs a restart. When no profile is active the process
falls back to the environment configuration (Config), which keeps existing
deployments and the test suite working unchanged.

API keys never leave this module unredacted: list/get return a fingerprint and
the last four characters only.
"""

from __future__ import annotations

import hashlib
import sqlite3

from .config import Config
from .db import Database, transaction, utcnow
from .errors import ConflictError, NotFoundError
from .models import new_id
from .providers import ImageProvider
from .providers.openai_compat import OpenAICompatProvider, OpenAICompatSettings

VALID_PROFILE_REFERENCE_MODES = ("auto", "seedream", "openai-edits", "off")


def _redact_key(api_key: str) -> dict:
    return {
        "fingerprint": hashlib.sha256(api_key.encode()).hexdigest()[:8],
        "last4": api_key[-4:] if len(api_key) >= 8 else "****",
    }


def _row_public(row: sqlite3.Row) -> dict:
    doc = dict(row)
    api_key = doc.pop("api_key")
    doc["api_key"] = _redact_key(api_key)
    doc["supports_mask"] = bool(doc["supports_mask"])
    doc["is_active"] = bool(doc["is_active"])
    return doc


class ProfileService:
    def __init__(self, db: Database):
        self.db = db

    # ---------------- queries ----------------

    def list_profiles(self, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self.db.conn().execute(
                "SELECT * FROM api_profiles WHERE kind = ? ORDER BY created_at", (kind,)
            ).fetchall()
        else:
            rows = self.db.conn().execute(
                "SELECT * FROM api_profiles ORDER BY created_at"
            ).fetchall()
        return [_row_public(r) for r in rows]

    def get_public(self, profile_id: str) -> dict:
        return _row_public(self._get_row(profile_id))

    def _get_row(self, profile_id: str) -> sqlite3.Row:
        row = self.db.conn().execute(
            "SELECT * FROM api_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"profile {profile_id} not found")
        return row

    def active_row(self, kind: str = "image") -> sqlite3.Row | None:
        return self.db.conn().execute(
            "SELECT * FROM api_profiles WHERE is_active = 1 AND kind = ? LIMIT 1", (kind,)
        ).fetchone()

    # ---------------- mutations ----------------

    def create(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        kind: str = "image",
        reference_mode: str = "auto",
        supports_mask: bool = False,
        price_per_image_usd: float | None = None,
    ) -> dict:
        base_url = base_url.rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("base_url must use https://")
        if reference_mode not in VALID_PROFILE_REFERENCE_MODES:
            raise ValueError(f"reference_mode must be one of {VALID_PROFILE_REFERENCE_MODES}")
        if kind not in ("image", "llm"):
            raise ValueError("kind must be 'image' or 'llm'")
        profile_id = new_id("pr")
        now = utcnow()
        conn = self.db.conn()
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO api_profiles (profile_id, name, base_url, api_key, model, kind,"
                    " reference_mode, supports_mask, price_per_image_usd, is_active,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
                    (profile_id, name, base_url, api_key, model, kind, reference_mode,
                     1 if supports_mask else 0, price_per_image_usd, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"profile name {name!r} already exists") from exc
        return self.get_public(profile_id)

    def update(self, profile_id: str, fields: dict) -> dict:
        allowed = {
            "name", "base_url", "api_key", "model", "reference_mode",
            "supports_mask", "price_per_image_usd",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "base_url" in updates:
            updates["base_url"] = str(updates["base_url"]).rstrip("/")
            if not updates["base_url"].startswith("https://"):
                raise ValueError("base_url must use https://")
        if "reference_mode" in updates and updates["reference_mode"] not in VALID_PROFILE_REFERENCE_MODES:
            raise ValueError(f"reference_mode must be one of {VALID_PROFILE_REFERENCE_MODES}")
        if "supports_mask" in updates:
            updates["supports_mask"] = 1 if updates["supports_mask"] else 0
        if not updates:
            return self.get_public(profile_id)
        self._get_row(profile_id)  # 404 before write
        sets = ", ".join(f"{column} = ?" for column in updates)
        conn = self.db.conn()
        try:
            with transaction(conn):
                conn.execute(  # noqa: S608 - column names filtered by allowlist above
                    f"UPDATE api_profiles SET {sets}, updated_at = ? WHERE profile_id = ?",  # noqa: S608
                    (*updates.values(), utcnow(), profile_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("profile name already exists") from exc
        return self.get_public(profile_id)

    def delete(self, profile_id: str) -> None:
        row = self._get_row(profile_id)
        if row["is_active"]:
            raise ConflictError("cannot delete the active profile; activate another one first")
        conn = self.db.conn()
        with transaction(conn):
            conn.execute("DELETE FROM api_profiles WHERE profile_id = ?", (profile_id,))

    def activate(self, profile_id: str) -> dict:
        row = self._get_row(profile_id)
        conn = self.db.conn()
        with transaction(conn):
            # One active channel per kind: an LLM profile never displaces the image one.
            conn.execute(
                "UPDATE api_profiles SET is_active = 0 WHERE is_active = 1 AND kind = ?",
                (row["kind"],),
            )
            conn.execute(
                "UPDATE api_profiles SET is_active = 1, updated_at = ? WHERE profile_id = ?",
                (utcnow(), profile_id),
            )
        return self.get_public(profile_id)

    def deactivate_all(self, kind: str | None = None) -> None:
        """Fall back to environment configuration (optionally one kind only)."""
        conn = self.db.conn()
        with transaction(conn):
            if kind:
                conn.execute(
                    "UPDATE api_profiles SET is_active = 0 WHERE is_active = 1 AND kind = ?",
                    (kind,),
                )
            else:
                conn.execute("UPDATE api_profiles SET is_active = 0 WHERE is_active = 1")


class ProviderResolver:
    """Per-task provider selection: active DB profile first, env config fallback.

    Provider objects are stateless httpx wrappers, so building one per profile
    revision is cheap; the cache exists only to avoid re-reading the row's key.
    """

    def __init__(self, config: Config, db: Database, fallback: ImageProvider):
        self.config = config
        self.profiles = ProfileService(db)
        self.fallback = fallback
        self._cache_key: tuple[str, str, str] | None = None
        self._cache_provider: ImageProvider | None = None

    def resolve(self) -> tuple[ImageProvider, dict | None]:
        """Return (provider, active_profile_public_or_None)."""
        row = self.profiles.active_row()
        if row is None:
            self._cache_key = None
            self._cache_provider = None
            return self.fallback, None
        # Keyed on the connection-relevant fields (timestamps are second-precision
        # and could collide on a rapid create-then-update sequence).
        cache_key = (row["base_url"], row["api_key"], row["reference_mode"])
        if cache_key != self._cache_key or self._cache_provider is None:
            self._cache_provider = OpenAICompatProvider(
                OpenAICompatSettings(
                    base_url=row["base_url"],
                    api_key=row["api_key"],
                    timeout_s=self.config.request_timeout_s,
                    reference_mode=row["reference_mode"],
                    disable_watermark=self.config.disable_provider_watermark,
                )
            )
            self._cache_key = cache_key
        return self._cache_provider, _row_public(row)
