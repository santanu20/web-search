from __future__ import annotations

import time

import pytest

from conftest import FakeCookieStore, FakeExtractor, make_fetch_result, ws


def test_extract_cascade_rejects_oversized_html_before_parser(monkeypatch):
    monkeypatch.setattr(ws, "DEFAULT_MAX_HTML_BYTES", 8)
    extractor = FakeExtractor()
    cascade = ws.ExtractCascade(
        fetchers=[],
        extractor=extractor,
        cookie_store=FakeCookieStore(),
    )
    result = make_fetch_result(
        html="<html>too much</html>",
        content_type="text/html",
        source="curl_cffi",
    )
    with pytest.raises(ws.BudgetExceeded) as exc:
        cascade._extract_or_passthrough(result, "https://example.test/")
    assert exc.value.budget == "MAX_HTML_BYTES"
    assert extractor.calls == []


def test_extracted_text_budgets_check_chars_and_utf8_bytes(monkeypatch):
    monkeypatch.setattr(ws, "DEFAULT_MAX_DOCUMENT_CHARS", 3)
    monkeypatch.setattr(ws, "DEFAULT_MAX_MARKDOWN_BYTES", 100)
    with pytest.raises(ws.BudgetExceeded) as char_exc:
        ws._enforce_extracted_text_budgets("four", "https://example.test/")
    assert char_exc.value.budget == "MAX_DOCUMENT_CHARS"

    monkeypatch.setattr(ws, "DEFAULT_MAX_DOCUMENT_CHARS", 100)
    monkeypatch.setattr(ws, "DEFAULT_MAX_MARKDOWN_BYTES", 4)
    with pytest.raises(ws.BudgetExceeded) as byte_exc:
        ws._enforce_extracted_text_budgets("😀a", "https://example.test/")
    assert byte_exc.value.budget == "MAX_MARKDOWN_BYTES"
    assert byte_exc.value.actual == 5


def test_oversized_cached_document_is_evicted_and_rejected(monkeypatch):
    deleted = []
    monkeypatch.setattr(ws, "DEFAULT_CACHE_ENABLED", True)
    monkeypatch.setattr(ws, "DEFAULT_MAX_DOCUMENT_CHARS", 3)
    monkeypatch.setattr(ws, "DEFAULT_MAX_MARKDOWN_BYTES", 100)
    monkeypatch.setattr(
        ws,
        "get_safe_url_resolver",
        lambda: type("Resolver", (), {"resolve_and_validate": lambda self, url: None})(),
    )
    monkeypatch.setattr(
        ws, "_cache_read", lambda *args, **kwargs: ({"content": "oversized"}, "fresh")
    )
    monkeypatch.setattr(ws, "_cache_delete", lambda key: deleted.append(key))
    with pytest.raises(ws.BudgetExceeded, match="MAX_DOCUMENT_CHARS"):
        ws._extract_with_fallback(None, "https://example.test/")
    assert len(deleted) == 1


def test_fresh_document_is_checked_before_cache_write(monkeypatch):
    writes = []
    monkeypatch.setattr(ws, "DEFAULT_CACHE_ENABLED", True)
    monkeypatch.setattr(ws, "DEFAULT_MAX_DOCUMENT_CHARS", 3)
    monkeypatch.setattr(ws, "DEFAULT_MAX_MARKDOWN_BYTES", 100)
    monkeypatch.setattr(
        ws,
        "get_safe_url_resolver",
        lambda: type("Resolver", (), {"resolve_and_validate": lambda self, url: None})(),
    )
    monkeypatch.setattr(ws, "_cache_read", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(ws, "_extract_with_fallback_uncached", lambda *a, **k: "oversized")
    monkeypatch.setattr(ws, "_cache_write", lambda *args, **kwargs: writes.append(args))
    with pytest.raises(ws.BudgetExceeded, match="MAX_DOCUMENT_CHARS"):
        ws._extract_with_fallback(None, "https://example.test/")
    assert writes == []


def test_link_parser_stops_at_unique_link_budget():
    markdown = "\n".join(
        f"[article {i}](https://example.test/article-{i})" for i in range(100)
    )
    links = ws._extract_links_from_markdown(
        markdown,
        "example.test",
        "https://example.test/",
        max_links=5,
    )
    assert len(links) == 5
    assert links[-1][0].endswith("/article-4")


def test_link_parser_zero_budget_does_not_scan_or_return_links():
    markdown = "[article](https://example.test/article)"
    assert ws._extract_links_from_markdown(
        markdown, "example.test", "https://example.test/", max_links=0
    ) == []


def test_utf8_crawl_truncation_never_splits_multibyte_characters():
    clipped, truncated = ws._truncate_utf8_to_bytes("abc😀z", 4)
    assert clipped == "abc"
    assert truncated is True
    assert len(clipped.encode("utf-8")) <= 4

    unchanged, truncated = ws._truncate_utf8_to_bytes("abc", 3)
    assert unchanged == "abc"
    assert truncated is False


def test_crawl_final_page_is_clipped_to_remaining_byte_budget(monkeypatch):
    class Resolver:
        def resolve_and_validate(self, url):
            return None

    class Limiter:
        def acquire(self, timeout=None):
            return True

    class Metrics:
        def incr(self, name):
            return None

        def snapshot(self):
            return {}

    monkeypatch.setattr(ws, "get_safe_url_resolver", lambda: Resolver())
    monkeypatch.setattr(ws, "_fetch_robots_txt", lambda origin: None)
    monkeypatch.setattr(ws, "_fetch_sitemap_locs", lambda *a, **k: [])
    monkeypatch.setattr(ws, "_get_rate_limiter", lambda: Limiter())
    monkeypatch.setattr(ws, "_get_metrics", lambda: Metrics())
    monkeypatch.setattr(ws, "_extract_with_fallback", lambda *a, **k: "abc😀")
    monkeypatch.setattr(ws, "_extract_links_from_markdown", lambda *a, **k: [])
    monkeypatch.setattr(ws, "_human_delay", lambda: None)
    monkeypatch.setattr(ws, "DEFAULT_MAX_CRAWL_BYTES", 4)
    monkeypatch.setattr(ws, "DEFAULT_MAX_LINKS_PER_PAGE", 5)
    monkeypatch.setattr(ws, "DEFAULT_MAX_TOTAL_LINKS", 10)

    result = ws._crawl_site(
        None,
        "https://example.test/",
        max_pages=3,
        max_chars=0,
        call_deadline=time.monotonic() + 60,
        proxy_for_log="<none>",
    )
    assert result["total_content_chars"] == 3
    assert result["pages"][0]["content"] == "abc"
    assert result["pages"][0]["truncated"] == "crawl_byte_budget"
    assert len(result["pages"][0]["content"].encode("utf-8")) <= 4


@pytest.mark.parametrize("configured,expected", [(0, 1), (-10, 1), (3, 3), (100, 16)])
def test_settings_bound_concurrent_workers(monkeypatch, configured, expected):
    monkeypatch.setenv("WEB_SEARCH_CONCURRENCY", str(configured))
    assert ws.Settings.from_env().concurrency == expected
