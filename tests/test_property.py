"""Property tests — invariants that must hold for any input.

Idempotency of canonicalize + dedup; Settings round-trip; ResilientExecutor
fresh-cache fast-path.
"""

from __future__ import annotations

import os

import pytest

from conftest import ws


# --------------------------------------------------------------------------- #
# Idempotency: canonicalize(canonicalize(u)) == canonicalize(u)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path?utm_source=x&id=42",
        "HTTPS://Example.COM/A/b/c?fbclid=abc",
        "http://rbi.org.in/notifications/2024/123.html",
        "https://reuters.com/article/us-fed-rates?id=xyz%20abc",
    ],
)
def test_canonicalize_idempotent(url):
    once = ws.canonicalize_url(url)
    twice = ws.canonicalize_url(once)
    assert once == twice, f"canonicalize not idempotent: {once} != {twice}"


def test_canonicalize_preserves_idempotency_after_stripping():
    """Stripping trackers should be stable across repeated calls."""
    raw = "https://example.com?a=1&utm_source=x&b=2&fbclid=y"
    c1 = ws.canonicalize_url(raw)
    c2 = ws.canonicalize_url(c1)
    c3 = ws.canonicalize_url(c2)
    assert c1 == c2 == c3


# --------------------------------------------------------------------------- #
# Dedup idempotency: dedup(dedup(results)) == dedup(results)
# --------------------------------------------------------------------------- #


def test_dedup_idempotent():
    """_dedup_by_content returns (kept, dropped) — verify applying it twice
    to the kept list yields the same result."""
    results = [
        {"title": "A", "body": "same body text", "href": "http://a"},
        {"title": "A2", "body": "same body text", "href": "http://b"},
        {"title": "B", "body": "different content", "href": "http://c"},
    ]
    once_kept, _ = ws._dedup_by_content(results, threshold=5)
    twice_kept, _ = ws._dedup_by_content(once_kept, threshold=5)
    assert [r["href"] for r in once_kept] == [r["href"] for r in twice_kept]


# --------------------------------------------------------------------------- #
# Settings — frozen + from_env round-trip
# --------------------------------------------------------------------------- #


def test_settings_is_frozen():
    s = ws.get_settings()
    with pytest.raises(Exception):
        s.call_budget = 999  # type: ignore


