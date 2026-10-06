"""ExtractCascade contract + failure-mode tests using FakeFetcher.

Exercises the layered fallback deterministically (no network): success on
first layer, fallback to second layer, bot-block detection, binary-content
detection, all-layers-exhausted, and policy-error propagation.
"""

from __future__ import annotations

import pytest

from conftest import FakeFetcher, FakeExtractor, FakeCookieStore, make_fetch_result, ws


@pytest.fixture
def cookie_store():
    return FakeCookieStore()


@pytest.fixture
def extractor():
    return FakeExtractor(output="# Title\n\nReal extracted content here.")


# --------------------------------------------------------------------------- #
# Success cases
# --------------------------------------------------------------------------- #


def test_cascade_success_first_fetcher(cookie_store, extractor):
    fetcher = FakeFetcher(
        results=[make_fetch_result(html="<html>OK</html>" * 100, source="fake")]
    )
    cascade = ws.ExtractCascade(
        fetchers=[fetcher], extractor=extractor, cookie_store=cookie_store
    )
    text = cascade.run("https://example.com/", "example.com")
    assert "Real extracted content" in text
    assert len(fetcher.calls) == 1


def test_cascade_fallback_to_second_fetcher(cookie_store, extractor):
    """First fetcher returns None (transport fail) → second succeeds."""
    f1 = FakeFetcher(name="f1", results=[None])
    f2 = FakeFetcher(
        name="f2",
        results=[make_fetch_result(html="<html>OK</html>" * 100, source="f2")],
    )
    cascade = ws.ExtractCascade(
        fetchers=[f1, f2], extractor=extractor, cookie_store=cookie_store
    )
    text = cascade.run("https://example.com/", "example.com")
    assert "Real extracted content" in text
    assert len(f1.calls) == 1
    assert len(f2.calls) == 1


def test_cascade_cookie_reuse_skips_ddgs(cookie_store, extractor):
    """When cached cookies exist, ddgs fetcher is skipped on the reuse pass."""
    cookie_store.save("example.com", "cf_clearance=abc")
    ddgs = FakeFetcher(name="ddgs", results=[make_fetch_result(source="ddgs")])
    curl = FakeFetcher(
        name="curl_cffi",
        results=[make_fetch_result(html="<html>OK</html>" * 100, source="curl_cffi")],
    )
    cascade = ws.ExtractCascade(
        fetchers=[ddgs, curl], extractor=extractor, cookie_store=cookie_store
    )
    cascade.run("https://example.com/", "example.com")
    # ddgs should NOT be called during cookie-reuse pass (curl_cffi handles it)
    assert len(ddgs.calls) == 0
    assert len(curl.calls) == 1


def test_cascade_saves_cookies_on_success(cookie_store, extractor):
    fetcher = FakeFetcher(results=[make_fetch_result(html="<html>OK</html>" * 100)])
    fetcher.name = "curl_cffi"
    cascade = ws.ExtractCascade(
        fetchers=[fetcher], extractor=extractor, cookie_store=cookie_store
    )
    cascade.run("https://example.com/", "example.com")
    # cookies were empty in make_fetch_result — nothing to save
    assert cookie_store.get("example.com") == ""


# --------------------------------------------------------------------------- #
# Bot-block + binary detection
# --------------------------------------------------------------------------- #


def test_cascade_detects_bot_block_and_continues(cookie_store, extractor):
    bot_html = (
        "<html><title>Just a moment...</title>Attention required! Cloudflare</html>"
    )
    f1 = FakeFetcher(
        name="blocked", results=[make_fetch_result(html=bot_html, source="f1")]
    )
    f2 = FakeFetcher(
        name="ok",
        results=[make_fetch_result(html="<html>OK</html>" * 100, source="f2")],
    )
    cascade = ws.ExtractCascade(
        fetchers=[f1, f2], extractor=extractor, cookie_store=cookie_store
    )
    text = cascade.run("https://example.com/", "example.com")
    assert "Real extracted content" in text
    assert len(f1.calls) == 1
    assert len(f2.calls) == 1


