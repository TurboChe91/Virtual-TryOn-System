"""A hostile provider must not be able to make the server fetch internal URLs.

The provider's response body is fully untrusted input. Before this guard,
`_download` only checked for an `https://` prefix, so an endpoint answering with
`{"data":[{"url":"https://169.254.169.254/latest/meta-data"}]}` would have the
server fetch cloud instance metadata and store the response as a nail image.
"""

from __future__ import annotations

import json

import httpx
import pytest

from lunelle.providers.base import GenerationRequest, ProviderError
from lunelle.providers.openai_compat import OpenAICompatProvider, OpenAICompatSettings
from lunelle.urlguard import configure_allow_private_hosts

HOSTILE_URLS = [
    "https://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "https://127.0.0.1:9000/secret.png",
    "https://localhost/secret.png",
    "https://10.0.0.5/internal.png",
    "https://192.168.1.1/router.png",
    "https://172.16.0.1/internal.png",
    "https://[::1]/secret.png",
    "https://[::ffff:127.0.0.1]/secret.png",
    "http://169.254.169.254/latest/meta-data",
    "file:///etc/passwd",
]


def _request() -> GenerationRequest:
    return GenerationRequest(
        prompt="ten nails", negative_prompt="", size=(512, 512),
        model="doubao-seedream-4-5-251128", task_id="tk_probe",
        reference_images=[],
    )


class RecordingTransport(httpx.BaseTransport):
    """Answers the generations call with an attacker-chosen image URL, and records
    every request so a test can assert the download never happened."""

    def __init__(self, image_url: str):
        self.image_url = image_url
        self.requests: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if request.url.path.endswith("/images/generations"):
            body = {"data": [{"url": self.image_url}], "id": "req-1"}
            return httpx.Response(200, json=body)
        # Any other request means the guard failed to stop the download.
        return httpx.Response(200, content=b"SENSITIVE-INTERNAL-DATA")


@pytest.fixture(autouse=True)
def _default_policy():
    """Every test starts from the blocking default and restores it after."""
    configure_allow_private_hosts(False)
    yield
    configure_allow_private_hosts(False)


def _provider(monkeypatch, transport: RecordingTransport) -> OpenAICompatProvider:
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)
    return OpenAICompatProvider(OpenAICompatSettings(
        base_url="https://example.com/v1", api_key="sk-test-not-real",
        timeout_s=5, reference_mode="off",
    ))


class TestHostileImageUrl:
    @pytest.mark.parametrize("url", HOSTILE_URLS)
    def test_provider_returned_internal_url_is_refused(self, monkeypatch, url):
        transport = RecordingTransport(url)
        provider = _provider(monkeypatch, transport)

        with pytest.raises(ProviderError) as excinfo:
            provider.generate(_request())

        assert excinfo.value.code == "unsafe_url"
        # Non-retryable: the URL will point at the same place next time.
        assert excinfo.value.retryable is False
        # The generations call happened; the download must NOT have.
        assert len(transport.requests) == 1, (
            f"download was attempted: {transport.requests}")
        assert "images/generations" in transport.requests[0]

    def test_a_public_image_url_still_downloads(self, monkeypatch):
        """The guard must not break the normal path."""
        transport = RecordingTransport("https://example.com/generated.png")
        provider = _provider(monkeypatch, transport)
        result = provider.generate(_request())
        assert result.image_bytes == b"SENSITIVE-INTERNAL-DATA"  # the stub's body
        assert len(transport.requests) == 2

    def test_escape_hatch_permits_a_private_image_url(self, monkeypatch):
        """A deliberately self-hosted provider on a trusted LAN still works."""
        configure_allow_private_hosts(True)
        transport = RecordingTransport("https://192.168.1.50/generated.png")
        provider = _provider(monkeypatch, transport)
        result = provider.generate(_request())
        assert result.image_bytes
        assert len(transport.requests) == 2

    def test_metadata_url_refused_even_with_the_escape_hatch_closed_again(
        self, monkeypatch
    ):
        """Re-tightening the policy takes effect immediately, with no restart."""
        configure_allow_private_hosts(True)
        configure_allow_private_hosts(False)
        transport = RecordingTransport("https://169.254.169.254/latest/meta-data")
        provider = _provider(monkeypatch, transport)
        with pytest.raises(ProviderError, match="unsafe"):
            provider.generate(_request())
        assert len(transport.requests) == 1


class TestBaseUrlGuardedPerRequest:
    def test_generations_call_to_an_internal_base_url_is_refused(self, monkeypatch):
        """A provider object may outlive a policy change or a DNS change, so the
        base URL is validated per request, not only at construction."""
        transport = RecordingTransport("https://example.com/x.png")
        real_client = httpx.Client

        def client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", client_factory)
        provider = OpenAICompatProvider(OpenAICompatSettings(
            base_url="https://169.254.169.254/v1", api_key="sk-test-not-real",
            timeout_s=5, reference_mode="off",
        ))
        with pytest.raises(ProviderError) as excinfo:
            provider.generate(_request())
        assert excinfo.value.code == "unsafe_url"
        # Zero requests: refused before any traffic left the process.
        assert transport.requests == []


class TestChatFnGuard:
    def test_build_chat_fn_refuses_an_internal_text_api(self, tmp_path):
        from lunelle.providers import build_chat_fn
        from lunelle.urlguard import UnsafeUrl
        from tests.conftest import make_config

        config = make_config(
            tmp_path, text_api_base_url="https://169.254.169.254/v1",
            text_api_key="sk-test-not-real", text_model="gpt-4o-mini",
        )
        chat = build_chat_fn(config)
        assert chat is not None
        with pytest.raises(UnsafeUrl, match="link-local"):
            chat("system", "user")

    def test_llm_chat_channel_refuses_an_internal_base_url(self, tmp_path, db):
        from lunelle.llm import build_llm_chat
        from lunelle.urlguard import UnsafeUrl
        from tests.conftest import make_config

        config = make_config(
            tmp_path, text_api_base_url="https://10.0.0.9/v1",
            text_api_key="sk-test-not-real", text_model="vision-model",
        )
        chat = build_llm_chat(config, db)
        with pytest.raises(UnsafeUrl, match="private"):
            chat("system", "user", [])


class TestRedirectsDisabled:
    def test_download_does_not_follow_redirects(self, monkeypatch):
        """A 302 to an internal address would bypass the URL check, since only the
        first URL is ours to validate."""
        seen: list[str] = []

        class RedirectTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                seen.append(str(request.url))
                if request.url.path.endswith("/images/generations"):
                    return httpx.Response(
                        200, json={"data": [{"url": "https://example.com/img.png"}]})
                if request.url.host == "example.com":
                    return httpx.Response(
                        302, headers={"location": "https://169.254.169.254/creds"})
                return httpx.Response(200, content=b"INTERNAL")

        transport = RedirectTransport()
        real_client = httpx.Client

        def client_factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", client_factory)
        provider = OpenAICompatProvider(OpenAICompatSettings(
            base_url="https://example.com/v1", api_key="sk-test-not-real",
            timeout_s=5, reference_mode="off",
        ))
        with pytest.raises(ProviderError) as excinfo:
            provider.generate(_request())
        # The redirect is reported as a failed download, never followed.
        assert excinfo.value.code == "download_failed"
        assert not any("169.254.169.254" in url for url in seen), seen
        assert json is not None  # module used for the JSON bodies above
