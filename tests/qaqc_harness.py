"""
Comprehensive QA/QC harness for web_search.py — adversarial security,
property invariants, budget enforcement, and edge-case coverage.

Run: python tests/qaqc_harness.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
os.environ.setdefault("WEB_SEARCH_NO_SKILL_VENV", "1")
os.environ.setdefault("WEB_SEARCH_NO_CACHE", "1")

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("web_search", SKILL_DIR / "web_search.py")
ws = importlib.util.module_from_spec(spec)
sys.modules["web_search"] = ws
spec.loader.exec_module(ws)

PASS = 0
FAIL = 0
RESULTS: list[tuple[str, str, str]] = []  # (category, name, status)


def check(category, name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        RESULTS.append((category, name, "PASS"))
    else:
        FAIL += 1
        RESULTS.append((category, name, f"FAIL: {detail}"))


def expect_raises(category, name, exc_type, fn):
    global PASS, FAIL
    try:
        fn()
        FAIL += 1
        RESULTS.append(
            (category, name, f"FAIL: no exception (expected {exc_type.__name__})")
        )
    except exc_type:
        PASS += 1
        RESULTS.append((category, name, "PASS"))
    except Exception as ex:
        FAIL += 1
        RESULTS.append(
            (
                category,
                name,
                f"FAIL: wrong exc {type(ex).__name__} (expected {exc_type.__name__})",
            )
        )


def expect_no_raise(category, name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        RESULTS.append((category, name, "PASS"))
    except Exception as ex:
        FAIL += 1
        RESULTS.append((category, name, f"FAIL: {type(ex).__name__}: {ex}"))


# =========================================================================== #
# PHASE A: SSRF ADVERSARIAL — bypass attempts
# =========================================================================== #

print("\n{'='*60}")
print("PHASE A: SSRF ADVERSARIAL BYPASS ATTEMPTS")
print("=" * 60)

r = ws.SafeURLResolver(dns_ttl=0)

# Decimal IP notation (127.0.0.1 = 2130706433)
expect_raises(
    "SSRF",
    "decimal-IP-127",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://2130706433/"),
)
# Octal notation
expect_raises(
    "SSRF",
    "octal-IP-0177",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://0177.0.0.1/"),
)
# Hex notation
expect_raises(
    "SSRF",
    "hex-IP-0x7f",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://0x7f000001/"),
)
# IPv6-mapped IPv4
expect_raises(
    "SSRF",
    "ipv6-mapped-v4-127",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://[::ffff:127.0.0.1]/"),
)
expect_raises(
    "SSRF",
    "ipv6-mapped-v4-10",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://[::ffff:10.0.0.1]/"),
)
# URL-encoded (%31%32%37 = "127") — urlparse doesn't decode hostnames, so DNS
# fails on literal "%31%32%37.0.0.1" → SearchError. Any block is valid.
expect_raises(
    "SSRF",
    "url-encoded-127",
    ws.SearchError,
    lambda: r.resolve_and_validate("http://%31%32%37.0.0.1/"),
)
# Username trick (127.0.0.1@evil.com — the host is evil.com, not 127... but test it)
# Actually urlparse hostname extraction: http://user@host/ → host is "host"
# http://127.0.0.1@evil.com/ → host is evil.com (correct), but test it doesn't bypass
expect_no_raise(
    "SSRF",
    "user-at-host-extracts-real-host",
    lambda: r.resolve_and_validate("http://127.0.0.1@evil.com/"),
)
# Double scheme — hostname "https" resolves → DNS fail → SearchError.
expect_raises(
    "SSRF",
    "double-scheme-https-https",
    ws.SearchError,
    lambda: r.resolve_and_validate("https://https://evil.com/"),
)
# All cloud metadata endpoints
for meta in ["169.254.169.254", "100.100.100.200"]:
    expect_raises(
        "SSRF",
        f"metadata-{meta}",
        ws.SSRFBlocked,
        lambda m=meta: r.resolve_and_validate(f"http://{m}/"),
    )
# Metadata DNS names
expect_raises(
    "SSRF",
    "metadata-gcp-dns",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://metadata.google.internal/"),
)
expect_raises(
    "SSRF",
    "metadata-azure-dns",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://metadata.azure.com/"),
)
# Link-local range edges
expect_raises(
    "SSRF",
    "linklocal-169-254-0-0",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://169.254.0.0/"),
)
expect_raises(
    "SSRF",
    "linklocal-169-254-255-255",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://169.254.255.255/"),
)
# CGNAT range
expect_raises(
    "SSRF",
    "cgnat-100-64-0-1",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://100.64.0.1/"),
)
# 0.0.0.0 (binds all interfaces)
expect_raises(
    "SSRF",
    "zero-0.0.0.0",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://0.0.0.0/"),
)
# Port-based tricks (high port on private IP)
expect_raises(
    "SSRF",
    "private-with-port-8080",
    ws.SSRFBlocked,
    lambda: r.resolve_and_validate("http://192.168.1.1:8080/"),
)
# Trusted hosts can't allowlist metadata
r_meta_trust = ws.SafeURLResolver(
    trusted_hosts={"169.254.169.254", "metadata.google.internal"}
)
expect_raises(
    "SSRF",
    "trusted-meta-still-blocked-1",
    ws.SSRFBlocked,
    lambda: r_meta_trust.resolve_and_validate("http://169.254.169.254/"),
)
expect_raises(
    "SSRF",
    "trusted-meta-still-blocked-2",
    ws.SSRFBlocked,
    lambda: r_meta_trust.resolve_and_validate("http://metadata.google.internal/"),
)
# Valid public IPs must PASS
expect_no_raise(
    "SSRF", "public-8.8.8.8-passes", lambda: r.resolve_and_validate("https://8.8.8.8/")
)
expect_no_raise(
    "SSRF", "public-1.1.1.1-passes", lambda: r.resolve_and_validate("https://1.1.1.1/")
)
# Trusted localhost works
r_localhost = ws.SafeURLResolver(trusted_hosts={"localhost"})
check(
    "SSRF",
    "trusted-localhost-is-trusted",
    r_localhost.resolve_and_validate("http://localhost:8080/").is_trusted,
)

# =========================================================================== #
# PHASE B: URL CANONICALIZATION PROPERTY TESTS
# =========================================================================== #

print("\nPHASE B: URL CANONICALIZATION PROPERTY TESTS")

import random  # noqa: E402

random.seed(42)

# Idempotency: canonicalize(canonicalize(u)) == canonicalize(u)
test_urls = [
    "https://example.com/path?utm_source=x&id=42",
    "HTTP://Example.COM/A/b?fbclid=abc&keep=1",
    "http://rbi.org.in/notifications/2024/123.html?gclid=xyz",
    "https://reuters.com/article/us-fed-rates?id=abc%20def&p=1",
    "https://www.quantifiedstrategies.com/trading-strategies?mc_eid=1&n=2",
    "http://sub.domain.co.uk/deep/path/a/b/c/d/e/f?x=1&y=2&utm_medium=email",
    "https://example.com/#fragment",
    "https://example.com/path/",
    "https://example.com/path/no-slash",
    "http://example.com:80/path",
    "https://example.com:443/path",
]
for u in test_urls:
    once = ws.canonicalize_url(u)
    twice = ws.canonicalize_url(once)
    check("CANON", f"idempotent-{u[:30]}", once == twice, f"{once} != {twice}")

# Tracker stripping
trackers_stripped = [
    ("utm_source", "x"),
    ("utm_medium", "x"),
    ("utm_campaign", "x"),
    ("fbclid", "x"),
    ("gclid", "x"),
    ("mc_eid", "x"),
    ("ocid", "x"),
]
for tracker, val in trackers_stripped:
    raw = f"https://example.com/article?{tracker}={val}&keep=1"
    got = ws.canonicalize_url(raw)
    check("CANON", f"strips-{tracker}", tracker not in got, f"still present: {got}")

# Keeps legitimate params
legit = "https://example.com/article?id=42&page=3&sort=date"
check(
    "CANON",
    "keeps-legit-params",
    "id=42" in ws.canonicalize_url(legit) and "page=3" in ws.canonicalize_url(legit),
)

# =========================================================================== #
# PHASE C: BUDGET / RESOURCE ENFORCEMENT (offline)
# =========================================================================== #

print("\nPHASE C: BUDGET / RESOURCE ENFORCEMENT")

# BudgetExceeded exception structure
bx = ws.BudgetExceeded("MAX_RESPONSE_BYTES", 50_000_000, 99_999_999, "http://x/")
check("BUDGET", "carries-budget-name", bx.budget == "MAX_RESPONSE_BYTES")
check("BUDGET", "carries-limit", bx.limit == 50_000_000)
check("BUDGET", "carries-actual", bx.actual == 99_999_999)
check("BUDGET", "carries-url", bx.url == "http://x/")

# _is_crawl_skip filter coverage
skip_urls = [
    ("https://example.com/style.css", True),
    ("https://example.com/app.js", True),
    ("https://example.com/font.woff2", True),
    ("https://example.com/image.png", True),
    ("https://example.com/icon.svg", True),
    ("https://example.com/wp-admin/admin.php", True),
    ("https://example.com/wp-content/uploads/file.jpg", True),
    ("https://example.com/wp-json/wp/v2/posts", True),
    ("https://example.com/feed", True),
    ("https://example.com/comments/feed", True),
    ("https://example.com/?p=123", True),
    ("https://example.com/?page_id=456", True),
    ("https://example.com/xmlrpc.php", True),
    ("https://example.com/robots.txt", True),
    ("https://example.com/article-title", False),
    ("https://example.com/blog/2024/post", False),
    ("https://example.com/category/news", False),
    ("https://example.com/strategy/rsi-reversal", False),
    ("https://example.com/trading-strategies", False),
]
for url, should_skip in skip_urls:
    actual = ws._is_crawl_skip(url)
    check(
        "BUDGET",
        f"crawl-skip-{url.split('/')[-1][:20]}({'skip' if should_skip else 'keep'})",
        actual == should_skip,
        f"got {actual}",
    )

# =========================================================================== #
# PHASE D: EXTRACT CASCADE (with fakes — failure injection)
# =========================================================================== #

print("\nPHASE D: EXTRACT CASCADE — FAILURE INJECTION")

from conftest import (  # noqa: E402
    FakeFetcher,
    FakeExtractor,
    FakeCookieStore,
    make_fetch_result,
)

# Success on first fetcher
cs = FakeCookieStore()
ex = FakeExtractor()
f1 = FakeFetcher(name="ok", results=[make_fetch_result(html="<html>" + "x" * 600)])
cascade = ws.ExtractCascade(fetchers=[f1], extractor=ex, cookie_store=cs)
text = cascade.run("https://example.com/", "example.com")
check(
    "CASCADE",
    "success-first-fetcher",
    isinstance(text, str) and len(text) > 0,
    f"text={text!r}",
)

# Fallback: f1 fails (None), f2 succeeds
cs2 = FakeCookieStore()
f1f = FakeFetcher(name="dead", results=[None])
f2f = FakeFetcher(name="ok2", results=[make_fetch_result(html="<html>" + "y" * 600)])
cascade2 = ws.ExtractCascade(fetchers=[f1f, f2f], extractor=ex, cookie_store=cs2)
text2 = cascade2.run("https://example.com/", "example.com")
check(
    "CASCADE",
    "fallback-to-second",
    isinstance(text2, str) and len(text2) > 0,
    f"text={text2!r}",
)

# All fail → FetchBlocked
cs3 = FakeCookieStore()
f_all_dead = [FakeFetcher(name=f"f{i}", results=[None]) for i in range(3)]
cascade3 = ws.ExtractCascade(fetchers=f_all_dead, extractor=ex, cookie_store=cs3)
expect_raises(
    "CASCADE",
    "all-dead-fetchblocked",
    ws.FetchBlocked,
    lambda: cascade3.run("https://example.com/", "example.com"),
)

# Binary content (PDF) detected → BinaryDocument
cs4 = FakeCookieStore()
f_pdf = FakeFetcher(
    name="pdf", results=[make_fetch_result(content_type="application/pdf")]
)
cascade4 = ws.ExtractCascade(fetchers=[f_pdf], extractor=ex, cookie_store=cs4)
expect_raises(
    "CASCADE",
    "pdf-binary-detected",
    ws.BinaryDocument,
    lambda: cascade4.run("https://example.com/doc.pdf", "example.com"),
)

# Bot-block detected → skipped to next fetcher
cs5 = FakeCookieStore()
bot_html = "<html><title>Just a moment...</title>Attention required! Cloudflare</html>"
f_blocked = FakeFetcher(name="blocked", results=[make_fetch_result(html=bot_html)])
f_ok = FakeFetcher(name="ok3", results=[make_fetch_result(html="<html>" + "z" * 600)])
cascade5 = ws.ExtractCascade(fetchers=[f_blocked, f_ok], extractor=ex, cookie_store=cs5)
text5 = cascade5.run("https://example.com/", "example.com")
check(
    "CASCADE",
    "botblock-skips-to-next",
    isinstance(text5, str) and len(text5) > 0,
    f"text={text5!r}",
)

# SSRF propagates (not swallowed)
cs6 = FakeCookieStore()
f_ssrf = FakeFetcher(name="ssrf", raise_exc=ws.SSRFBlocked("http://x", "private"))
cascade6 = ws.ExtractCascade(fetchers=[f_ssrf], extractor=ex, cookie_store=cs6)
expect_raises(
    "CASCADE",
    "ssrf-propagates",
    ws.SSRFBlocked,
    lambda: cascade6.run("https://example.com/", "example.com"),
)

# Budget exceeded propagates
cs7 = FakeCookieStore()
f_budget = FakeFetcher(
    name="big", raise_exc=ws.BudgetExceeded("MAX_HTML_BYTES", 20_000_000, 99_000_000)
)
cascade7 = ws.ExtractCascade(fetchers=[f_budget], extractor=ex, cookie_store=cs7)
expect_raises(
    "CASCADE",
    "budget-propagates",
    ws.BudgetExceeded,
    lambda: cascade7.run("https://example.com/", "example.com"),
)

# =========================================================================== #
# PHASE E: RANKING QUALITY (SimHash, BM25, dedup)
# =========================================================================== #

print("\nPHASE E: RANKING QUALITY")

# SimHash: identical → distance 0
h1 = ws._simhash("The Reserve Bank held rates steady at 6.5 percent")
h2 = ws._simhash("The Reserve Bank held rates steady at 6.5 percent")
check("RANK", "simhash-identical-distance-0", ws._hamming_distance(h1, h2) == 0)

# SimHash: different texts → far apart
h3 = ws._simhash("Cricket match results from Mumbai stadium")
check("RANK", "simhash-different-far", ws._hamming_distance(h1, h3) > 10)

# BM25 scores relevant doc highest
bm_results = [
    {
        "title": "Off-topic",
        "body": "Cooking pasta with tomatoes and basil for dinner",
        "href": "http://a",
    },
    {
        "title": "Relevant",
        "body": "RBI repo rate decision monetary policy India central bank",
        "href": "http://b",
    },
    {
        "title": "Partial",
        "body": "India stock market news and analysis today",
        "href": "http://c",
    },
]
scored = ws._bm25_rerank([dict(r) for r in bm_results], "RBI repo rate monetary policy")
by_score = sorted(scored, key=lambda r: -r.get("bm25_score", 0))
check("RANK", "bm25-ranks-relevant-first", by_score[0]["title"] == "Relevant")
check("RANK", "bm25-offtopic-scores-zero", by_score[1].get("bm25_score", 0) == 0)

# Dedup drops EXACT syndicated content (same title + body, different URL)
dedup_input = [
    {
        "title": "RBI holds rates steady",
        "body": "The Reserve Bank maintained its policy rate at 6.5 percent unchanged in the latest monetary policy review meeting",
        "href": "http://a",
    },
    {
        "title": "RBI holds rates steady",
        "body": "The Reserve Bank maintained its policy rate at 6.5 percent unchanged in the latest monetary policy review meeting",
        "href": "http://b",
    },
    {
        "title": "Tech earnings Q3",
        "body": "Stock markets rallied on tech earnings reports from Apple and Microsoft",
        "href": "http://c",
    },
]
kept, dropped = ws._dedup_by_content(dedup_input, threshold=5)
check(
    "RANK",
    "dedup-drops-exact-syndicated",
    len(kept) == 2 and len(dropped) == 1,
    f"kept={len(kept)} dropped={len(dropped)}",
)

# Tier classification — STRUCTURAL only, no hardcoded domain bias
check("RANK", "tier0-gov-tld-structural", ws.domain_tier("fda.gov") == 0)
check("RANK", "tier0-edu-tld-structural", ws.domain_tier("mit.edu") == 0)
check("RANK", "tier0-ac-tld-structural", ws.domain_tier("ox.ac.uk") == 0)
check("RANK", "tier3-no-hardcoded-bias", ws.domain_tier("reuters.com") == 3)
check("RANK", "tier3-no-hardcoded-bias-2", ws.domain_tier("wikipedia.org") == 3)
check("RANK", "tier3-no-hardcoded-bias-3", ws.domain_tier("rbi.org.in") == 3)
check("RANK", "tier-config-works", len(ws._parse_domain_list("__nonexistent__")) == 0)

# Date extraction
check("RANK", "date-iso", "2025" in ws._extract_date("Published 2025-08-11"))
check("RANK", "date-named-month", "2024" in ws._extract_date("March 15, 2024"))
check("RANK", "date-none", ws._extract_date("No date in this text") == "")

# Language detection
check("RANK", "lang-en", ws._detect_language("The quick brown fox jumps") == "en")
check("RANK", "lang-hi", ws._detect_language("भारतीय रिजर्व बैंक") == "hi")
check("RANK", "lang-ru", ws._detect_language("Российский банк") == "ru")

# =========================================================================== #
# PHASE F: SETTINGS ALIAS INVARIANT
# =========================================================================== #

print("\nPHASE F: SETTINGS ALIAS INVARIANT")

s = ws.get_settings()
checks = {
    "DEFAULT_REGION": (s.search.region, ws.DEFAULT_REGION),
    "DEFAULT_MAX_RESULTS": (s.search.max_results, ws.DEFAULT_MAX_RESULTS),
    "DEFAULT_RATE_LIMIT": (s.rate.initial, ws.DEFAULT_RATE_LIMIT),
    "DEFAULT_CACHE_TTL": (s.cache.ttl, ws.DEFAULT_CACHE_TTL),
    "DEFAULT_MAX_RESPONSE_BYTES": (
        s.security.max_response_bytes,
        ws.DEFAULT_MAX_RESPONSE_BYTES,
    ),
    "DEFAULT_MAX_CRAWL_BYTES": (s.security.max_crawl_bytes, ws.DEFAULT_MAX_CRAWL_BYTES),
    "DEFAULT_MAX_REDIRECTS": (s.security.max_redirects, ws.DEFAULT_MAX_REDIRECTS),
    "OUTPUT_SCHEMA_VERSION": (s.output_schema_version, ws.OUTPUT_SCHEMA_VERSION),
}
for name, (expected, actual) in checks.items():
    check("SETTINGS", f"alias-{name}", expected == actual, f"{expected} != {actual}")

# Settings is frozen
try:
    s.call_budget = 999
    check("SETTINGS", "frozen-immutable", False, "mutation succeeded")
except Exception:
    check("SETTINGS", "frozen-immutable", True)

# =========================================================================== #
# PHASE G: SEMANTIC RERANKER (offline — embedding cache + structure)
# =========================================================================== #

print("\nPHASE G: SEMANTIC RERANKER STRUCTURE")

# Embedding cache initializes empty
rr = ws.SemanticReranker()
check("SEM", "embed-cache-starts-empty", len(rr._embed_cache) == 0)
# Weights normalize
check("SEM", "weights-sum-to-1", abs(rr._w_bm25 + rr._w_sem - 1.0) < 0.01)
check("SEM", "weight-bm25-default", abs(rr._w_bm25 - 0.4) < 0.01)
check("SEM", "weight-sem-default", abs(rr._w_sem - 0.6) < 0.01)

# Custom weights normalize
rr2 = ws.SemanticReranker(w_bm25=3, w_sem=1)  # 3:1 → 0.75/0.25
check(
    "SEM",
    "custom-weights-normalize",
    abs(rr2._w_bm25 - 0.75) < 0.01 and abs(rr2._w_sem - 0.25) < 0.01,
)

# Flag-off → passthrough (no rerank)
results_test = [{"title": "test", "body": "content", "href": "http://a"}]
os.environ.pop("WEB_SEARCH_SEMANTIC_RERANK", None)
out = ws._semantic_rerank_if_enabled("query", results_test)
check("SEM", "flag-off-passthrough", out is results_test)  # same object, no rerank

# =========================================================================== #
# PHASE H: ERROR MODEL COMPLETENESS
# =========================================================================== #

print("\nPHASE H: ERROR MODEL COMPLETENESS")

# All SearchError subclasses
error_classes = [
    ws.BackendUnavailable,
    ws.RateLimited,
    ws.FetchTimeout,
    ws.FetchBlocked,
    ws.InvalidURL,
    ws.SSRFBlocked,
    ws.BudgetExceeded,
    ws.BinaryDocument,
    ws.ExtractionFailed,
]
for ec in error_classes:
    check(
        "ERROR",
        f"subclass-of-SearchError/{ec.__name__}",
        issubclass(ec, ws.SearchError),
    )

# SSRFBlocked carries resolved_ips
ex_ssrf = ws.SSRFBlocked("http://x", "private", ["10.0.0.1"])
check("ERROR", "ssrf-carries-ips", "10.0.0.1" in ex_ssrf.resolved_ips)
check("ERROR", "ssrf-carries-reason", ex_ssrf.reason == "private")

# BinaryDocument carries content_type
ex_bin = ws.BinaryDocument("http://x/y.pdf", "application/pdf")
check("ERROR", "binary-carries-ct", ex_bin.content_type == "application/pdf")

# =========================================================================== #
# PHASE I: RESILIENT EXECUTOR (cache fast-path, neg cache)
# =========================================================================== #

print("\nPHASE I: RESILIENT EXECUTOR")

# Operation NOT called on neg cache

os.environ["WEB_SEARCH_NO_CACHE"] = "0"
ws.DEFAULT_CACHE_ENABLED = True
executor = ws.ResilientExecutor(ws._get_metrics(), ws._single_flight)
key_neg = ws._cache_key("qaqc_neg", {"q": "negtest"})
ws._cache_write(key_neg, {"error": "prev failure"}, negative=True)
called = []


def op_neg(c, s):
    called.append(True)
    return {"results": []}


executor.execute(
    cache_key=key_neg,
    cache_ttl=60,
    neg_cache_ttl=60,
    category="qaqc_neg",
    query="negtest",
    params={"q": "negtest"},
    operation=op_neg,
)
check("EXEC", "neg-cache-skips-operation", called == [])

os.environ["WEB_SEARCH_NO_CACHE"] = "1"

# =========================================================================== #
# PHASE J: HTTP METHOD / SCHEME VALIDATION
# =========================================================================== #

print("\nPHASE J: SCHEME VALIDATION")

for scheme in ["ftp", "file", "gopher", "dict", "ldap", "javascript", "data", "blob"]:
    expect_raises(
        "SCHEME",
        f"blocks-{scheme}",
        (ws.InvalidURL, ws.SSRFBlocked),
        lambda s=scheme: r.resolve_and_validate(f"{scheme}://example.com/"),
    )

check(
    "SCHEME",
    "allows-http",
    r.resolve_and_validate("http://example.com/").scheme == "http",
)
check(
    "SCHEME",
    "allows-https",
    r.resolve_and_validate("https://example.com/").scheme == "https",
)

# =========================================================================== #
# PHASE K: DEDUP BODY-ONLY (Gap 1 fix — syndicated content)
# =========================================================================== #

print("\nPHASE K: DEDUP BODY-ONLY (syndicated content)")

syn_input = [
    {
        "title": "RBI holds rates steady",
        "body": "The Reserve Bank maintained its policy rate at 6.5 percent unchanged in the latest monetary policy committee meeting today",
        "href": "http://a",
    },
    {
        "title": "Central bank keeps key rate unchanged",
        "body": "The Reserve Bank maintained its policy rate at 6.5 percent unchanged in the latest monetary policy committee meeting today",
        "href": "http://b",
    },
    {
        "title": "Tech earnings",
        "body": "Apple and Microsoft reported strong quarterly earnings beating analyst expectations across all segments",
        "href": "http://c",
    },
]
syn_kept, syn_dropped = ws._dedup_by_content([dict(r) for r in syn_input], threshold=5)
check(
    "DEDUP",
    "syndicated-diff-title-same-body",
    len(syn_kept) == 2 and len(syn_dropped) == 1,
    f"kept={len(syn_kept)} dropped={len(syn_dropped)}",
)

diff_input = [
    {
        "title": "Market update",
        "body": "Stocks rallied today on positive economic data showing strong GDP growth",
        "href": "http://a",
    },
    {
        "title": "Market update",
        "body": "Bonds sold off sharply as the Federal Reserve signaled potential rate increases",
        "href": "http://b",
    },
]
diff_kept, diff_dropped = ws._dedup_by_content(
    [dict(r) for r in diff_input], threshold=5
)
check(
    "DEDUP",
    "same-title-diff-body-no-dedup",
    len(diff_kept) == 2,
    f"should keep both, kept={len(diff_kept)}",
)

# =========================================================================== #
# PHASE L: PRIORITY FRONTIER SCORING (Gap 2 fix)
# =========================================================================== #

print("\nPHASE L: PRIORITY FRONTIER SCORING")

qt = {"mean", "reversion", "rsi", "strategy", "backtest"}
check(
    "FRONTIER",
    "exact-match-high-score",
    ws._score_link_by_query("https://x.com/mean-reversion-rsi-strategy", qt) >= 3,
)
check(
    "FRONTIER",
    "stem-match-strategy-strategies",
    ws._score_link_by_query("https://x.com/trading-strategies", qt) >= 1,
)
check(
    "FRONTIER",
    "stem-match-reversion-reversal",
    ws._score_link_by_query("https://x.com/rsi-reversal-backtest", qt) >= 2,
)
check(
    "FRONTIER",
    "irrelevant-scores-zero",
    ws._score_link_by_query("https://x.com/testimonials", qt) == 0,
)
check(
    "FRONTIER",
    "cart-scores-zero",
    ws._score_link_by_query("https://x.com/cart", qt) == 0,
)
check(
    "FRONTIER",
    "no-query-zero",
    ws._score_link_by_query("https://x.com/anything", set()) == 0,
)

# =========================================================================== #
# PHASE M: EXTRACTION CASCADE — raw HTML detection + fallbacks
# =========================================================================== #

print("\nPHASE M: EXTRACTION RAW-HTML DETECTION + FALLBACKS")

raw_html = "<!doctype html><html><head><title>Test</title></head><body><p>Hello world content here for testing</p></body></html>"
traf_result = ws._extract_trafilatura(raw_html, "https://x.com")
check(
    "EXTRACT",
    "trafilatura-on-simple-html",
    len(traf_result) > 0 and "hello" in traf_result.lower(),
)

h2t_result = ws._html_to_text_fallback(raw_html)
check(
    "EXTRACT",
    "html2text-fallback-works",
    len(h2t_result) > 0 and "hello" in h2t_result.lower(),
)

read_result = ws._extract_readability(raw_html)
check("EXTRACT", "readability-works", len(read_result) > 0)

full_result = ws._html_to_markdown(raw_html, "https://x.com")
is_clean = not (
    full_result.strip().startswith("<!doctype")
    or full_result.strip().startswith("<html")
)
check(
    "EXTRACT",
    "full-cascade-cleans-html",
    is_clean and len(full_result) > 10,
    f"result={full_result[:60]}",
)

# =========================================================================== #
# PHASE N: RESEARCH ORCHESTRATOR (key_passage + confidence)
# =========================================================================== #

print("\nPHASE N: RESEARCH ORCHESTRATOR")

test_content = (
    "# Intro\n\n"
    "Random paragraph about cooking pasta.\n\n"
    "## Strategy\n\n"
    "Mean reversion strategies exploit the tendency of prices to return to "
    "their historical average. The Z-score measures deviation from the mean.\n\n"
    "Unrelated paragraph about weather.\n"
)
qt_test = {"mean", "reversion", "strategy", "z-score"}
passage, rel = ws._extract_key_passage(test_content, qt_test)
check(
    "ORCH",
    "key-passage-finds-relevant",
    "mean reversion" in passage.lower(),
    f"got: {passage[:60]}",
)
check("ORCH", "key-passage-relevance-positive", rel > 0, f"relevance={rel}")

p_empty, r_empty = ws._extract_key_passage("", qt_test)
check("ORCH", "key-passage-empty-content", p_empty == "" and r_empty == 0.0)

p_noqt, r_noqt = ws._extract_key_passage(test_content, set())
check("ORCH", "key-passage-no-terms-returns-first", len(p_noqt) > 0 and r_noqt == 0.0)

conf1, reason1 = ws._compute_confidence(
    [{"content": "x" * 500}, {"content": "y" * 500}, {"content": "z" * 500}],
    total_results=25,
    unique_domains=8,
)
check(
    "ORCH",
    "confidence-high-sufficient",
    conf1 >= 0.7 and reason1 == "sufficient_evidence",
    f"conf={conf1}",
)

conf2, reason2 = ws._compute_confidence([], total_results=10, unique_domains=3)
check("ORCH", "confidence-low-no-extractions", conf2 < 0.3, f"conf={conf2}")

conf3, reason3 = ws._compute_confidence([], total_results=0, unique_domains=0)
check("ORCH", "confidence-zero-nothing", conf3 == 0.0 and reason3 == "no_results")

conf4, reason4 = ws._compute_confidence(
    [{"content": "short"}, {"content": "x" * 500}, {"content": None}],
    total_results=5,
    unique_domains=2,
)
check("ORCH", "confidence-partial-mixed", conf4 < 0.7, f"conf={conf4}")

# =========================================================================== #
# SUMMARY
# =========================================================================== #

print("\n" + "=" * 60)
print(f"QAQC HARNESS SUMMARY: {PASS} PASS / {FAIL} FAIL / {PASS + FAIL} TOTAL")
print("=" * 60)

if RESULTS:
    # Group by category
    from collections import defaultdict

    by_cat = defaultdict(lambda: [0, 0])
    for cat, _, status in RESULTS:
        if status == "PASS":
            by_cat[cat][0] += 1
        else:
            by_cat[cat][1] += 1
    print("\nBY CATEGORY:")
    for cat in sorted(by_cat):
        p, f = by_cat[cat]
        status = "OK" if f == 0 else "FAIL"
        print(f"  [{status}] {cat:12s}: {p} pass / {f} fail")

    if FAIL:
        print("\nFAILURES:")
        for cat, name, status in RESULTS:
            if status != "PASS":
                print(f"  {cat}/{name}: {status}")

sys.exit(1 if FAIL else 0)
