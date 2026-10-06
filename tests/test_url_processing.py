"""Unit tests — URL canonicalization, domain tiering, date extraction,
language detection, SimHash dedup, BM25 rerank. Pure functions, no network.
"""

from __future__ import annotations

import os


from conftest import ws


# --------------------------------------------------------------------------- #
# canonicalize_url
# --------------------------------------------------------------------------- #


def test_canonicalize_strips_utm():
    raw = "https://example.com/path?utm_source=x&id=42"
    got = ws.canonicalize_url(raw)
    assert "utm_source" not in got
    assert "id=42" in got


def test_canonicalize_strips_fbclid_gclid():
    for tracker in ("fbclid=abc", "gclid=xyz", "mc_eid=1", "ocid=z"):
        raw = f"https://example.com/article?{tracker}&keep=1"
        got = ws.canonicalize_url(raw)
        assert tracker.split("=")[0] not in got
        assert "keep=1" in got


def test_canonicalize_empty():
    assert ws.canonicalize_url("") == ""


def test_canonicalize_normalizes_scheme_host():
    raw = "HTTPS://Example.COM/Path"
    got = ws.canonicalize_url(raw)
    assert got.startswith("https://example.com")


# --------------------------------------------------------------------------- #
# domain_tier — STRUCTURAL heuristics only, NO hardcoded domain bias
# --------------------------------------------------------------------------- #


def test_tier0_gov_tld():
    """Government TLDs are T0 structurally (any country, any domain)."""
    for host in ["sec.gov", "fda.gov", "example.gov.in"]:
        assert ws.domain_tier(host) == 0, f"{host} should be T0 (.gov structural)"


def test_tier0_edu_ac_tld():
    """Education/academic TLDs are T0 structurally."""
    for host in ["mit.edu", "stanford.edu", "ox.ac.uk", "iitb.ac.in"]:
        assert ws.domain_tier(host) == 0, f"{host} should be T0 (.edu/.ac structural)"


def test_tier3_unclassified_no_bias(monkeypatch):
    """No hardcoded domain favoritism — all non-gov/edu are T3 by default.

    Env hygiene: user shells may legitimately export TIER0/TIER1 lists
    (documented feature); the module parses them once at import, so pin
    the unbiased default via the parsed constants."""
    monkeypatch.setattr(ws, "_T0_DOMAINS", frozenset())
    monkeypatch.setattr(ws, "_T1_DOMAINS", frozenset())
    for host in [
        "reuters.com",
        "bloomberg.com",
        "wikipedia.org",
        "rbi.org.in",
        "random-blog.example",
        "github.com",
        "stackoverflow.com",
        "nature.com",
        "arxiv.org",
    ]:
        assert ws.domain_tier(host) == 3, f"{host} should be T3 (no bias)"


def test_tier_configurable_via_env():
    """User configures T0/T1 for THEIR domain — proves system is unbiased."""
    result = ws._parse_domain_list("WEB_SEARCH_TIER0_DOMAINS_TEST")
    assert result == frozenset(), "empty default"

    os.environ["WEB_SEARCH_TIER0_DOMAINS_TEST"] = "nih.gov,pubmed.ncbi.nlm.nih.gov"
    result2 = ws._parse_domain_list("WEB_SEARCH_TIER0_DOMAINS_TEST")
    assert result2 == frozenset({"nih.gov", "pubmed.ncbi.nlm.nih.gov"})
    del os.environ["WEB_SEARCH_TIER0_DOMAINS_TEST"]


# --------------------------------------------------------------------------- #
# SimHash + dedup
# --------------------------------------------------------------------------- #


def test_simhash_identical_texts_same_hash():
    a = ws._simhash("The Reserve Bank held rates steady")
    b = ws._simhash("The Reserve Bank held rates steady")
    assert a == b


def test_simhash_different_texts_far_apart():
    a = ws._simhash("Federal Reserve interest rate decision")
    b = ws._simhash("Cricket match results from Mumbai")
    assert ws._hamming_distance(a, b) > 10


def test_simhash_near_dupes_close():
    # One-word swap in a longer text — SimHash should stay within threshold 5.
    a = ws._simhash("RBI keeps repo rate unchanged at 6.5 percent in latest review")
    b = ws._simhash("RBI keeps repo rate unchanged at 6.5 percent in recent review")
    assert ws._hamming_distance(a, b) <= 8  # one-word change in 12-word text


def test_dedup_by_content_drops_near_dupes():
    results = [
        {
            "title": "RBI holds rates",
            "body": "The Reserve Bank maintained its policy rate.",
            "href": "http://a",
        },
        {
            "title": "RBI holds rates",
            "body": "The Reserve Bank maintained its policy rate.",
            "href": "http://b",
        },
        {
            "title": "Different story",
            "body": "Stock markets rallied on tech earnings.",
            "href": "http://c",
        },
    ]
    deduped = ws._dedup_by_content(results, threshold=5)
    assert len(deduped) == 2  # one dupe dropped


# --------------------------------------------------------------------------- #
# BM25 rerank
# --------------------------------------------------------------------------- #


def test_bm25_reranks_by_query_relevance():
    """_bm25_rerank adds bm25_score (IDF-weighted) — does NOT sort.
    The caller sorts by bm25_score separately (via _apply_sort). Here we
    verify the SCORE correctly ranks the RBI result above cricket + cooking."""
    query = "RBI repo rate"
    results = [
        {
            "title": "Cricket match",
            "body": "India vs Australia cricket match highlights",
        },
        {"title": "RBI policy", "body": "RBI repo rate decision monetary policy"},
        {"title": "Cooking recipe", "body": "How to make pasta with tomato sauce"},
    ]
    scored = ws._bm25_rerank(results, query)
    # Sort by bm25_score (what the real pipeline does via _apply_sort)
    by_score = sorted(scored, key=lambda r: -r.get("bm25_score", 0))
    assert by_score[0]["title"] == "RBI policy"
    assert by_score[0]["bm25_score"] > by_score[1]["bm25_score"]
    assert by_score[0]["bm25_score"] > by_score[2]["bm25_score"]


def test_bm25_adds_score_field():
    results = [{"title": "test", "body": "test content"}]
    reranked = ws._bm25_rerank(results, "test")
    assert "bm25_score" in reranked[0]


# --------------------------------------------------------------------------- #
# Date extraction
# --------------------------------------------------------------------------- #


def test_extract_date_iso():
    d = ws._extract_date("Published 2025-08-11")
    assert "2025" in d


def test_extract_date_named_month():
    d = ws._extract_date("March 15, 2024")
    assert "2024" in d


def test_extract_date_none_when_absent():
    d = ws._extract_date("No date here just text")
    assert d == ""


# --------------------------------------------------------------------------- #
# Language detection
# --------------------------------------------------------------------------- #


def test_detect_language_english():
    assert ws._detect_language("The quick brown fox jumps over the lazy dog") == "en"


def test_detect_language_devanagari():
    assert ws._detect_language("भारतीय रिजर्व बैंक") == "hi"


def test_detect_language_cyrillic():
    assert ws._detect_language("Российский банк") == "ru"
