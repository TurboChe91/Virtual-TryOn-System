"""Cloudflare R2 + D1 client for publishing try-on assets to the deployed
Worker API (api.finglow.cn).

The Worker itself is read-only (see its docs): publishing means writing the
image objects into the `tryon-assets` R2 bucket and the style/asset rows into
the `tryon-db` D1 database through Cloudflare's REST API. Credentials live in
app_settings (managed from the settings page) and never leave this module
unredacted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

from .db import Database, transaction, utcnow

API = "https://api.cloudflare.com/client/v4"

CF_KEYS = ("cf_account_id", "cf_api_token", "cf_d1_database_id", "cf_r2_bucket")


class CloudflareError(Exception):
    pass


@dataclass(frozen=True)
class CloudflareConfig:
    account_id: str
    api_token: str
    d1_database_id: str
    r2_bucket: str


def load_cf_config(db: Database) -> CloudflareConfig | None:
    conn = db.conn()
    values: dict[str, str] = {}
    for key in CF_KEYS:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        if row is None or not row["value"].strip():
            return None
        values[key] = row["value"].strip()
    return CloudflareConfig(
        account_id=values["cf_account_id"],
        api_token=values["cf_api_token"],
        d1_database_id=values["cf_d1_database_id"],
        r2_bucket=values["cf_r2_bucket"],
    )


def save_cf_config(db: Database, fields: dict) -> None:
    conn = db.conn()
    with transaction(conn):
        for key in CF_KEYS:
            value = str(fields.get(key) or "").strip()
            if not value:
                continue
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (key, value, utcnow()),
            )


def cf_config_public(db: Database) -> dict:
    conn = db.conn()
    out: dict = {}
    for key in CF_KEYS:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        value = row["value"].strip() if row else ""
        if key == "cf_api_token":
            out[key] = {"set": bool(value),
                        "fingerprint": hashlib.sha256(value.encode()).hexdigest()[:8] if value else None}
        else:
            out[key] = value
    out["configured"] = load_cf_config(db) is not None
    return out


class CloudflareClient:
    def __init__(self, config: CloudflareConfig, timeout: int = 120):
        self.config = config
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_token}"}

    def r2_put(self, key: str, data: bytes, content_type: str) -> None:
        url = (f"{API}/accounts/{self.config.account_id}/r2/buckets/"
               f"{self.config.r2_bucket}/objects/{key}")
        try:
            response = httpx.put(
                url, content=data,
                headers={**self._headers(), "Content-Type": content_type},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise CloudflareError(f"R2 upload transport error: {exc}") from exc
        if response.status_code != 200:
            raise CloudflareError(
                f"R2 upload failed for {key}: HTTP {response.status_code} {response.text[:200]}"
            )

    def d1_query(self, sql: str, params: list | None = None) -> list[dict]:
        url = (f"{API}/accounts/{self.config.account_id}/d1/database/"
               f"{self.config.d1_database_id}/query")
        try:
            response = httpx.post(
                url, json={"sql": sql, "params": params or []},
                headers=self._headers(), timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise CloudflareError(f"D1 transport error: {exc}") from exc
        is_json = response.headers.get("content-type", "").startswith("application/json")
        doc = response.json() if is_json else {}
        if response.status_code != 200 or not doc.get("success"):
            errors = doc.get("errors") or [{"message": response.text[:200]}]
            raise CloudflareError(f"D1 query failed: {errors}")
        results = doc.get("result") or []
        return results[0].get("results", []) if results else []

    def test(self) -> dict:
        """Cheap read-only probe of both bindings."""
        out: dict = {}
        try:
            rows = self.d1_query(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name IN ('tryon_styles','tryon_assets')"
            )
            out["d1"] = {"ok": True, "tables": sorted(r["name"] for r in rows)}
        except CloudflareError as exc:
            out["d1"] = {"ok": False, "detail": str(exc)[:200]}
        try:
            url = (f"{API}/accounts/{self.config.account_id}/r2/buckets/"
                   f"{self.config.r2_bucket}")
            response = httpx.get(url, headers=self._headers(), timeout=30)
            out["r2"] = {"ok": response.status_code == 200,
                         "http_status": response.status_code}
        except httpx.HTTPError as exc:
            out["r2"] = {"ok": False, "detail": str(exc)[:200]}
        out["ok"] = bool(out["d1"].get("ok") and out["r2"].get("ok"))
        return out
