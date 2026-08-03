"""OpenAI-compatible images adapter (official OpenAI, wisech/Seedream, geek2api, ...).

Wire formats supported for passing a design reference image:
- "seedream": JSON body of /images/generations carries an ``image`` array of
  data URLs (Volcano Ark / Seedream convention exposed by aggregators);
- "openai-edits": multipart /images/edits with image[] files (OpenAI convention);
- "off": text-only.
``auto`` picks seedream for doubao-seedream* models, otherwise openai-edits.

Error classification maps HTTP/network conditions onto the retryability model
in lunelle.models. The API key never appears in logs; only a fingerprint does.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from dataclasses import dataclass

import httpx

from ..logging_setup import redact
from ..urlguard import UnsafeUrl, guard_request_url
from .base import GenerationRequest, GenerationResult, ImageProvider, ProviderError

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def _guard(url: str, *, stage: str) -> None:
    """Validate an outbound URL, as a classified ProviderError on refusal.

    Non-retryable: a URL pointing at internal space will point there next time
    too, and retrying a blocked request only repeats the attempt.
    """
    try:
        guard_request_url(url)
    except UnsafeUrl as exc:
        raise ProviderError(
            "unsafe_url", f"refusing unsafe {stage} url: {exc}", retryable=False
        ) from exc


@dataclass(frozen=True)
class OpenAICompatSettings:
    base_url: str
    api_key: str
    timeout_s: int
    reference_mode: str  # auto | seedream | openai-edits | off
    disable_watermark: bool = True


def _classify_http(status: int, body_text: str) -> ProviderError:
    snippet = redact(body_text[:600])
    lowered_body = body_text.lower()
    # Some aggregators report unknown models as 5xx; retrying cannot help.
    if any(marker in lowered_body for marker in ("model_not_found", "invalid model", "unknown model")):
        return ProviderError(
            "config_error", f"model not available at provider (HTTP {status}): {snippet}",
            retryable=False, http_status=status,
        )
    if status in (401, 403):
        return ProviderError(
            "auth_invalid", f"provider rejected credentials (HTTP {status}): {snippet}",
            retryable=False, http_status=status,
        )
    if status == 429:
        return ProviderError(
            "rate_limited", f"provider rate limit (HTTP 429): {snippet}",
            retryable=True, http_status=status,
        )
    if status >= 500:
        return ProviderError(
            "server_error", f"provider server error (HTTP {status}): {snippet}",
            retryable=True, http_status=status,
        )
    lowered = body_text.lower()
    if any(marker in lowered for marker in ("content_policy", "safety", "moderation")):
        return ProviderError(
            "content_policy", f"provider content policy rejection (HTTP {status}): {snippet}",
            retryable=False, http_status=status,
        )
    return ProviderError(
        "bad_request", f"provider rejected request (HTTP {status}): {snippet}",
        retryable=False, http_status=status,
    )


def _classify_transport(exc: Exception) -> ProviderError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", f"provider request timed out: {exc}", retryable=True)
    if isinstance(exc, httpx.ConnectError):
        message = str(exc)
        code = "dns" if "getaddrinfo" in message or "Name or service" in message else "network"
        return ProviderError(code, f"connection failed: {redact(message)}", retryable=True)
    if isinstance(exc, httpx.HTTPError):
        return ProviderError("network", f"network error: {redact(str(exc))}", retryable=True)
    return ProviderError("network", f"unexpected transport error: {redact(str(exc))}", retryable=True)


def _data_url(path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{encoded}"


class OpenAICompatProvider(ImageProvider):
    name = "openai-compat"

    def __init__(self, settings: OpenAICompatSettings):
        if not settings.api_key or not settings.base_url:
            raise ProviderError(
                "config_error", "image API base URL and key must be configured", retryable=False
            )
        self.settings = settings

    # -- public API -----------------------------------------------------------

    def generate(self, request: GenerationRequest) -> GenerationResult:
        mode = self._effective_mode(request)
        if mode == "seedream" and request.reference_images:
            try:
                return self._generations(request, include_reference=True)
            except ProviderError as exc:
                if exc.code == "bad_request" and "image" in exc.message.lower():
                    # Endpoint does not understand the image param — one text-only retry.
                    logger.warning(
                        "provider rejected reference param; retrying text-only",
                        extra={"ctx": {"task_id": request.task_id, "stage": "reference_fallback"}},
                    )
                    return self._generations(request, include_reference=False)
                raise
        if mode == "openai-edits" and request.reference_images:
            return self._edits(request)
        return self._generations(request, include_reference=False)

    # -- internals --------------------------------------------------------------

    def _effective_mode(self, request: GenerationRequest) -> str:
        if not request.reference_images:
            return "off"
        mode = self.settings.reference_mode
        if mode == "auto":
            return "seedream" if request.model.startswith("doubao-seedream") else "openai-edits"
        return mode

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.api_key}"}

    def _post(self, url: str, *, json_body=None, files=None, data=None) -> httpx.Response:
        # Re-validated per request, not just when this provider was built: the
        # base URL comes from an operator-editable profile and DNS can change
        # between the two.
        _guard(url, stage="request")
        try:
            with httpx.Client(timeout=self.settings.timeout_s) as client:
                return client.post(
                    url, headers=self._headers(), json=json_body, files=files, data=data,
                    # A 302 to an internal address would bypass the check above:
                    # only the first URL is ours to validate.
                    follow_redirects=False,
                )
        except Exception as exc:  # noqa: BLE001 - classified below
            raise _classify_transport(exc) from exc

    def _full_prompt(self, request: GenerationRequest) -> str:
        # Not every OpenAI-compatible endpoint accepts negative_prompt; fold it in.
        if request.negative_prompt:
            return f"{request.prompt}\n\nDo NOT include any of: {request.negative_prompt}."
        return request.prompt

    def _generations(self, request: GenerationRequest, *, include_reference: bool) -> GenerationResult:
        body = {
            "model": request.model,
            "prompt": self._full_prompt(request),
            "size": f"{request.size[0]}x{request.size[1]}",
            "n": 1,
            "response_format": "b64_json",
        }
        if include_reference:
            body["image"] = [_data_url(p) for p in request.reference_images]
        if self.settings.disable_watermark and request.model.startswith("doubao-seedream"):
            body["watermark"] = False  # Volcano Ark param; ignored by other providers
        body.update(request.extra)
        response = self._post(f"{self.settings.base_url}/images/generations", json_body=body)
        return self._parse_response(response, request, reference_used=include_reference)

    def _edits(self, request: GenerationRequest) -> GenerationResult:
        files = []
        for path in request.reference_images:
            mime = mimetypes.guess_type(str(path))[0] or "image/png"
            files.append(("image[]", (path.name, path.read_bytes(), mime)))
        data = {
            "model": request.model,
            "prompt": self._full_prompt(request),
            "size": f"{request.size[0]}x{request.size[1]}",
            "n": "1",
        }
        data.update({k: str(v) for k, v in request.extra.items()})
        response = self._post(f"{self.settings.base_url}/images/edits", files=files, data=data)
        return self._parse_response(response, request, reference_used=True)

    def _parse_response(
        self, response: httpx.Response, request: GenerationRequest, *, reference_used: bool
    ) -> GenerationResult:
        if response.status_code != 200:
            raise _classify_http(response.status_code, response.text)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "invalid_response",
                f"provider returned non-JSON body: {redact(response.text[:300])}",
                retryable=False, http_status=response.status_code,
            ) from exc

        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ProviderError(
                "invalid_response",
                f"provider response missing data[0]: keys={sorted(payload)[:10]}",
                retryable=False, http_status=response.status_code,
            )
        entry = data[0]
        image_bytes: bytes | None = None
        if entry.get("b64_json"):
            try:
                image_bytes = base64.b64decode(entry["b64_json"])
            except (ValueError, TypeError) as exc:
                raise ProviderError(
                    "invalid_response", "b64_json payload is not valid base64", retryable=False
                ) from exc
        elif entry.get("url"):
            image_bytes = self._download(entry["url"])
        if not image_bytes:
            raise ProviderError(
                "invalid_response", "provider returned neither b64_json nor url", retryable=False
            )

        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        meta = {
            "created": payload.get("created"),
            "usage": usage,
            "size_requested": f"{request.size[0]}x{request.size[1]}",
            "revised_prompt_present": bool(entry.get("revised_prompt")),
        }
        external_id = (
            response.headers.get("x-request-id")
            or response.headers.get("x-oneapi-request-id")
            or payload.get("id")
        )
        return GenerationResult(
            image_bytes=image_bytes,
            external_request_id=str(external_id) if external_id else None,
            actual_cost_usd=None,  # OpenAI-compatible image APIs do not return spend
            reference_used=reference_used,
            response_meta=meta,
        )

    def _download(self, url: str) -> bytes:
        """Fetch a provider-supplied image URL.

        The URL comes from the provider's response body, so it is fully untrusted:
        a compromised or hostile endpoint returning
        `https://169.254.169.254/latest/meta-data` would have the server fetch it
        and store the result as an image. An `https://` prefix check alone (what
        this used to do) does not stop that.
        """
        _guard(url, stage="download")
        try:
            # No redirects: image URLs are direct object-storage links; redirects widen SSRF surface.
            with httpx.Client(timeout=self.settings.timeout_s, follow_redirects=False) as client:
                with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise ProviderError(
                            "download_failed",
                            f"image download failed with HTTP {response.status_code}",
                            retryable=True, http_status=response.status_code,
                        )
                    chunks = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ProviderError(
                                "download_failed", "image download exceeded 50MB cap", retryable=False
                            )
                        chunks.append(chunk)
                    return b"".join(chunks)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            raise _classify_transport(exc) from exc
