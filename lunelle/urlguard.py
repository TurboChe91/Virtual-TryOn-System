"""Outbound URL validation: HTTPS enforcement and SSRF blocking.

Profiles are operator-editable at runtime, and two endpoints then make
server-side requests to whatever they contain — `/api/profiles/{id}/test`, which
returns the first 200 bytes of the response body, and the LLM chat channel. That
combination (attacker-chosen URL + response echoed back) is a read-capable SSRF,
so a URL is checked before either is allowed to use it.

What is blocked: non-HTTPS schemes, embedded credentials, and hostnames that
resolve to loopback / private / link-local / reserved space — including cloud
metadata endpoints such as 169.254.169.254, which is the payload that turns SSRF
into credential theft.

Escape hatch: LUNELLE_ALLOW_PRIVATE_API_HOSTS=1 permits private ranges for
operators self-hosting a model on their LAN. It is refused in production.

Known limitation, stated rather than papered over: validation resolves DNS at
check time, so a name that resolves to a public IP now and a private one at
request time (DNS rebinding) is not fully prevented. Closing that needs
connection-time pinning inside the HTTP client; the checks here are re-run
immediately before each request to keep the window small.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("https",)


class UnsafeUrl(ValueError):
    """A URL is not allowed for outbound server-side requests."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Reason this address is off-limits, or None if it is routable public space."""
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        # Covers 169.254.0.0/16 — AWS/GCP/Azure instance metadata.
        return "link-local address (cloud metadata range)"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    # IPv4-mapped/compatible IPv6 (e.g. ::ffff:127.0.0.1) would otherwise slip
    # past the checks above, which only see the IPv6 form.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        inner = _is_blocked_ip(mapped)
        return f"IPv4-mapped {inner}" if inner else None
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None:
        inner = _is_blocked_ip(sixtofour)
        return f"6to4-embedded {inner}" if inner else None
    return None


def resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address] | None:
    """Every address a hostname resolves to, or None if it does not resolve.

    Not resolving is deliberately NOT treated as unsafe. Two reasons: an
    unresolvable host cannot be connected to, so it is not an SSRF vector; and
    turning a DNS failure into a validation error would misclassify a transient
    outage as a permanent configuration error — the provider layer already maps
    DNS failures to a retryable `dns` error, which is the correct behaviour.
    Liveness is the HTTP client's business; this module only refuses addresses
    that are internal.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None
    out = []
    for info in infos:
        address = info[4][0]
        if not isinstance(address, str):  # AF_INET6 sockaddr is a 4-tuple
            continue
        try:
            # Strip any IPv6 zone index (fe80::1%en0).
            out.append(ipaddress.ip_address(address.split("%")[0]))
        except ValueError:  # pragma: no cover - defensive
            continue
    return out or None


def validate_outbound_url(
    url: str,
    *,
    allow_private: bool = False,
    require_https: bool = True,
) -> str:
    """Return the normalized URL, or raise UnsafeUrl.

    ALL resolved addresses must be acceptable: a name with one public and one
    private address is rejected, since which one the client connects to is not
    ours to choose.
    """
    raw = (url or "").strip()
    if not raw:
        raise UnsafeUrl("URL is empty")
    parts = urlsplit(raw)
    if not parts.scheme:
        raise UnsafeUrl("URL must include a scheme, e.g. https://api.example.com/v1")
    if require_https and parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrl(
            f"scheme {parts.scheme!r} is not allowed; use https:// "
            "(credentials would otherwise travel in clear text)"
        )
    if parts.username or parts.password:
        # Credentials in a URL leak into logs, error strings, and Referer headers.
        raise UnsafeUrl(
            "URL must not embed credentials; put the key in the api_key field instead"
        )
    host = parts.hostname
    if not host:
        raise UnsafeUrl("URL has no host")

    if not allow_private:
        addresses = resolve_host(host)
        if addresses is None:
            # Cannot verify, cannot connect either. Let the HTTP client report a
            # DNS error (retryable) instead of failing validation permanently.
            logger.info("outbound host %r does not resolve; deferring to request time", host)
            return raw.rstrip("/")
        for address in addresses:
            reason = _is_blocked_ip(address)
            if reason is not None:
                raise UnsafeUrl(
                    f"host {host!r} resolves to {address} ({reason}); refusing to let "
                    "the server request internal addresses. Set "
                    "LUNELLE_ALLOW_PRIVATE_API_HOSTS=1 only if this is a deliberate "
                    "self-hosted endpoint on a trusted network."
                )
    return raw.rstrip("/")


def assert_safe_request_url(
    url: str, *, allow_private: bool = False, require_https: bool = True
) -> None:
    """Re-check immediately before an outbound request (rebinding window)."""
    validate_outbound_url(url, allow_private=allow_private, require_https=require_https)


#: Process-wide setting for code too deep to thread config through (the provider
#: adapters). Set once at startup from Config; defaults to the safe value, so a
#: caller that forgets to configure it gets blocking, not bypassing.
_allow_private_hosts = False


def configure_allow_private_hosts(allow: bool) -> None:
    """Set the process-wide private-host policy (from Config at startup)."""
    global _allow_private_hosts  # noqa: PLW0603 - deliberate process-wide policy
    _allow_private_hosts = bool(allow)
    if allow:
        logger.warning(
            "private/loopback API hosts are ALLOWED for outbound requests "
            "(LUNELLE_ALLOW_PRIVATE_API_HOSTS=1); refused in production"
        )


def allow_private_hosts() -> bool:
    return _allow_private_hosts


def guard_request_url(url: str, *, require_https: bool = True) -> str:
    """Validate any URL about to be requested, using the process-wide policy.

    This is the choke point every outbound HTTP call goes through, so a new
    request path cannot silently skip the check: the URL a provider hands back is
    just as untrusted as one an operator typed.
    """
    return validate_outbound_url(
        url, allow_private=_allow_private_hosts, require_https=require_https
    )
