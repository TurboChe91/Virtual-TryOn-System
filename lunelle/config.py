"""Central configuration. All settings come from environment variables (optionally via .env).

Nothing else in the codebase reads os.environ directly for business settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_SIZE_RE = re.compile(r"^(\d{3,4})x(\d{3,4})$")

VALID_ENVS = ("development", "production", "test")
VALID_PROVIDERS = ("openai-compat", "mock")
VALID_REFERENCE_MODES = ("auto", "seedream", "openai-edits", "off")

DEFAULT_PRICING_USD = {
    # Rough per-image list prices; override with LUNELLE_PRICING_JSON.
    "doubao-seedream-4-0-250828": 0.03,
    "doubao-seedream-4-5-251128": 0.04,
    "doubao-seedream-5-0-260128": 0.05,
    "gpt-image-1": 0.17,
    "gpt-image-2": 0.19,
}
FALLBACK_PRICE_USD = 0.05


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _env_size(name: str, default: str) -> tuple[int, int]:
    raw = _env(name) or default
    m = _SIZE_RE.match(raw)
    if not m:
        raise ConfigError(f"{name} must look like 1024x1024, got {raw!r}")
    return int(m.group(1)), int(m.group(2))


@dataclass(frozen=True)
class Config:
    env: str
    debug: bool
    log_level: str

    host: str
    port: int
    admin_token: str

    data_dir: Path
    db_path: Path
    output_dir: Path
    upload_dir: Path
    export_dir: Path
    log_dir: Path

    image_api_base_url: str
    image_api_key: str
    image_model: str
    image_provider: str
    grid_size: tuple[int, int]
    wearing_size: tuple[int, int]
    reference_mode: str

    text_api_base_url: str
    text_api_key: str
    text_model: str

    max_concurrency: int
    max_retries: int
    retry_backoff_base_s: int
    request_timeout_s: int

    qa_min_side: int
    max_upload_mb: int
    disable_provider_watermark: bool

    pricing_usd: dict[str, float] = field(default_factory=dict)

    # ---- derived helpers -------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def key_fingerprint(self) -> str:
        """Short non-reversible identifier of the API key, safe for logs."""
        if not self.image_api_key:
            return "unset"
        return hashlib.sha256(self.image_api_key.encode()).hexdigest()[:8]

    def price_for(self, model: str) -> float:
        return self.pricing_usd.get(model, FALLBACK_PRICE_USD)

    def runtime_dirs(self) -> list[Path]:
        return [self.data_dir, self.output_dir, self.upload_dir, self.export_dir, self.log_dir]

    def ensure_dirs(self) -> None:
        for d in self.runtime_dirs():
            d.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def validate_for_serve(self) -> list[str]:
        """Return a list of human-readable problems that block serving real traffic."""
        problems: list[str] = []
        if self.image_provider == "mock":
            if self.is_production:
                problems.append(
                    "LUNELLE_IMAGE_PROVIDER=mock is refused in production; "
                    "use openai-compat with real credentials."
                )
        else:
            if not self.image_api_key:
                problems.append("LUNELLE_IMAGE_API_KEY is required for image generation.")
            if not self.image_api_base_url:
                problems.append("LUNELLE_IMAGE_API_BASE_URL is required for image generation.")
            elif not self.image_api_base_url.startswith("https://"):
                problems.append("LUNELLE_IMAGE_API_BASE_URL must use https://")
            if not self.image_model:
                problems.append("LUNELLE_IMAGE_MODEL is required for image generation.")
        if self.grid_size[0] != self.grid_size[1]:
            problems.append("LUNELLE_GRID_IMAGE_SIZE must be square (1:1), e.g. 2048x2048.")
        if self.is_production and self.debug:
            problems.append("LUNELLE_DEBUG must be 0 in production.")
        return problems


def load_config(dotenv_path: str | os.PathLike | None = None) -> Config:
    """Build config from the process environment (plus optional .env file)."""
    if dotenv_path is not None:
        load_dotenv(dotenv_path, override=False)
    else:
        load_dotenv(override=False)

    env = _env("LUNELLE_ENV", "development").lower()
    if env not in VALID_ENVS:
        raise ConfigError(f"LUNELLE_ENV must be one of {VALID_ENVS}, got {env!r}")

    provider = _env("LUNELLE_IMAGE_PROVIDER", "openai-compat").lower()
    if provider not in VALID_PROVIDERS:
        raise ConfigError(f"LUNELLE_IMAGE_PROVIDER must be one of {VALID_PROVIDERS}, got {provider!r}")

    reference_mode = _env("LUNELLE_REFERENCE_MODE", "auto").lower()
    if reference_mode not in VALID_REFERENCE_MODES:
        raise ConfigError(
            f"LUNELLE_REFERENCE_MODE must be one of {VALID_REFERENCE_MODES}, got {reference_mode!r}"
        )

    data_dir = Path(_env("LUNELLE_DATA_DIR", "./data")).expanduser().resolve()

    def sub(name: str, default: Path) -> Path:
        raw = _env(name)
        return Path(raw).expanduser().resolve() if raw else default

    pricing = dict(DEFAULT_PRICING_USD)
    pricing_raw = _env("LUNELLE_PRICING_JSON")
    if pricing_raw:
        try:
            overrides = json.loads(pricing_raw)
            if not isinstance(overrides, dict):
                raise ValueError("must be a JSON object")
            for k, v in overrides.items():
                pricing[str(k)] = float(v)
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"LUNELLE_PRICING_JSON is not a valid JSON object of prices: {exc}") from exc

    image_key = _env("LUNELLE_IMAGE_API_KEY")
    image_base = _env("LUNELLE_IMAGE_API_BASE_URL").rstrip("/")

    log_level = _env("LUNELLE_LOG_LEVEL", "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(f"LUNELLE_LOG_LEVEL invalid: {log_level!r}")

    return Config(
        env=env,
        debug=_env("LUNELLE_DEBUG", "0") == "1",
        log_level=log_level,
        host=_env("LUNELLE_HOST", "127.0.0.1"),
        port=_env_int("LUNELLE_PORT", 8300, 1, 65535),
        admin_token=_env("LUNELLE_ADMIN_TOKEN"),
        data_dir=data_dir,
        db_path=sub("LUNELLE_DB_PATH", data_dir / "lunelle.db"),
        output_dir=sub("LUNELLE_OUTPUT_DIR", data_dir / "outputs"),
        upload_dir=sub("LUNELLE_UPLOAD_DIR", data_dir / "uploads"),
        export_dir=sub("LUNELLE_EXPORT_DIR", data_dir / "exports"),
        log_dir=sub("LUNELLE_LOG_DIR", data_dir / "logs"),
        image_api_base_url=image_base,
        image_api_key=image_key,
        image_model=_env("LUNELLE_IMAGE_MODEL", "doubao-seedream-4-5-251128"),
        image_provider=provider,
        grid_size=_env_size("LUNELLE_GRID_IMAGE_SIZE", "2048x2048"),
        wearing_size=_env_size("LUNELLE_WEARING_IMAGE_SIZE", "2048x2048"),
        reference_mode=reference_mode,
        text_api_base_url=(_env("LUNELLE_TEXT_API_BASE_URL").rstrip("/") or image_base),
        text_api_key=(_env("LUNELLE_TEXT_API_KEY") or image_key),
        text_model=_env("LUNELLE_TEXT_MODEL"),
        max_concurrency=_env_int("LUNELLE_MAX_CONCURRENCY", 2, 1, 16),
        max_retries=_env_int("LUNELLE_MAX_RETRIES", 2, 0, 10),
        retry_backoff_base_s=_env_int("LUNELLE_RETRY_BACKOFF_BASE_S", 15, 1, 3600),
        request_timeout_s=_env_int("LUNELLE_REQUEST_TIMEOUT_S", 300, 10, 1800),
        qa_min_side=_env_int("LUNELLE_QA_MIN_SIDE", 1024, 64, 8192),
        disable_provider_watermark=_env("LUNELLE_DISABLE_PROVIDER_WATERMARK", "1") == "1",
        max_upload_mb=_env_int("LUNELLE_MAX_UPLOAD_MB", 10, 1, 100),
        pricing_usd=pricing,
    )