def test_settings_from_env_reads_region(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_REGION", "in-en")
    s = ws.Settings.from_env()
    assert s.search.region == "in-en"


def test_settings_from_env_defaults():
    # Ensure no env override → default values present
    for var in ("WEB_SEARCH_REGION", "WEB_SEARCH_MAX_RESULTS", "WEB_SEARCH_TIMEOUT"):
        os.environ.pop(var, None)
    s = ws.Settings.from_env()
    assert s.search.region == "us-en"
    assert s.search.max_results == 10
    assert s.search.timeout == 10


def test_settings_security_budgets_positive():
    s = ws.get_settings()
    assert s.security.max_response_bytes > 0
    assert s.security.max_crawl_bytes > s.security.max_response_bytes
    assert s.security.max_redirects >= 1


def test_default_constants_are_true_settings_aliases():
    """Invariant: every DEFAULT_* module constant equals the corresponding
    Settings.from_env() field. Proves they're true aliases (not independent
    reads) — no drift risk. If this fails, someone added a DEFAULT_* that
    bypasses Settings.from_env()."""
    s = ws.get_settings()
    # Map each DEFAULT_* name → (settings_path, expected_value). This IS the
    # contract: module constants must read from _settings, never from env directly.
    checks = {
        "DEFAULT_REGION": (s.search.region, ws.DEFAULT_REGION),
        "DEFAULT_SAFESEARCH": (s.search.safesearch, ws.DEFAULT_SAFESEARCH),
        "DEFAULT_MAX_RESULTS": (s.search.max_results, ws.DEFAULT_MAX_RESULTS),
        "DEFAULT_BACKEND": (s.search.backend, ws.DEFAULT_BACKEND),
        "DEFAULT_TIMEOUT": (s.search.timeout, ws.DEFAULT_TIMEOUT),
        "DEFAULT_PROXY": (s.search.proxy, ws.DEFAULT_PROXY),
        "DEFAULT_MAX_RETRIES": (s.search.max_retries, ws.DEFAULT_MAX_RETRIES),
        "DEFAULT_RATE_LIMIT": (s.rate.initial, ws.DEFAULT_RATE_LIMIT),
        "DEFAULT_RATE_CAP": (s.rate.cap, ws.DEFAULT_RATE_CAP),
        "DEFAULT_RATE_FLOOR": (s.rate.floor, ws.DEFAULT_RATE_FLOOR),
        "DEFAULT_CB_FAILURE_THRESHOLD": (
            s.circuit.failure_threshold,
            ws.DEFAULT_CB_FAILURE_THRESHOLD,
        ),
        "DEFAULT_CB_COOLDOWN": (s.circuit.cooldown, ws.DEFAULT_CB_COOLDOWN),
        "DEFAULT_CACHE_TTL": (s.cache.ttl, ws.DEFAULT_CACHE_TTL),
        "DEFAULT_NEG_CACHE_TTL": (s.cache.neg_ttl, ws.DEFAULT_NEG_CACHE_TTL),
        "DEFAULT_CACHE_ENABLED": (s.cache.enabled, ws.DEFAULT_CACHE_ENABLED),
        "DEFAULT_MAX_RESPONSE_BYTES": (
            s.security.max_response_bytes,
            ws.DEFAULT_MAX_RESPONSE_BYTES,
        ),
        "DEFAULT_MAX_CRAWL_BYTES": (
            s.security.max_crawl_bytes,
            ws.DEFAULT_MAX_CRAWL_BYTES,
        ),
        "DEFAULT_MAX_REDIRECTS": (s.security.max_redirects, ws.DEFAULT_MAX_REDIRECTS),
        "DEFAULT_AGENTIC_MAX_ITER": (s.agentic.max_iter, ws.DEFAULT_AGENTIC_MAX_ITER),
        "DEFAULT_CALL_BUDGET": (s.call_budget, ws.DEFAULT_CALL_BUDGET),
        "DEFAULT_CONCURRENCY": (s.concurrency, ws.DEFAULT_CONCURRENCY),
        "DEFAULT_LOG_LEVEL": (s.log_level, ws.DEFAULT_LOG_LEVEL),
        "OUTPUT_SCHEMA_VERSION": (s.output_schema_version, ws.OUTPUT_SCHEMA_VERSION),
    }
    failures = []
    for name, (expected, actual) in checks.items():
        if expected != actual:
            failures.append(f"{name}: settings={expected!r} != alias={actual!r}")
    assert not failures, "DEFAULT_* alias drift:\n  " + "\n  ".join(failures)


# --------------------------------------------------------------------------- #
# ResilientExecutor — fresh cache fast-path returns immediately
# --------------------------------------------------------------------------- #


def test_resilient_executor_fresh_cache_returns_cached(monkeypatch, tmp_path):
    """When cache has a fresh entry, operation is NEVER called.
    Enable the cache (disabled in conftest via WEB_SEARCH_NO_CACHE=1) by
    patching DEFAULT_CACHE_ENABLED directly — _settings is frozen at import.
    F-20: cache dir is redirected to tmp_path so the test never pollutes the
    user's real ~/.cache/web-search."""
    monkeypatch.setattr(ws, "DEFAULT_CACHE_ENABLED", True)
    monkeypatch.setattr(ws, "DEFAULT_CACHE_DIR", tmp_path)
    key = ws._cache_key("test_prop", {"q": "cached"})
    cached_payload = {
        "schema": ws.OUTPUT_SCHEMA_VERSION,
        "query": "cached",
        "category": "test_prop",
        "params": {"q": "cached"},
        "results": [{"title": "cached-result"}],
        "citations": [],
        "meta": {
            "cache_status": "fresh",
            "served_from": "cache",
            "attempts": [],
            "backends_tried": [],
            "rate_limited": False,
            "timed_out": False,
        },
    }
    ws._cache_write(key, cached_payload)

    executor = ws.ResilientExecutor(ws._get_metrics(), ws._single_flight)

    operation_called = []

    def operation(cached, status):
        operation_called.append(True)
        return {"results": "should-not-reach"}

    params = {"q": "cached"}
    result = executor.execute(
        cache_key=key,
        cache_ttl=60,
        neg_cache_ttl=30,
        category="test_prop",
        query="cached",
        params=params,
        operation=operation,
    )
    assert operation_called == []  # operation must NOT fire on fresh cache
    assert result["meta"]["cache_status"] == "fresh"