def test_cascade_raises_binary_on_pdf_content_type(cookie_store, extractor):
    fetcher = FakeFetcher(
        results=[make_fetch_result(content_type="application/pdf", source="fake")]
    )
    cascade = ws.ExtractCascade(
        fetchers=[fetcher], extractor=extractor, cookie_store=cookie_store
    )
    with pytest.raises(ws.BinaryDocument):
        cascade.run("https://example.com/doc.pdf", "example.com")


# --------------------------------------------------------------------------- #
# All-layers-exhausted
# --------------------------------------------------------------------------- #


def test_cascade_raises_fetchblocked_when_all_exhaust(cookie_store, extractor):
    fetchers = [
        FakeFetcher(name="f1", results=[None]),
        FakeFetcher(name="f2", results=[None]),
        FakeFetcher(name="f3", results=[None]),
    ]
    cascade = ws.ExtractCascade(
        fetchers=fetchers, extractor=extractor, cookie_store=cookie_store
    )
    with pytest.raises(ws.FetchBlocked):
        cascade.run("https://example.com/", "example.com")


# --------------------------------------------------------------------------- #
# Policy-error propagation (SSRF/Budget)
# --------------------------------------------------------------------------- #


def test_cascade_propagates_ssrf_error(cookie_store, extractor):
    fetcher = FakeFetcher(raise_exc=ws.SSRFBlocked("http://x/", "private IP"))
    cascade = ws.ExtractCascade(
        fetchers=[fetcher], extractor=extractor, cookie_store=cookie_store
    )
    with pytest.raises(ws.SSRFBlocked):
        cascade.run("https://example.com/", "example.com")


def test_cascade_propagates_budget_error(cookie_store, extractor):
    fetcher = FakeFetcher(
        raise_exc=ws.BudgetExceeded("MAX_RESPONSE_BYTES", 50_000_000, 99_999_999)
    )
    cascade = ws.ExtractCascade(
        fetchers=[fetcher], extractor=extractor, cookie_store=cookie_store
    )
    with pytest.raises(ws.BudgetExceeded):
        cascade.run("https://example.com/", "example.com")


# --------------------------------------------------------------------------- #
# Protocol contract
# --------------------------------------------------------------------------- #


def test_curl_cffi_fetcher_name():
    assert ws.CurlCffiFetcher().name == "curl_cffi"


def test_playwright_fetcher_name():
    assert ws.PlaywrightFetcher().name == "playwright"


def test_composite_extractor_name():
    assert ws.CompositeExtractor().name == "composite"


def test_fetch_result_is_frozen():
    fr = make_fetch_result()
    with pytest.raises(Exception):
        fr.html = "mutated"  # type: ignore


# --------------------------------------------------------------------------- #
# F-32 pin: malformed redirect (no Location) = bounded same-URL retry with
# freshly harvested cookies, not chain abandonment.
# --------------------------------------------------------------------------- #


class _FakeCffiResponse:
    def __init__(self, status_code, headers=None, text="", cookies=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.cookies = cookies or {}


def test_f32_redirect_without_location_retries_same_url_with_cookies(monkeypatch):
    import web_search as _ws_mod

    calls: list[tuple[str, str | None]] = []
    responses = [
        _FakeCffiResponse(302, headers={}, cookies={"cf_chl": "1"}),  # no Location
        _FakeCffiResponse(
            200,
            headers={"Content-Type": "text/html"},
            text="<html>" + "x" * 600 + "</html>",
        ),
    ]

    def fake_get(url, **kwargs):
        cookie_header = kwargs.get("headers", {}).get("Cookie")
        calls.append((url, cookie_header))
        return responses[min(len(calls) - 1, len(responses) - 1)]

    class _StubResolver:
        def resolve_and_validate(self, url):
            pass

        def validate_redirect_target(self, url):
            pass

    monkeypatch.setattr(_ws_mod, "get_safe_url_resolver", lambda: _StubResolver())
    monkeypatch.setattr("curl_cffi.requests.get", fake_get)

    html, _, content_type = ws._fetch_curl_cffi("https://example.com/a")

    assert html is not None and "xxxx" in html
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == "https://example.com/a"
    assert "cf_chl=1" in (calls[1][1] or "")
    assert content_type.startswith("text/html")
