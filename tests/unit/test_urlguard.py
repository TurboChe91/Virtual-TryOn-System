"""Outbound URL validation: HTTPS enforcement and SSRF blocking.

Uses IP literals wherever possible so the tests do not depend on DNS.
"""

from __future__ import annotations

import pytest

from lunelle.urlguard import UnsafeUrl, resolve_host, validate_outbound_url


class TestSchemeAndShape:
    def test_https_public_host_is_allowed(self):
        assert validate_outbound_url("https://93.184.216.34/v1") == "https://93.184.216.34/v1"

    def test_trailing_slash_is_stripped(self):
        assert validate_outbound_url("https://93.184.216.34/v1/") == "https://93.184.216.34/v1"

    @pytest.mark.parametrize("url", [
        "http://93.184.216.34/v1",
        "file:///etc/passwd",
        "gopher://93.184.216.34/1",
        "ftp://93.184.216.34/x",
    ])
    def test_non_https_schemes_are_refused(self, url):
        with pytest.raises(UnsafeUrl, match="not allowed"):
            validate_outbound_url(url)

    def test_missing_scheme_is_refused(self):
        with pytest.raises(UnsafeUrl, match="must include a scheme"):
            validate_outbound_url("93.184.216.34/v1")

    def test_empty_is_refused(self):
        with pytest.raises(UnsafeUrl, match="empty"):
            validate_outbound_url("")

    def test_embedded_credentials_are_refused(self):
        """Credentials in a URL leak into logs and error strings."""
        with pytest.raises(UnsafeUrl, match="must not embed credentials"):
            validate_outbound_url("https://user:secret@93.184.216.34/v1")

    def test_http_refused_even_when_private_is_allowed(self):
        """The escape hatch relaxes the address check, never the scheme."""
        with pytest.raises(UnsafeUrl, match="not allowed"):
            validate_outbound_url("http://192.168.1.10/v1", allow_private=True)


class TestSsrfRanges:
    @pytest.mark.parametrize("url,expected", [
        ("https://127.0.0.1/v1", "loopback"),
        ("https://127.1.2.3/v1", "loopback"),
        ("https://[::1]/v1", "loopback"),
        ("https://169.254.169.254/latest/meta-data", "link-local"),
        ("https://[fe80::1]/v1", "link-local"),
        ("https://10.0.0.5/v1", "private"),
        ("https://192.168.1.1/v1", "private"),
        ("https://172.16.0.1/v1", "private"),
        ("https://0.0.0.0/v1", "private"),
        ("https://[fc00::1]/v1", "private"),
    ])
    def test_internal_addresses_are_refused(self, url, expected):
        with pytest.raises(UnsafeUrl, match=expected):
            validate_outbound_url(url)

    def test_cloud_metadata_is_called_out_by_name(self):
        """The message should tell an operator why this one matters."""
        with pytest.raises(UnsafeUrl, match="metadata"):
            validate_outbound_url("https://169.254.169.254/latest/meta-data")

    def test_ipv4_mapped_ipv6_cannot_smuggle_loopback(self):
        with pytest.raises(UnsafeUrl, match="loopback"):
            validate_outbound_url("https://[::ffff:127.0.0.1]/v1")

    @pytest.mark.parametrize("url", [
        "https://192.168.1.50:8000/v1",
        "https://127.0.0.1:9000/v1",
        "https://10.1.2.3/v1",
    ])
    def test_escape_hatch_permits_private_hosts(self, url):
        assert validate_outbound_url(url, allow_private=True) == url

    def test_public_address_passes(self):
        assert validate_outbound_url("https://8.8.8.8/v1")


class TestDnsBehaviour:
    def test_unresolvable_host_is_allowed_not_rejected(self):
        """An unresolvable host cannot be connected to, so it is not an SSRF
        vector; rejecting it would also misreport a DNS outage as a config error.
        The provider layer maps DNS failures to a retryable error instead."""
        url = "https://definitely-not-a-real-host-xyz123.invalid/v1"
        assert validate_outbound_url(url) == url

    def test_resolve_host_returns_none_for_unresolvable(self):
        assert resolve_host("definitely-not-a-real-host-xyz123.invalid") is None

    def test_resolve_host_passes_through_literals(self):
        assert [str(a) for a in resolve_host("10.0.0.1")] == ["10.0.0.1"]

    def test_localhost_name_resolves_to_blocked_space(self):
        """Blocking must work on names, not just literals."""
        with pytest.raises(UnsafeUrl, match="loopback"):
            validate_outbound_url("https://localhost/v1")
