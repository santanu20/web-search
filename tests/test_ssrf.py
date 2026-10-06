"""P0 SSRF / egress security — policy enforcement tests.

Validates the SafeURLResolver blocks every forbidden target and allows every
legitimate one. These are the security-critical tests: a regression here is a
security hole. Property: private IP space + metadata endpoints NEVER reachable
via any fetch path.
"""

from __future__ import annotations

import pytest

from conftest import ws


@pytest.fixture
def resolver():
    return ws.SafeURLResolver(dns_ttl=0)  # zero TTL so DNS cache doesn't mask changes


# --------------------------------------------------------------------------- #
# MUST BLOCK — private / loopback / link-local / metadata
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url,label",
    [
        ("http://127.0.0.1/", "loopback-v4"),
        ("http://127.255.255.255/", "loopback-v4-high"),
        ("http://10.0.0.1/", "private-10"),
        ("http://10.255.255.255/", "private-10-high"),
        ("http://192.168.1.1/", "private-192168"),
        ("http://192.168.0.0/", "private-192168-low"),
        ("http://172.16.0.1/", "private-17216"),
        ("http://172.31.255.255/", "private-17216-high"),
        ("http://169.254.169.254/latest/meta-data/", "aws-metadata"),
        ("http://169.254.170.2/", "link-local-ecs"),
        ("http://100.100.100.200/", "alibaba-metadata"),
        ("http://0.0.0.0/", "this-network"),
        ("http://[::1]/", "ipv6-loopback"),
        ("http://[fc00::1]/", "ipv6-ula"),
        ("http://[fe80::1]/", "ipv6-linklocal"),
        ("http://192.0.2.1/", "test-net-1-reserved"),
    ],
)
def test_blocks_forbidden_ip(resolver, url, label):
    with pytest.raises(ws.SSRFBlocked):
        resolver.resolve_and_validate(url)


def test_blocks_metadata_hostname_gcp(resolver):
    with pytest.raises(ws.SSRFBlocked):
        resolver.resolve_and_validate("http://metadata.google.internal/")


def test_blocks_metadata_hostname_azure(resolver):
    with pytest.raises(ws.SSRFBlocked):
        resolver.resolve_and_validate("http://metadata.azure.com/")


def test_blocks_bad_scheme_ftp(resolver):
    with pytest.raises(ws.InvalidURL):
        resolver.resolve_and_validate("ftp://example.com/")


def test_blocks_bad_scheme_file(resolver):
    with pytest.raises(ws.InvalidURL):
        resolver.resolve_and_validate("file:///etc/passwd")


def test_blocks_no_hostname(resolver):
    with pytest.raises(ws.InvalidURL):
        resolver.resolve_and_validate("http:///")


# --------------------------------------------------------------------------- #
# MUST ALLOW — public IPs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://8.8.8.8/",
        "https://1.1.1.1/",
        "http://104.16.123.96/",
    ],
)
def test_allows_public_ip(resolver, url):
    target = resolver.resolve_and_validate(url)
    assert target.scheme in ("http", "https")
    assert target.host
    assert len(target.resolved_ips) > 0


def test_allows_public_dns(resolver):
    target = resolver.resolve_and_validate("https://www.google.com/")
    assert target.host == "www.google.com"
    assert len(target.resolved_ips) > 0


# --------------------------------------------------------------------------- #
# Trusted hosts — allowlist works, metadata STILL blocked
# --------------------------------------------------------------------------- #


def test_trusted_hosts_allows_localhost():
    resolver = ws.SafeURLResolver(trusted_hosts={"localhost"})
    target = resolver.resolve_and_validate("http://localhost:4479/")
    assert target.is_trusted is True


def test_metadata_still_blocked_when_trusted():
    """Metadata endpoints must NEVER be allowlisted, even if listed in trusted_hosts."""
    resolver = ws.SafeURLResolver(trusted_hosts={"169.254.169.254"})
    with pytest.raises(ws.SSRFBlocked):
        resolver.resolve_and_validate("http://169.254.169.254/")


# --------------------------------------------------------------------------- #
# Budget exception structure
# --------------------------------------------------------------------------- #


def test_budget_exceeded_carries_limit_and_actual():
    ex = ws.BudgetExceeded("MAX_RESPONSE_BYTES", 50_000_000, 99_999_999, "http://x/")
    assert ex.budget == "MAX_RESPONSE_BYTES"
    assert ex.limit == 50_000_000
    assert ex.actual == 99_999_999
    assert "99" in str(ex)


def test_ssrf_blocked_carries_resolved_ips():
    ex = ws.SSRFBlocked(
        "http://169.254.169.254/", "cloud-metadata", ["169.254.169.254"]
    )
    assert ex.url == "http://169.254.169.254/"
    assert ex.reason == "cloud-metadata"
    assert "169.254.169.254" in ex.resolved_ips


def test_search_error_hierarchy():
    """All custom errors inherit SearchError (vendor-agnostic base)."""
    for exc_class in [
        ws.BackendUnavailable,
        ws.RateLimited,
        ws.FetchTimeout,
        ws.FetchBlocked,
        ws.InvalidURL,
        ws.SSRFBlocked,
        ws.BudgetExceeded,
        ws.BinaryDocument,
        ws.ExtractionFailed,
    ]:
        assert issubclass(exc_class, ws.SearchError)
