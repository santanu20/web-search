"""Regression pins for the 2026-08-21 audit fixes (F-02, F-03, F-05, F-14,
F-24, F-28, F-33, F-36, F-37, F-39, F-40).

Every test here pins a live-verified bug from .audit/findings.md — if one
fails, a fix regressed. No network: all deterministic unit-level contracts.
"""

from __future__ import annotations

import time

import argparse as _ap
import inspect
import json

import pytest

from urllib.robotparser import RobotFileParser

from conftest import FakeFetcher, FakeExtractor, FakeCookieStore, make_fetch_result, ws


@pytest.fixture
def cookie_store():
    return FakeCookieStore()


# --------------------------------------------------------------------------- #
# F-02: binary magic sniff at the cascade boundary
# --------------------------------------------------------------------------- #


def test_sniff_detects_pdf_magic():
    assert ws._sniff_binary("%PDF-1.5 ...binary junk...") == "application/pdf"


def test_sniff_detects_zip_magic():
    assert ws._sniff_binary("PK\x03\x04archive") == "application/zip"


def test_sniff_clean_html_is_none():
    assert ws._sniff_binary("<html><body>real page</body></html>") is None
    assert ws._sniff_binary("Plain text article body.") is None


def test_cascade_raises_binary_on_pdf_body_despite_text_plain(cookie_store):
    """F-02 live shape: ddgs layer returns PDF bytes labeled text/plain."""
    fetcher = FakeFetcher(
        results=[
            make_fetch_result(
                html="%PDF-1.5 %\ufffd 125 0 obj <<stream garbage",
                content_type="text/plain",
                source="ddgs",
            )
        ]
    )
    cascade = ws.ExtractCascade(
        fetchers=[fetcher], extractor=FakeExtractor(), cookie_store=cookie_store
    )
    with pytest.raises(ws.BinaryDocument) as exc_info:
        cascade.run("https://arxiv.org/pdf/2401.00001", "arxiv.org")
    assert exc_info.value.content_type == "application/pdf"


# --------------------------------------------------------------------------- #
# F-03: bot-block detection on realistic-size challenge pages
# --------------------------------------------------------------------------- #


def test_bot_blocked_on_6kb_challenge_page():
    """Live shape: scrapingcourse.com CF challenge = 6,006 chars (old guard:
    >2000 chars → undetected)."""
    challenge = (
        "<html><head><title>Just a moment...</title>"
        "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate.js'></script>"
        "</head><body>" + "x" * 6000 + "</body></html>"
    )
    assert ws._is_bot_blocked(challenge) is True


def test_bot_not_blocked_on_real_content_without_indicators():
    real = (
        "<html><body>"
        + "<p>Real article about CDN networks.</p>" * 500
        + "</body></html>"
    )
    assert ws._is_bot_blocked(real) is False


# --------------------------------------------------------------------------- #
# F-05: exit-code contract
# --------------------------------------------------------------------------- #


def test_main_returns_1_on_ssrf_crawl_start():
    """crawl with SSRF-blocked start URL must exit 1 (was 0)."""
    args = _ap.Namespace(
        cmd="crawl",
        self_check=False,
        url="http://169.254.169.254/",
        max_pages=1,
        max_chars=0,
        bfs=False,
        no_cache=True,
        dht=False,
        proxy=None,
        timeout=10,
        call_budget=5.0,
        query=None,
        max_depth=0,
    )
    assert ws.main_with_args(args) == 1


# --------------------------------------------------------------------------- #
# F-14: negative cache only for persistent failures
# --------------------------------------------------------------------------- #


def test_transient_error_not_neg_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "DEFAULT_CACHE_ENABLED", True)
    monkeypatch.setattr(ws, "DEFAULT_CACHE_DIR", tmp_path)
    key = ws._cache_key("extract", {"url": "https://x/y"})
    ws._maybe_neg_cache(key, ws.FetchTimeout("transient blip"))
    assert not list(tmp_path.rglob("*.json"))


def test_persistent_error_neg_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "DEFAULT_CACHE_ENABLED", True)
    monkeypatch.setattr(ws, "DEFAULT_CACHE_DIR", tmp_path)
    key = ws._cache_key("extract", {"url": "https://x/y"})
    ws._maybe_neg_cache(key, ws.FetchBlocked("all layers exhausted"))
    assert list(tmp_path.rglob("*.json")), "FetchBlocked must be negative-cached"


# --------------------------------------------------------------------------- #
# F-24: tier-budget parser warns on dropped parts
# --------------------------------------------------------------------------- #


def test_tier_budget_warns_on_invalid_part(caplog):
    with caplog.at_level("WARNING", logger="web_search"):
        out = ws._parse_tier_budget("T0:3,TYPO:5")
    assert out == {0: 3}
    assert any("tier_budget_dropped_parts" in r.msg for r in caplog.records)


# --------------------------------------------------------------------------- #
# F-28: September date forms
# --------------------------------------------------------------------------- #


def test_sept_abbrev_date_parsed():
    assert ws._extract_date("Sept 15, 2024").startswith("2024-09-15")
    assert ws._extract_date("15 Sept 2024").startswith("2024-09-15")


# --------------------------------------------------------------------------- #
# F-33: Metrics whitelist rejects unknown counters
# --------------------------------------------------------------------------- #


def test_metrics_incr_rejects_unknown_counter():
    m = ws.Metrics()
    with pytest.raises(ValueError):
        m.incr("nonexistent_counter")


# --------------------------------------------------------------------------- #
# F-36: rank sequence contract (mutation M3 pin)
# --------------------------------------------------------------------------- #


def test_apply_sort_assigns_sequential_ranks():
    results = [
        {"host": "a.com", "tier": 3, "relevance_score": 0, "date": ""},
        {"host": "b.com", "tier": 0, "relevance_score": 5, "date": "2025-08-01"},
        {"host": "c.com", "tier": 3, "relevance_score": 1, "date": "2025-08-09"},
    ]
    sorted_r = ws._apply_sort(list(results), "latest")
    assert [r["rank"] for r in sorted_r] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# F-37: dedup drops near-dupes (mutation M4 pin — vacuous assert replaced)
# --------------------------------------------------------------------------- #


def test_dedup_drops_and_keeps_are_disjoint():
    results = [
        {"title": "A", "body": "same body text", "href": "http://a", "tier": 3},
        {"title": "A2", "body": "same body text", "href": "http://b", "tier": 3},
        {"title": "B", "body": "different content", "href": "http://c", "tier": 3},
    ]
    kept, dropped = ws._dedup_by_content(results, threshold=5)
    kept_hrefs = {r["href"] for r in kept}
    dropped_hrefs = {r["href"] for r in dropped}
    assert len(kept) == 2 and len(dropped) == 1
    assert kept_hrefs & dropped_hrefs == set()
    assert kept_hrefs | dropped_hrefs == {"http://a", "http://b", "http://c"}


# --------------------------------------------------------------------------- #
# F-39: think-tag stripping shapes
# --------------------------------------------------------------------------- #


def test_strip_lone_think_closer_keeps_tail_only():
    leaked = "deliberation " * 100 + "</think>\n# Real Answer\n\nBody."
    assert ws._strip_inline_think(leaked).startswith("# Real Answer")


def test_strip_matched_think_pair():
    assert ws._strip_inline_think("pre <think>hidden</think> post") == "pre post"


def test_strip_unclosed_think_returns_empty():
    assert ws._strip_inline_think("<think>unfinished") == ""


def test_strip_clean_text_untouched():
    assert ws._strip_inline_think("# Answer") == "# Answer"


# --------------------------------------------------------------------------- #
# F-40: LLM line filter rejects meta-commentary
# --------------------------------------------------------------------------- #


def test_filter_llm_lines_drops_meta():
    raw = (
        "The user wants me to break down the question\n"
        "Let me identify key aspects\n"
        "1. What are off-target effects?\n"
        "- How are they detected?"
    )
    out = ws._filter_llm_lines(raw, min_len=10, max_items=6)
    assert out == ["What are off-target effects?", "How are they detected?"]


def test_filter_llm_lines_caps_items():
    raw = "\n".join(f"Question number {i} about topic?" for i in range(10))
    assert len(ws._filter_llm_lines(raw, min_len=10, max_items=4)) == 4


def test_filter_llm_lines_keeps_mid_sentence_based_on():
    """Sweep-2 pin: prefix-only matching must not drop legitimate content
    lines that merely CONTAIN a marker phrase mid-sentence."""
    lines = "Strategies based on RNP delivery reduce off-target risk"
    assert ws._filter_llm_lines(lines, min_len=10, max_items=6) == [lines]


# --------------------------------------------------------------------------- #
# F-35 pin: SemanticReranker embed cache is FIFO-bounded, evicting oldest.
# --------------------------------------------------------------------------- #


def test_f35_embed_cache_fifo_bounded():
    from collections import OrderedDict

    reranker = ws.SemanticReranker.__new__(ws.SemanticReranker)
    reranker._embed_cache = OrderedDict()
    reranker._embed_cache_max = 4

    for i in range(10):
        reranker._embed_cache_put(f"h{i}", [float(i)])

    assert len(reranker._embed_cache) == 4
    assert list(reranker._embed_cache) == ["h6", "h7", "h8", "h9"]


def test_f35_embed_cache_reput_refreshes_recency():
    from collections import OrderedDict

    reranker = ws.SemanticReranker.__new__(ws.SemanticReranker)
    reranker._embed_cache = OrderedDict()
    reranker._embed_cache_max = 3

    for i in range(3):
        reranker._embed_cache_put(f"h{i}", [float(i)])
    reranker._embed_cache_put("h0", [9.0])  # re-put oldest → becomes newest
    reranker._embed_cache_put("h3", [3.0])  # evicts h1, not h0

    assert list(reranker._embed_cache) == ["h2", "h0", "h3"]
    assert reranker._embed_cache["h0"] == [9.0]


# --------------------------------------------------------------------------- #
# Live-validation pin (2026-08-21): pre-F-02-era extract-cache entries holding
# raw binary must be evicted on read, never served as markdown.
# --------------------------------------------------------------------------- #


def test_f46_binary_cache_entry_evicted_not_served(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "DEFAULT_CACHE_ENABLED", True)
    monkeypatch.setattr(ws, "DEFAULT_CACHE_DIR", tmp_path)

    url = "https://arxiv.org/pdf/2401.00001"
    key = ws._cache_key("extract", {"url": ws.canonicalize_url(url)})
    ws._cache_write(key, {"content": "%PDF-1.5 poisoned bytes"})
    assert ws._cache_path(key).exists()

    def _fail_fetch(ddgs, u, **kw):
        raise ws.FetchBlocked("all layers exhausted")

    monkeypatch.setattr(ws, "_extract_with_fallback_uncached", _fail_fetch)

    with pytest.raises((ws.FetchBlocked, ws.SearchError)) as exc_info:
        ws._extract_with_fallback(ddgs=None, url=url)

    assert "%PDF" not in str(exc_info.value)
    if ws._cache_path(key).exists():
        payload = json.loads(ws._cache_path(key).read_text(encoding="utf-8"))
        assert payload.get("__neg__") is True
        assert "%PDF" not in payload.get("content", "")


def test_f47_clean_cache_hit_still_served(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "DEFAULT_CACHE_ENABLED", True)
    monkeypatch.setattr(ws, "DEFAULT_CACHE_DIR", tmp_path)

    url = "https://en.wikipedia.org/wiki/Basalt"
    key = ws._cache_key("extract", {"url": ws.canonicalize_url(url)})
    ws._cache_write(key, {"content": "# Basalt\n\nMafic rock."})

    out = ws._extract_with_fallback(ddgs=None, url=url)
    assert "Mafic rock" in out


# --------------------------------------------------------------------------- #
# F-48 pin: boilerplate-only extractions are dropped at result assembly so
# genuine-empty can surface (exit 3) instead of junk masking it.
# --------------------------------------------------------------------------- #


def test_f48_junk_footer_result_dropped():
    junk = {
        "url": "https://x.test/",
        "content": "About Press Copyright Contact us " * 3,
    }
    assert ws._content_floor_ok(junk) is False
    kept = ws._drop_below_content_floor([junk])
    assert kept == []


def test_f48_clean_and_unextracted_results_kept():
    clean = {"url": "https://y.test/", "content": "Basalt is a mafic rock. " * 30}
    thin = {"url": "https://z.test/"}  # never extracted: no evidence, passes
    assert ws._content_floor_ok(clean) is True
    assert ws._content_floor_ok(thin) is True
    kept = ws._drop_below_content_floor([clean, thin])
    assert len(kept) == 2


def test_f48_threshold_env_overridable(monkeypatch):
    medium = {
        "url": "https://w.test/",
        "content": "Basalt paragraph text. " * 25,
    }  # ~525 chars
    assert len(medium["content"].strip()) >= 200

    monkeypatch.setattr(ws, "DEFAULT_MIN_CONTENT_CHARS", 200)
    assert ws._content_floor_ok(medium) is True

    monkeypatch.setattr(ws, "DEFAULT_MIN_CONTENT_CHARS", 1000)
    assert ws._content_floor_ok(medium) is False


# --------------------------------------------------------------------------- #
# F-48 exit semantics: junk-yielding searches surface as genuine-empty (3).
# --------------------------------------------------------------------------- #


def test_f48_exit3_when_all_extractions_unusable():
    payload = {
        "results": [{"url": "https://a.test/"}, {"url": "https://b.test/"}],
        "extracted": [
            {"url": "https://a.test/", "content": None, "error": "below_content_floor"},
            {"url": "https://b.test/", "content": None, "error": "FetchBlocked: x"},
        ],
    }
    assert ws._search_exit_code(payload, extract_ran=True) == 3


def test_f48_exit0_with_usable_or_binary():
    usable = {
        "results": [{"url": "https://a.test/"}],
        "extracted": [
            {"url": "https://a.test/", "content": None, "error": "below_content_floor"},
            {"url": "https://b.test/", "content": "Real content. " * 40},
        ],
    }
    binary = {
        "results": [{"url": "https://pdf.test/"}],
        "extracted": [{"url": "https://pdf.test/", "content": None, "binary": True}],
    }
    assert ws._search_exit_code(usable, extract_ran=True) == 0
    assert ws._search_exit_code(binary, extract_ran=True) == 0


def test_f48_no_extract_keeps_results_based_semantics():
    with_results = {"results": [{"url": "https://a.test/"}]}
    no_results = {"results": []}
    assert ws._search_exit_code(with_results, extract_ran=False) == 0
    assert ws._search_exit_code(no_results, extract_ran=False) == 3
    assert ws._search_exit_code(no_results, extract_ran=True) == 3


# --------------------------------------------------------------------------- #
# F-54 pin: adaptive crawl seed picked by query-term overlap, not raw rank.
# --------------------------------------------------------------------------- #


def test_f54_adaptive_seed_prefers_query_relevant_hit():
    results = [
        {  # rank #1 but tangential (the live failure mode)
            "href": "https://nimh.test/research/adhd-what-you-need-to-know",
            "title": "ADHD: What You Need to Know",
        },
        {
            "href": "https://pmc.test/articles/PMC123",
            "title": "Childhood trauma and adult self-esteem: a longitudinal study",
        },
    ]
    terms = {"childhood", "trauma", "adult", "self-esteem"}
    assert ws._rank_adaptive_seeds(results, terms)[0] == (
        "https://pmc.test/articles/PMC123"
    )


def test_f54_adaptive_seeds_skip_urlless_results():
    results = [{"title": "no url here"}, {"href": "https://ok.test/x"}]
    ranked = ws._rank_adaptive_seeds(results, {"x"})
    assert ranked == ["https://ok.test/x"]


def test_f65_ranked_seeds_enable_yield_fallback():
    """Fallback contract: ranker returns ORDERED candidates so dispatch can
    walk past a zero-yield seed (image viewers etc.)."""
    results = [
        {"href": "https://first.test/a", "tier": 3, "title": "banded iron x"},
        {"href": "https://second.test/b", "tier": 1, "title": "banded iron y"},
        {"href": "https://third.test/c", "tier": 0, "title": "unrelated cooking"},
    ]
    seeds = ws._rank_adaptive_seeds(results, {"banded", "iron"})
    # equal overlap -> tier tiebreak puts T1 ahead of T3; both retained so
    # dispatch can fall through when the first yields nothing
    assert seeds[0] == "https://second.test/b"
    assert "https://first.test/a" in seeds
    assert "https://third.test/c" not in seeds  # zero-overlap excluded
    # tie on overlap -> authoritative tier first (F-50b)
    tied = [
        {"href": "https://farm.test/t", "tier": 3, "title": "banded iron"},
        {"href": "https://nih.test/n", "tier": 0, "title": "banded iron"},
    ]
    assert ws._rank_adaptive_seeds(tied, {"banded", "iron"})[0] == "https://nih.test/n"


def test_f53_deepsearch_parser_accepts_tier_budget():
    parser = ws._build_parser()
    args = parser.parse_args(
        ["deep-search", "childhood trauma", "--tier-budget", "T0:4,T1:2"]
    )
    assert args.tier_budget == "T0:4,T1:2"
    assert ws._parse_tier_budget(args.tier_budget) == {0: 4, 1: 2}


# --------------------------------------------------------------------------- #
# F-58 pins: explicit --backend specs are validated, not silently rerouted.
# --------------------------------------------------------------------------- #


def test_f58_mixed_spec_filters_unknown_keeps_known():
    good, bad = ws._clean_backend_spec(
        "duckduckgo, bogus ,brave", {"duckduckgo", "brave"}
    )
    assert good == ["duckduckgo", "brave"]
    assert bad == ["bogus"]


def test_f58_unknown_registry_passes_through_gracefully():
    good, bad = ws._clean_backend_spec("anything,goes", set())
    assert good == ["anything", "goes"]
    assert bad == []


def test_f58_all_unknown_yields_empty_good():
    good, bad = ws._clean_backend_spec("bogus", {"duckduckgo", "brave"})
    assert good == [] and bad == ["bogus"]


# --------------------------------------------------------------------------- #
# F-49/50/51/52 pins: passage quality + polysemy + source demotion.
# --------------------------------------------------------------------------- #


def test_f52_markdown_stripped_from_passage():
    content = (
        "Intro filler paragraph that stands alone here.\n\n"
        "![Mom and dad playing](https://img.test/x.jpg)\n\n"
        "Childhood trauma shapes adult self-esteem according to research.\n\n"
        "[Follow us](https://twitter.test/x) [Subscribe](https://s.test)"
    )
    terms = {"childhood", "trauma", "self-esteem"}
    out = ws.find_key_passage(content, terms)
    assert "childhood trauma" in out.lower()
    assert "![" not in out and "](http" not in out


def test_f51_link_scaffolding_content_rejected():
    junk_links = " ".join(
        f"[Follow us {i}](https://social.test/{i})" for i in range(12)
    )
    junk = {"url": "https://tiktok.test/discover/x", "content": junk_links}
    assert ws._content_floor_ok(junk) is False


def test_f51_real_prose_with_some_links_passes():
    prose = (
        "# Article\n\n"
        + "Real paragraph about trauma recovery science. " * 10
        + "\n\n[Source](https://journal.test/1)"
    )
    assert ws._content_floor_ok({"url": "https://a.test/", "content": prose}) is True


def test_f49_stats_passage_skipped_for_nonstats_query():
    stats_para = (
        "We computed the standard error and a confidence interval for the mean; "
        "the p-value with margin of error confirmed statistical significance."
    )
    trauma_terms = {"childhood", "confidence", "adults"}
    assert ws._is_stats_methods_noise(stats_para, trauma_terms) is True
    # Same passage is legitimate evidence for a methods query:
    stats_terms = {"confidence interval", "standard error", "mean"}
    assert ws._is_stats_methods_noise(stats_para, stats_terms) is False


def test_f49_single_strong_marker_condemns_passage():
    teaching = (
        "If we estimate the mean age for our population to be 25.5, we "
        "calculate the standard error of the mean and read it off the table."
    )
    assert ws._is_stats_methods_noise(teaching, {"childhood", "confidence"}) is True
    # Weak single marker alone must NOT condemn (study-reporting survives):
    reporting = (
        "Adults with childhood trauma showed higher resilience scores than "
        "the control group in this longitudinal study (p < 0.05)."
    )
    assert ws._is_stats_methods_noise(reporting, {"childhood", "trauma"}) is False


def test_f49_title_declared_methods_page_flagged_in_normalize():
    norm = ws._normalize_results(
        [
            {
                "title": "Confidence Intervals and Confidence Levels Explained",
                "body": "how to compute intervals from sample data",
                "href": "https://thoughtco.test/confidence-intervals",
            },
            {
                "title": "Childhood trauma and adult self-esteem: a review",
                "body": "childhood trauma confidence outcomes research",
                "href": "https://journal.test/review",
            },
        ],
        {"childhood", "confidence", "adults"},
        5,
    )
    th = next(r for r in norm if "thoughtco" in r["host"])
    jr = next(r for r in norm if "journal.test" in r["host"])
    assert th.get("stats_methods_noise") is True
    assert jr.get("stats_methods_noise") is None


def test_f50_discovery_pages_demoted_in_normalize():
    norm = ws._normalize_results(
        [
            {
                "title": "childhood trauma guide",
                "body": "childhood trauma self-esteem",
                "href": "https://www.tiktok.com/discover/childhood-trauma",
            },
            {
                "title": "childhood trauma study",
                "body": "childhood trauma self-esteem research",
                "href": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1",
            },
        ],
        {"childhood", "trauma"},
        5,
    )
    tiktok = next(r for r in norm if "tiktok" in r["host"])
    pmc = next(r for r in norm if "pmc" in r["host"])
    assert tiktok.get("low_quality_source") is True
    assert tiktok["relevance_score"] < pmc["relevance_score"]


def test_f50c_seed_skips_flagged_discovery_pages():
    results = [
        {
            "href": "https://pinterest.test/ideas/iron-formation",
            "tier": 3,
            "title": "iron formation geology ideas photos and boards",
            "low_quality_source": True,
        },
        {
            "href": "https://edu.test/bif",
            "tier": 0,
            "title": "banded iron formation geology overview",
        },
    ]
    assert ws._rank_adaptive_seeds(results, {"iron", "formation", "geology"})[0] == (
        "https://edu.test/bif"
    )


def test_f50b_seed_tiebreak_prefers_authoritative_tier():
    results = [
        {"href": "https://farm.test/a", "tier": 3, "title": "childhood trauma x"},
        {"href": "https://nih.test/b", "tier": 0, "title": "childhood trauma x"},
    ]
    seed = ws._rank_adaptive_seeds(results, {"childhood", "trauma"})[0]
    assert seed == "https://nih.test/b"


# --------------------------------------------------------------------------- #
# --retries knob: per-call attempt cap, Settings default preserved.
# --------------------------------------------------------------------------- #


def test_retries_flag_parses_with_settings_default():
    parser = ws._build_parser()
    args = parser.parse_args(["text", "q"])
    assert args.retries == ws.DEFAULT_MAX_RETRIES
    args2 = parser.parse_args(["text", "q", "--retries", "1"])
    assert args2.retries == 1


def test_retries_cap_bounds_attempts(monkeypatch):
    ws._import_ddgs()  # populate module-level ddgs exception globals
    attempts = []

    class _AlwaysFails:
        def text(self, *a, **k):
            attempts.append(1)
            raise ws._DDGSException("no results found")

    params = {
        "query": "q",
        "region": "us-en",
        "safesearch": "moderate",
        "timelimit": None,
        "max_results": 3,
        "page": 1,
        "backend": "duckduckgo",
    }
    cb = ws.CircuitBreaker(99, 60.0)
    limiter = ws.AdaptiveTokenBucket(50, 50, 10.0, 0.2, 0.2, 0.5)
    try:
        ws._do_search(
            _AlwaysFails(),
            "text",
            "q",
            params,
            "k",
            None,
            "miss",
            ws._get_metrics(),
            cb,
            limiter,
            ws.LatencyTracker(10),
            cache_ttl=60,
            call_deadline=__import__("time").monotonic() + 30,
            proxy_for_log="<none>",
            max_retries=1,
        )
    except Exception:
        pass
    assert len(attempts) == 2  # max_retries=1 -> initial + 1 retry


# --------------------------------------------------------------------------- #
# F-63 pins: runtime state (AIMD rate + circuit cooldowns) survives restarts,
# TTL-expired state is ignored, corrupt state degrades silently.
# --------------------------------------------------------------------------- #


def test_f63_state_roundtrip_restores_rate_and_circuit(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "DEFAULT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(ws, "DEFAULT_PERSIST_STATE", True)

    rl = ws._get_rate_limiter()
    cb = ws._get_cb()
    orig_rate = rl.rate
    rl.rate = 0.75  # simulate AIMD decrease after soft-blocks
    cb.record_failure("text:brave")
    cb.record_failure("text:brave")  # threshold=1 default? use explicit open
    cb._open_until["text:brave"] = time.monotonic() + 120.0

    ws._save_runtime_state()
    assert (tmp_path / "state.json").exists()

    # Fresh "process": reset in-memory singletons, restore from disk.
    rl.rate = ws.DEFAULT_RATE_LIMIT
    cb._open_until.pop("text:brave", None)
    ws._restore_runtime_state()

    assert abs(rl.rate - 0.75) < 0.01
    assert cb.is_open("text:brave") is True
    rl.rate = orig_rate
    cb.record_success("text:brave")


def test_f63_ttl_expired_state_ignored(tmp_path, monkeypatch):
    import json as _json

    monkeypatch.setattr(ws, "DEFAULT_CACHE_DIR", tmp_path)
    stale = {"schema": 1, "saved_at": time.time() - 99999, "rate": 0.1, "circuits": {}}
    (tmp_path / "state.json").write_text(_json.dumps(stale))
    rl = ws._get_rate_limiter()
    before = rl.rate

    ws._restore_runtime_state()

    assert rl.rate == before


def test_f63_corrupt_state_degrades_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "DEFAULT_CACHE_DIR", tmp_path)
    (tmp_path / "state.json").write_text("{not json")
    rl = ws._get_rate_limiter()
    before = rl.rate

    ws._restore_runtime_state()

    assert rl.rate == before


def test_f50d_seed_scorer_ignores_url_slugs():
    results = [
        {  # slug-only match: false friend must lose
            "href": "https://360cities.test/image/bif",
            "tier": 3,
            "title": "BIF panorama viewer",
        },
        {
            "href": "https://edu.test/x",
            "tier": 1,
            "title": "banded iron formation geology overview",
        },
    ]
    terms = {"banded", "iron", "formation", "geology"}
    assert ws._rank_adaptive_seeds(results, terms)[0] == "https://edu.test/x"


# --------------------------------------------------------------------------- #
# F-59: query-language gate — Yandex i18n spam must not survive normalize
# --------------------------------------------------------------------------- #


def _mk_result(title: str, url: str, body: str = "") -> dict:
    return {"title": title, "href": url, "body": body or title}


def test_f59_lang_gate_drops_mismatched_script_results(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_LANG_GATE", raising=False)
    monkeypatch.delenv("WEB_SEARCH_LANG_FLOOR", raising=False)
    en = [
        _mk_result(f"English cargo answer {i}", f"https://en{i}.test/x")
        for i in range(6)
    ]
    ru = [
        _mk_result(
            "Установка Rust на Linux пошаговая инструкция", "https://selectel.ru/x"
        ),
        _mk_result("Как работает Cargo в Rust руководство", "https://habr.com/y"),
    ]
    out = ws._normalize_results(
        en + ru,
        {"cargo", "rust", "garbage"},
        3,
        query="cargo rust garbage collection target",
    )
    hosts = {r["host"] for r in out}
    assert "selectel.ru" not in hosts
    assert "habr.com" not in hosts
    assert len(out) == 6


def test_f59_lang_gate_failopen_below_floor(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_LANG_GATE", raising=False)
    monkeypatch.delenv("WEB_SEARCH_LANG_FLOOR", raising=False)
    en = [_mk_result("only english hit", "https://en0.test/x")]
    ru = [_mk_result("Установка Rust на Linux", "https://habr.com/y")]
    out = ws._normalize_results(
        en + ru,
        {"cargo"},
        3,
        query="cargo garbage collection",
    )
    assert len(out) == 2  # floor=5 unmet -> keep everything


def test_f59_lang_gate_disabled_via_env(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_LANG_GATE", "0")
    en = [
        _mk_result(f"English cargo answer {i}", f"https://en{i}.test/x")
        for i in range(6)
    ]
    ru = [_mk_result("Установка Rust на Linux", "https://habr.com/y")]
    out = ws._normalize_results(
        en + ru,
        {"cargo"},
        3,
        query="cargo garbage collection",
    )
    assert len(out) == 7


def test_f59_lang_gate_symmetric_ru_query_keeps_ru(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_LANG_GATE", raising=False)
    monkeypatch.delenv("WEB_SEARCH_LANG_FLOOR", raising=False)
    ru = [
        _mk_result(f"Установка Rust инструкция {i}", f"https://ru{i}.test/x")
        for i in range(6)
    ]
    en = [_mk_result("English cargo tutorial", "https://en0.test/x")]
    out = ws._normalize_results(
        ru + en,
        {"cargo"},
        3,
        query="установка rust окружение инструкция",
    )
    hosts = {r["host"] for r in out}
    assert "en0.test" not in hosts
    assert len(out) == 6


def test_f59_lang_gate_keeps_unknown_language(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_LANG_GATE", raising=False)
    monkeypatch.delenv("WEB_SEARCH_LANG_FLOOR", raising=False)
    en = [
        _mk_result(f"English cargo answer {i}", f"https://en{i}.test/x")
        for i in range(6)
    ]
    unknown = [{"title": "123 456", "href": "https://u0.test/x", "body": "123"}]
    out = ws._normalize_results(
        en + unknown,
        {"cargo"},
        3,
        query="cargo garbage collection",
    )
    assert any(r["host"] == "u0.test" for r in out)


# --------------------------------------------------------------------------- #
# F-60: coverage-weighted relevance — partial matches must not rival full
# --------------------------------------------------------------------------- #


def test_f60_score_relevance_coverage_weighting():
    terms = {
        "rust",
        "cargo",
        "garbage",
        "collection",
        "target",
        "directory",
        "issue",
        "6724",
        "status",
    }
    partial = {
        "title": "Incremental Garbage Collection in Unity",
        "href": "https://unity.com/gc",
        "body": "garbage collection",
    }
    full = {
        "title": "rust cargo garbage collection target directory issue "
        "6724 status tracking",
        "href": "https://github.com/rust-lang/cargo",
        "body": "",
    }
    p = ws._score_relevance(partial, terms)
    f = ws._score_relevance(full, terms)
    assert f == 9  # full coverage: matched^2 / n
    assert p < 2  # 3-of-9 scattered: 3 * (3/9) = 1
    assert p < f


def test_f60_score_relevance_zero_terms():
    assert ws._score_relevance({"title": "x"}, set()) == 0


# --------------------------------------------------------------------------- #
# WS1: LLM provider probe — detection, /v1 short-circuit, per-host cache
# --------------------------------------------------------------------------- #


def _settings_with(**llm_overrides):
    import dataclasses

    return dataclasses.replace(
        ws._settings,
        llm=dataclasses.replace(ws._settings.llm, **llm_overrides),
    )


def test_ws1_llm_probe_detects_ollama_and_caches(monkeypatch):
    monkeypatch.setattr(ws, "_LLM_PROVIDER_CACHE", {})
    monkeypatch.setattr(
        ws,
        "_settings",
        _settings_with(provider="auto", ollama_host="http://localhost:11434"),
    )
    calls = []

    def fake_probe(url, timeout):
        calls.append(url)
        return url.endswith("/api/tags")

    monkeypatch.setattr(ws, "_probe_url", fake_probe)
    assert ws._llm_probe() == "ollama"
    assert ws._llm_probe() == "ollama"  # second call served from cache
    assert calls == ["http://localhost:11434/api/tags"]


def test_ws1_llm_probe_v1_host_is_openai(monkeypatch):
    monkeypatch.setattr(ws, "_LLM_PROVIDER_CACHE", {})
    monkeypatch.setattr(
        ws,
        "_settings",
        _settings_with(provider="auto", ollama_host="http://localhost:8000/v1"),
    )
    monkeypatch.setattr(ws, "_probe_url", lambda url, timeout: url.endswith("/models"))
    assert ws._llm_probe() == "openai"


def test_ws1_llm_probe_env_pin_short_circuits(monkeypatch):
    monkeypatch.setattr(ws, "_LLM_PROVIDER_CACHE", {})
    monkeypatch.setattr(ws, "_settings", _settings_with(provider="openai"))
    monkeypatch.setattr(
        ws,
        "_probe_url",
        lambda url, timeout: (_ for _ in ()).throw(
            AssertionError("probe must not fire when provider is pinned")
        ),
    )
    assert ws._llm_probe("http://localhost:11434") == "openai"


def test_ws1_llm_probe_none_when_silent(monkeypatch):
    monkeypatch.setattr(ws, "_LLM_PROVIDER_CACHE", {})
    monkeypatch.setattr(
        ws,
        "_settings",
        _settings_with(provider="auto", ollama_host="http://localhost:9999"),
    )
    monkeypatch.setattr(ws, "_probe_url", lambda url, timeout: False)
    assert ws._llm_probe() is None


def test_ws1_llm_egress_guard_flags_offbox_only():
    class _Log:
        def __init__(self):
            self.events = []

        def warning(self, event, **kw):
            self.events.append(event)

    import types

    fake = _Log()
    orig = ws.log
    ws.log = fake
    try:
        ws._llm_egress_guard("http://localhost:11434")
        ws._llm_egress_guard("http://127.0.0.1:11434")
        assert fake.events == []  # local hosts never flagged
        ws._llm_egress_guard("http://10.0.0.5:11434")
        assert fake.events == ["llm_egress_nonlocal"]
    finally:
        ws.log = orig


# --------------------------------------------------------------------------- #
# WS2/WS2b: tri-state flags + LLM rerank parser + refine fallback
# --------------------------------------------------------------------------- #


def _ns(use_llm=False, no_llm=False):
    import argparse

    return argparse.Namespace(use_llm=use_llm, no_llm=no_llm, model=None)


def test_ws2_llm_flags_tri_state_table():
    assert ws._llm_flags_from_args(_ns()) == ("auto", None)
    assert ws._llm_flags_from_args(_ns(use_llm=True)) == ("on", None)
    assert ws._llm_flags_from_args(_ns(no_llm=True)) == ("off", None)
    mode, err = ws._llm_flags_from_args(_ns(use_llm=True, no_llm=True))
    assert mode == "auto" and "mutually exclusive" in err


def test_ws2b_rerank_parses_scores_and_sorts(monkeypatch):
    results = [{"title": f"r{i}", "body": "b", "host": f"h{i}.test"} for i in range(3)]
    monkeypatch.setattr(ws, "_llm_call", lambda *a, **k: "1 9\n2 0\n3 5\njunk line\n")
    out, n = ws._rerank_results_llm("q", results)
    assert n == 3
    assert [r["title"] for r in out] == ["r0", "r2", "r1"]  # 9, 5, 0
    assert out[0]["llm_relevance"] == 9
    assert out[2]["llm_relevance"] == 0


def test_ws2b_rerank_clamps_and_marks_unscored_minus1(monkeypatch):
    results = [{"title": "a", "body": ""}, {"title": "b", "body": ""}]
    monkeypatch.setattr(ws, "_llm_call", lambda *a, **k: "1 99\n2 -7\n")
    out, n = ws._rerank_results_llm("q", results)
    assert n == 1  # "-7" is malformed → line skipped
    assert out[0]["llm_relevance"] == 10  # 99 clamped up to 10
    assert out[1]["llm_relevance"] == -1  # unscored marker


def test_ws2b_rerank_never_raises_on_llm_down(monkeypatch):
    def boom(*a, **k):
        raise ws.OllamaUnavailable("down")

    results = [{"title": "a", "body": ""}]
    monkeypatch.setattr(ws, "_llm_call", boom)
    out, n = ws._rerank_results_llm("q", results)
    assert n == 0
    assert [r["title"] for r in out] == ["a"]  # input order unchanged


def test_ws2b_refine_query_first_line_and_cap(monkeypatch):
    monkeypatch.setattr(ws, "_llm_call", lambda *a, **k: '"alt query" extra\nignored')
    assert ws._refine_query_llm("orig", ["seen"]) == "alt query"
    monkeypatch.setattr(ws, "_llm_call", lambda *a, **k: "x" * 500)
    assert len(ws._refine_query_llm("orig", [])) == 300
    monkeypatch.setattr(ws, "_llm_call", lambda *a, **k: "   ")
    assert ws._refine_query_llm("orig", []) == "orig"  # empty → original

    def boom(*a, **k):
        raise ws.OllamaUnavailable("down")

    monkeypatch.setattr(ws, "_llm_call", boom)
    import pytest as _pytest

    with _pytest.raises(ws.OllamaUnavailable):
        ws._refine_query_llm("orig", [])


# --------------------------------------------------------------------------- #
# WS3: Wikipedia resilience floor — MediaWiki parser (stubbed transport)
# --------------------------------------------------------------------------- #


def test_ws3_wikipedia_api_search_parses(monkeypatch):
    import json as _json

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps(
                {
                    "query": {
                        "search": [
                            {
                                "title": "Rust (programming language)",
                                "snippet": "Rust is a <b>systems</b> language",
                            },
                            {"title": "", "snippet": "dropped — no title"},
                        ]
                    }
                }
            ).encode()

    captured = {}

    def fake_urlopen(req, timeout=5.0):
        captured["url"] = req.full_url
        captured["ua"] = req.headers.get("User-agent")
        return _Resp()

    monkeypatch.setattr(ws.urllib_request, "urlopen", fake_urlopen)
    out = ws._wikipedia_api_search("rust language")
    assert "en.wikipedia.org/w/api.php" in captured["url"]
    assert "srsearch=rust+language" in captured["url"]
    assert captured["ua"] == ws._LLM_UA  # descriptive UA is mandatory
    assert len(out) == 1
    assert out[0]["title"] == "Rust (programming language)"
    assert out[0]["body"] == "Rust is a systems language"  # HTML stripped
    assert out[0]["href"] == "https://en.wikipedia.org/wiki/Rust_(programming_language)"


def test_ws3_wikipedia_api_search_network_failure_returns_empty(monkeypatch):
    def boom(req, timeout=5.0):
        raise ws.urllib_error.URLError("down")

    monkeypatch.setattr(ws.urllib_request, "urlopen", boom)
    assert ws._wikipedia_api_search("anything") == []


# --------------------------------------------------------------------------- #
# WS4: recency-intent detection — vocabulary → timelimit table
# --------------------------------------------------------------------------- #


def test_ws4_detect_recency_intent_table():
    import time as _t

    y = _t.localtime().tm_year
    table = {
        "latest fed rate decision": "d",
        "what happened TODAY in markets": "d",  # case-insensitive
        "latest news this month": "d",  # strong beats medium
        "new release notes": "w",
        "cargo-sweep features recently": "w",
        f"best rust tools {y}": "m",
        f"in {y - 1} retrospective": "m",
        "how does photosynthesis work": None,
        "rust package manager": None,
    }
    for q, expected in table.items():
        assert ws._detect_recency_intent(q) == expected, q


# --------------------------------------------------------------------------- #
# WS5: quote-grounded citations — fuzzy containment + SOURCES: verifier
# --------------------------------------------------------------------------- #


def test_ws5_quote_in_content_exact_substring_is_one():
    content = "The cargo build system compiles crates in target directory trees."
    quote = "compiles crates in target directory"
    got = ws._quote_in_content(
        ws._normalize_quote_text(quote), ws._normalize_quote_text(content), 0.65
    )
    assert got == 1.0


def test_ws5_quote_in_content_minor_drift_above_floor():
    content = "The cargo build system compiles crates in the target directory tree."
    quote = "compiles crates into target directory trees"
    got = ws._quote_in_content(
        ws._normalize_quote_text(quote), ws._normalize_quote_text(content), 0.65
    )
    assert 0.65 <= got < 1.0


def test_ws5_quote_in_content_absent_below_floor():
    content = "Rust is a systems programming language focused on safety."
    quote = "completely unrelated fabricated quote about elephants"
    got = ws._quote_in_content(
        ws._normalize_quote_text(quote), ws._normalize_quote_text(content), 0.65
    )
    assert got < 0.65


def test_ws5_quote_in_content_degenerate_inputs():
    assert ws._quote_in_content("", "content", 0.65) == 0.0
    assert ws._quote_in_content("quote", "", 0.65) == 0.0
    assert ws._quote_in_content("tiny", "tiny but longer content", 0.65) == 0.0


def test_ws5_verify_quote_grounding_flags_and_strips():
    raw = (
        "Cargo cleans via `cargo clean` [1].\n\n"
        "SOURCES:\n"
        '1. "cargo clean removes the entire target directory"\n'
        '2. "elephants never forget their rust toolchains"\n'
        "3. unparseable line without quotes"
    )
    sources = [{"url": "https://a.test/1"}, {"url": "https://b.test/2"}]
    extracted = [
        {
            "url": "https://a.test/1",
            "content": "Note: cargo clean removes the entire target directory.",
        },
        {"url": "https://b.test/2", "content": "unrelated text entirely"},
    ]
    records, stripped = ws._verify_quote_grounding(raw, sources, extracted)
    assert [(r["index"], r["grounded"]) for r in records] == [(1, True), (2, False)]
    assert records[0]["ratio"] == 1.0
    assert records[1]["ratio"] < 0.65
    assert "SOURCES:" not in stripped
    assert stripped.endswith("Cargo cleans via `cargo clean` [1].")


def test_ws5_verify_quote_grounding_no_section_passthrough():
    records, out = ws._verify_quote_grounding("plain answer, no section", [], [])
    assert records == []
    assert out == "plain answer, no section"


def test_ws5_verify_quote_grounding_out_of_range_index_ungrounded():
    raw = 'SOURCES:\n9. "quote for a source that does not exist"'
    records, _ = ws._verify_quote_grounding(raw, [{"url": "https://a.test/1"}], [])
    assert len(records) == 1
    assert records[0]["index"] == 9
    assert records[0]["grounded"] is False


# --------------------------------------------------------------------------- #
# Provider dispatch integrity — pins the 2026-09-15 shadowing incident where
# BOTH the ollama worker and the dispatcher were named _llm_call; Python used
# the last def, the OpenAI path was dead code, and no test noticed because
# nothing exercised the dispatch ENTRY end-to-end.
# --------------------------------------------------------------------------- #


def test_llm_call_dispatch_routes_to_openai_worker(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_LLM_PROVIDER", "openai")
    calls = {"probe": 0, "worker": 0}

    def fake_probe(host=None, timeout=2.0):
        calls["probe"] += 1
        return "openai"

    def fake_worker(*a, **k):
        calls["worker"] += 1
        return "openai-routed"

    monkeypatch.setattr(ws, "_llm_probe", fake_probe)
    monkeypatch.setattr(ws, "_openai_call", fake_worker)
    assert ws._llm_call("ping") == "openai-routed"
    assert calls == {"probe": 1, "worker": 1}


def test_llm_call_dispatch_routes_to_ollama_worker(monkeypatch):
    calls = {"worker": 0}

    def fake_probe(host=None, timeout=2.0):
        return "ollama"

    def fake_worker(*a, **k):
        calls["worker"] += 1
        return "ollama-routed"

    monkeypatch.setattr(ws, "_llm_probe", fake_probe)
    monkeypatch.setattr(ws, "_ollama_call", fake_worker)
    assert ws._llm_call("ping") == "ollama-routed"
    assert calls["worker"] == 1


def test_llm_call_dispatch_raises_when_no_runtime(monkeypatch):
    monkeypatch.setattr(ws, "_llm_probe", lambda host=None, timeout=2.0: None)
    with pytest.raises(ws.OllamaUnavailable):
        ws._llm_call("ping")


def test_no_shadowed_top_level_definitions():
    import ast
    from collections import Counter
    from pathlib import Path

    tree = ast.parse(Path(ws.__file__).read_text(encoding="utf-8"))
    names = Counter()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[node.name] += 1
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names[t.id] += 1
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names[node.target.id] += 1
    dupes = sorted(n for n, c in names.items() if c > 1)
    assert dupes == [], f"top-level names shadowed (last def wins): {dupes}"


def test_ollama_call_wraps_socket_timeout(monkeypatch):
    import urllib.error as ue

    def explode(req, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(ws.urllib_request, "urlopen", explode)
    with pytest.raises(ws.OllamaUnavailable, match="timed out"):
        ws._ollama_call("ping", timeout=0.01)


# --------------------------------------------------------------------------- #
# WS8: WEB_SEARCH_PROFILE presets — defaults reshape, env still wins
# --------------------------------------------------------------------------- #

_WS8_KNOB_VARS = [
    "WEB_SEARCH_PROFILE",
    "WEB_SEARCH_RATE_LIMIT",
    "WEB_SEARCH_RATE_CAP",
    "WEB_SEARCH_RATE_FLOOR",
    "WEB_SEARCH_RATE_INCREASE",
    "WEB_SEARCH_MAX_RETRIES",
    "WEB_SEARCH_BACKOFF_BASE",
    "WEB_SEARCH_BACKOFF_CAP",
    "WEB_SEARCH_CB_FAILURE_THRESHOLD",
    "WEB_SEARCH_CB_COOLDOWN",
]


def _clear_ws8_env(monkeypatch):
    for var in _WS8_KNOB_VARS:
        monkeypatch.delenv(var, raising=False)


def test_ws8_standard_profile_matches_historic_defaults(monkeypatch):
    _clear_ws8_env(monkeypatch)
    s = ws.Settings.from_env()
    assert s.rate.initial == 2.0
    assert s.rate.cap == 10.0
    assert s.rate.floor == 0.2
    assert s.rate.increase == 0.2
    assert s.search.max_retries == 4
    assert s.search.backoff_base == 1.5
    assert s.search.backoff_cap == 30.0
    assert s.circuit.failure_threshold == 3
    assert s.circuit.cooldown == 60.0


def test_ws8_polite_and_aggressive_reshape_defaults(monkeypatch):
    _clear_ws8_env(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_PROFILE", "polite")
    s = ws.Settings.from_env()
    assert s.rate.cap == 1.0
    assert s.circuit.cooldown == 120.0
    assert s.search.max_retries == 3
    monkeypatch.setenv("WEB_SEARCH_PROFILE", "aggressive")
    s = ws.Settings.from_env()
    assert s.rate.cap == 20.0
    assert s.search.max_retries == 6
    assert s.search.backoff_cap == 15.0


def test_ws8_env_var_beats_profile(monkeypatch):
    _clear_ws8_env(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_PROFILE", "polite")
    monkeypatch.setenv("WEB_SEARCH_RATE_CAP", "7.5")
    s = ws.Settings.from_env()
    assert s.rate.cap == 7.5  # env wins over preset default 1.0
    assert s.rate.floor == 0.05  # unset knobs still follow the preset


def test_ws8_invalid_profile_fails_fast(monkeypatch):
    _clear_ws8_env(monkeypatch)
    monkeypatch.setenv("WEB_SEARCH_PROFILE", "turbo")
    with pytest.raises(ValueError, match="Valid profiles"):
        ws.Settings.from_env()


# --------------------------------------------------------------------------- #
# WS7: --summarize on agentic/deep-search
# --------------------------------------------------------------------------- #


def _loop_kwargs(**over):
    kw = dict(
        max_iter=0,
        max_results_per_iter=5,
        top_n_extract=0,
        relevance_domains=5,
        max_per_domain=3,
        region="us-en",
        safesearch="moderate",
        timelimit=None,
        backend="auto",
        cache_ttl=60,
        neg_cache_ttl=60,
        extract_chars=0,
        call_budget=30.0,
        proxy_for_log="<none>",
    )
    kw.update(over)
    return kw


def test_ws7_summarize_with_no_llm_conflict_raises(monkeypatch):
    monkeypatch.setattr(ws, "_llm_probe", lambda *a, **k: "ollama")
    with pytest.raises(ValueError, match="--no-llm"):
        ws._agentic_loop(None, "q", llm_mode="off", summarize=True, **_loop_kwargs())


def test_ws7_summarize_without_runtime_fails_fast(monkeypatch):
    monkeypatch.setattr(ws, "_llm_probe", lambda *a, **k: None)
    with pytest.raises(ws.OllamaUnavailable, match="--summarize"):
        ws._agentic_loop(None, "q", llm_mode="auto", summarize=True, **_loop_kwargs())


def test_ws7_payload_carries_synthesis_fields_when_off(monkeypatch):
    monkeypatch.setattr(ws, "_llm_probe", lambda *a, **k: None)
    payload = ws._agentic_loop(None, "q", llm_mode="auto", **_loop_kwargs())
    assert payload["summarized"] is False
    assert payload["synthesis"] is None
    assert payload["ungrounded_citations"] == 0


def test_ws7_synthesis_wired_into_payload(monkeypatch):
    monkeypatch.setattr(ws, "_llm_probe", lambda *a, **k: "ollama")
    fake_result = {
        "title": "Answer page",
        "href": "https://x.test/a",
        "body": "content",
        "host": "x.test",
        "language": "en",
        "canonical_url": "https://x.test/a",
        "tier": 3,
    }
    monkeypatch.setattr(
        ws,
        "_do_search",
        lambda *a, **k: {
            "results": [dict(fake_result)],
            "meta": {"cache_status": "miss"},
        },
    )
    monkeypatch.setattr(ws, "_rerank_results_llm", lambda q, rs, **k: (rs, 0))
    monkeypatch.setattr(
        ws,
        "_extract_top_urls",
        lambda *a, **k: [
            {
                "url": "https://x.test/a",
                "content": "evidence text",
                "title": "Answer page",
                "host": "x.test",
                "tier": 3,
            }
        ],
    )
    captured = {}

    def fake_synth(query, extracted, sources, **k):
        captured["extracted"] = extracted
        captured["sources"] = sources
        return (
            "# synth",
            [1],
            [{"index": 1, "quote": "q", "grounded": False, "ratio": 0.1}],
        )

    monkeypatch.setattr(ws, "_synthesize_llm", fake_synth)
    payload = ws._agentic_loop(
        None,
        "q",
        llm_mode="auto",
        summarize=True,
        **_loop_kwargs(max_iter=1, top_n_extract=3),
    )
    assert payload["summarized"] is True
    assert payload["synthesis"] == "# synth"
    assert payload["ungrounded_citations"] == 1
    assert captured["sources"][0]["href"] == "https://x.test/a"
    assert captured["extracted"][0]["url"] == "https://x.test/a"


# --------------------------------------------------------------------------- #
# WS9: JS-render retry + UA consistency
# --------------------------------------------------------------------------- #


class _FakeFetcher:
    def __init__(self, name, responses):
        self.name = name
        self._responses = list(responses)
        self.calls = 0

    def fetch(self, url, cookies=""):
        self.calls += 1
        return self._responses.pop(0) if self._responses else None


class _FakeExtractor:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def extract(self, html, url):
        self.calls += 1
        return self._outputs.pop(0) if self._outputs else ""


class _NullCookieStore:
    @staticmethod
    def get(host):
        return ""

    @staticmethod
    def save(host, cookies):
        pass


def _fr(html, source):
    return ws.FetchResult(
        html=html, content_type="text/html", cookies="", source=source
    )


def test_ws9_js_render_retry_fires_on_spa_shell():
    shell = "<div>" + "spacer " * 4000 + "</div>"  # >20K clean HTML, no text
    rendered = "<html><body>" + "rendered content " * 30 + "</body></html>"
    curl = _FakeFetcher("curl_cffi", [_fr(shell, "curl_cffi")])
    pw = _FakeFetcher("playwright", [None, _fr(rendered, "playwright")])
    extractor = _FakeExtractor(["", "rendered content " * 30])
    cascade = ws.ExtractCascade(
        fetchers=[curl, pw], extractor=extractor, cookie_store=_NullCookieStore()
    )
    text = cascade.run("https://spa.test/app", "spa.test")
    assert len(text) >= ws._JS_RENDER_MIN_TEXT
    assert curl.calls == 1  # shell came from the HTTP layer
    assert pw.calls == 2  # regular layer (None) + one retry


def test_ws9_no_retry_when_http_layer_extracts():
    good = "<html><body>" + "plain article text " * 60 + "</body></html>"
    curl = _FakeFetcher("curl_cffi", [_fr(good, "curl_cffi")])
    pw = _FakeFetcher("playwright", [])
    extractor = _FakeExtractor(["plain article text " * 60])
    cascade = ws.ExtractCascade(
        fetchers=[curl, pw], extractor=extractor, cookie_store=_NullCookieStore()
    )
    text = cascade.run("https://plain.test/a", "plain.test")
    assert "plain article text" in text
    assert pw.calls == 0  # never needed a browser


def test_ws9_ua_major_shared_across_fingerprint_surfaces():
    import inspect

    curl_src = inspect.getsource(ws._fetch_curl_cffi)
    pw_src = inspect.getsource(ws._fetch_playwright)
    # Source contains the literal f-string placeholders — pin that every
    # advertised surface derives from the ONE constant, no raw majors left.
    assert '"Google Chrome";v="{_CHROME_UA_MAJOR}"' in curl_src
    assert "Chrome/{_CHROME_UA_MAJOR}.0.0.0" in pw_src
    assert "120" not in pw_src.replace("512", "")  # no stale raw major
    assert "prof_headers.pop(h, None)" in curl_src  # non-chrome hint strip


# --------------------------------------------------------------------------- #
# Wave 5 — crawl intelligence: robots.txt (F2), sitemap seeding (F1),
# anchor-text frontier scoring (F3), challenge re-solve loop (F4)
# --------------------------------------------------------------------------- #

_URLSET_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://quotes.test/delayed-gratification</loc></url>"
    "<url><loc>https://quotes.test/value-investing</loc></url>"
    "<url><loc>https://other.test/not-internal</loc></url>"
    "</urlset>"
)

_INDEX_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<sitemap><loc>https://quotes.test/sitemap-articles.xml</loc></sitemap>"
    "<sitemap><loc>https://quotes.test/sitemap-tags.xml</loc></sitemap>"
    "</sitemapindex>"
)

_ROBOTS_TXT = "\n".join(
    [
        "User-agent: *",
        "Disallow: /private",
        "Sitemap: https://quotes.test/sitemap.xml",
        "",
    ]
)


class _OkResolver:
    def resolve_and_validate(self, url):
        return None


def test_f1_parse_locs_urlset_index_and_malformed():
    urls, children = ws._parse_locs(_URLSET_XML)
    assert urls == [
        "https://quotes.test/delayed-gratification",
        "https://quotes.test/value-investing",
        "https://other.test/not-internal",
    ]
    assert children == []
    urls2, children2 = ws._parse_locs(_INDEX_XML)
    assert urls2 == []
    assert children2 == [
        "https://quotes.test/sitemap-articles.xml",
        "https://quotes.test/sitemap-tags.xml",
    ]
    assert ws._parse_locs("this is not xml") == ([], [])


def test_f1_fetch_sitemap_locs_filters_and_kill_switch(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_SITEMAP", raising=False)
    monkeypatch.delenv("WEB_SEARCH_SITEMAP_MAX", raising=False)
    calls = []

    def fake_fetch(url, max_bytes):
        calls.append(url)
        return _URLSET_XML

    monkeypatch.setattr(ws, "_fetch_text_bounded", fake_fetch)
    locs = ws._fetch_sitemap_locs(
        ["https://quotes.test/sitemap.xml"],
        "quotes.test",
        _OkResolver(),
        deadline=time.monotonic() + 10.0,
    )
    # external + non-internal locs filtered, internal kept
    assert locs == [
        "https://quotes.test/delayed-gratification",
        "https://quotes.test/value-investing",
    ]
    assert calls == ["https://quotes.test/sitemap.xml"]

    # R3: every sitemap URL is SSRF-validated before fetch — resolver
    # raising means NO fetch happens
    class _BlockedResolver:
        def resolve_and_validate(self, url):
            raise ws.SSRFBlocked("blocked", ["10.0.0.1"])

    monkeypatch.setattr(ws, "_fetch_text_bounded", fake_fetch)
    locs2 = ws._fetch_sitemap_locs(
        ["https://quotes.test/sitemap.xml"],
        "quotes.test",
        _BlockedResolver(),
        deadline=time.monotonic() + 10.0,
    )
    assert locs2 == []
    # kill switch
    monkeypatch.setenv("WEB_SEARCH_SITEMAP", "0")
    assert (
        ws._fetch_sitemap_locs(
            ["https://quotes.test/sitemap.xml"],
            "quotes.test",
            _OkResolver(),
            deadline=time.monotonic() + 10.0,
        )
        == []
    )


def test_f1_fetch_sitemap_locs_index_one_level(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_SITEMAP", raising=False)
    fetched = []

    def fake_fetch(url, max_bytes):
        fetched.append(url)
        if url.endswith("sitemap.xml"):
            return _INDEX_XML
        return _URLSET_XML.replace("quotes.test/sitemap", "quotes.test/other")

    monkeypatch.setattr(ws, "_fetch_text_bounded", fake_fetch)
    locs = ws._fetch_sitemap_locs(
        ["https://quotes.test/sitemap.xml"],
        "quotes.test",
        _OkResolver(),
        deadline=time.monotonic() + 10.0,
    )
    # index root fetched, then first child sitemap absorbed (one level)
    assert "https://quotes.test/sitemap.xml" in fetched
    assert "https://quotes.test/sitemap-articles.xml" in fetched
    assert any("delayed-gratification" in u for u in locs)


def test_f2_fetch_robots_txt_parse_and_kill_switch(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_ROBOTS", raising=False)

    def fake_fetch(url, max_bytes):
        assert url == "https://quotes.test/robots.txt"
        assert max_bytes == ws._ROBOTS_MAX_BYTES
        return _ROBOTS_TXT

    monkeypatch.setattr(ws, "_fetch_text_bounded", fake_fetch)
    rp = ws._fetch_robots_txt("https://quotes.test")
    assert rp is not None
    assert rp.can_fetch("*", "https://quotes.test/public") is True
    assert rp.can_fetch("*", "https://quotes.test/private/x") is False
    assert "https://quotes.test/sitemap.xml" in rp.site_maps()
    # unreachable robots -> None (degrade, never crash)
    monkeypatch.setattr(ws, "_fetch_text_bounded", lambda u, m: None)
    assert ws._fetch_robots_txt("https://quotes.test") is None
    # kill switch
    monkeypatch.setattr(ws, "_fetch_text_bounded", fake_fetch)
    monkeypatch.setenv("WEB_SEARCH_ROBOTS", "0")
    assert ws._fetch_robots_txt("https://quotes.test") is None


def test_f3_score_anchor_text_stems():
    terms = {"rsi", "mean", "reversion", "backtest"}
    assert ws._score_anchor_text("A Complete Guide to RSI Mean Reversion", terms) == 3
    assert ws._score_anchor_text("click here", terms) == 0
    assert ws._score_anchor_text("", terms) == 0
    assert ws._score_anchor_text("backtesting workbook", {"backtest"}) == 1


def test_f3_extract_links_returns_url_anchor_tuples():
    md = (
        "# Page\n\n"
        "[Guide to RSI strategies](/rsi-guide) and "
        "[value notes](https://quotes.test/value-notes).\n\n"
        "[1]: /ref-style\n"
    )
    links = ws._extract_links_from_markdown(md, "quotes.test", "https://quotes.test/")
    as_map = dict(links)
    assert as_map.get("/rsi-guide") is None  # urljoin resolves relative
    assert as_map.get("https://quotes.test/rsi-guide") == "Guide to RSI strategies"
    assert as_map.get("https://quotes.test/value-notes") == "value notes"
    assert as_map.get("https://quotes.test/ref-style") == ""


def test_f4_challenge_attempts_env(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_CHALLENGE_ATTEMPTS", raising=False)
    assert ws._challenge_max_attempts() == 2
    monkeypatch.setenv("WEB_SEARCH_CHALLENGE_ATTEMPTS", "5")
    assert ws._challenge_max_attempts() == 5
    monkeypatch.setenv("WEB_SEARCH_CHALLENGE_ATTEMPTS", "99")
    assert ws._challenge_max_attempts() == 5  # clamped high
    monkeypatch.setenv("WEB_SEARCH_CHALLENGE_ATTEMPTS", "abc")
    assert ws._challenge_max_attempts() == 2  # invalid -> default
    monkeypatch.setenv("WEB_SEARCH_CHALLENGE_ATTEMPTS", "0")
    assert ws._challenge_max_attempts() == 0  # disabled (legacy single settle)


def test_f4_challenge_loop_wired_in_playwright():
    src = inspect.getsource(ws._fetch_playwright)
    assert "while _is_bot_blocked(html) and attempts < max_attempts:" in src
    assert "page.reload(" in src
    assert "playwright_resolve_attempts" in src


def test_f2_crawl_site_robots_gate_sitemap_seed_payload(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_ROBOTS", raising=False)
    monkeypatch.delenv("WEB_SEARCH_SITEMAP", raising=False)

    def fake_robots(origin):
        rp = RobotFileParser()
        rp.parse(_ROBOTS_TXT.splitlines())
        return rp

    monkeypatch.setattr(ws, "_fetch_robots_txt", fake_robots)
    monkeypatch.setattr(
        ws,
        "_fetch_sitemap_locs",
        lambda urls, host, resolver, deadline: ["https://quotes.test/from-sitemap"],
    )
    monkeypatch.setattr(ws, "get_safe_url_resolver", lambda: _OkResolver())

    class _Limiter:
        def acquire(self, timeout=0):
            return True

    class _Metrics:
        def incr(self, key):
            pass

        def snapshot(self):
            return {}

    monkeypatch.setattr(ws, "_get_rate_limiter", lambda: _Limiter())
    monkeypatch.setattr(ws, "_get_metrics", lambda: _Metrics())
    monkeypatch.setattr(ws, "_human_delay", lambda: None)

    pages_content = {
        "https://quotes.test/": ("# Home\n[pub](/public) [priv](/private)\n"),
        "https://quotes.test/from-sitemap": "# From Sitemap\n[pub](/public)\n",
        "https://quotes.test/public": "# Public\n",
    }

    def fake_extract(ddgs, url, proxy_for_log=None):
        return pages_content[url]

    monkeypatch.setattr(ws, "_extract_with_fallback", fake_extract)

    payload = ws._crawl_site(
        None,
        "https://quotes.test/",
        max_pages=3,
        max_chars=1_000_000,
        call_deadline=time.monotonic() + 30.0,
        proxy_for_log="",
    )

    visited = [p["url"] for p in payload["pages"]]
    # order: start (depth 0) -> from-sitemap (enqueued by F1 seed before
    # page-discovered links) -> /public. /private is robots-disallowed so
    # it never enters the frontier — the third slot goes to /public.
    assert visited == [
        "https://quotes.test/",
        "https://quotes.test/from-sitemap",
        "https://quotes.test/public",
    ]
    assert payload["robots"] == {
        "fetched": True,
        "crawl_delay": None,
        "sitemap_seeded": 1,
    }


def test_f1_parse_locs_text_sitemap_format():
    txt = (
        "https://doc.rust-lang.org/book/\n"
        "https://doc.rust-lang.org/std/\n"
        "\n"
        "https://doc.rust-lang.org/cargo/\n"
    )
    urls, children = ws._parse_locs(txt)
    assert urls == [
        "https://doc.rust-lang.org/book/",
        "https://doc.rust-lang.org/std/",
        "https://doc.rust-lang.org/cargo/",
    ]
    assert children == []


# --------------------------------------------------------------------------- #
# Wave 6: UA auto-derive + closest-match impersonation + stealth extension
# --------------------------------------------------------------------------- #

def test_w6_parse_chrome_major_variants():
    assert ws._parse_chrome_major("Google Chrome for Testing 151.0.7922.34") == 151
    assert ws._parse_chrome_major("Chromium 140.0.7268.0") == 140
    assert ws._parse_chrome_major("chrome 99") == 99
    assert ws._parse_chrome_major("not a version string") is None
    assert ws._parse_chrome_major("") is None


def test_w6_derive_env_pin_wins_and_validates(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_CHROME_UA_MAJOR", "123")
    assert ws._derive_chrome_ua_major() == 123
    monkeypatch.setenv("WEB_SEARCH_CHROME_UA_MAJOR", "abc")
    with pytest.raises(ValueError, match="must be an integer"):
        ws._derive_chrome_ua_major()
    monkeypatch.setenv("WEB_SEARCH_CHROME_UA_MAJOR", "500")
    with pytest.raises(ValueError, match="plausible range"):
        ws._derive_chrome_ua_major()


def test_w6_derive_falls_back_without_binary(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_CHROME_UA_MAJOR", raising=False)
    monkeypatch.setattr(ws, "_find_playwright_chromium", lambda: None)
    assert ws._derive_chrome_ua_major() == ws._CHROME_UA_FALLBACK_MAJOR


def test_w6_derive_mtime_cache_hit_skips_spawn(tmp_path, monkeypatch):
    import os as _os

    monkeypatch.delenv("WEB_SEARCH_CHROME_UA_MAJOR", raising=False)
    fake_bin = tmp_path / "chrome"
    fake_bin.write_text("#!/bin/sh\necho should-not-run\n")
    st = fake_bin.stat()
    monkeypatch.setattr(ws, "_find_playwright_chromium", lambda: fake_bin)
    monkeypatch.setattr(ws, "DEFAULT_CACHE_DIR", tmp_path)
    (tmp_path / "chrome_ua_major.json").write_text(
        ws.json.dumps(
            {"exe": str(fake_bin), "mtime": int(st.st_mtime), "major": 150}
        )
    )

    def _no_spawn(*a, **k):  # cache hit must never reach the binary
        raise AssertionError("subprocess spawned on cache hit")

    monkeypatch.setattr(ws.subprocess, "run", _no_spawn)
    assert ws._derive_chrome_ua_major() == 150
    assert _os.environ.get("WEB_SEARCH_CHROME_UA_MAJOR") is None


def test_w6_rotation_closest_match_and_fallback(monkeypatch):
    fake = [(146, "chrome146"), (145, "chrome145"),
            (136, "chrome136"), (124, "chrome124")]
    monkeypatch.setattr(ws, "_chrome_profiles_cache", fake)
    monkeypatch.setattr(ws, "_CHROME_UA_MAJOR", 151)
    assert ws._impersonation_rotation() == [
        "chrome146", "chrome145", "firefox133", "safari17_2_ios",
    ]
    monkeypatch.setattr(ws, "_CHROME_UA_MAJOR", 130)
    assert ws._impersonation_rotation() == [
        "chrome124", "firefox133", "safari17_2_ios",
    ]
    monkeypatch.setattr(ws, "_chrome_profiles_cache", [])
    assert ws._impersonation_rotation() == list(ws._IMPERSONATION_FALLBACK)


def test_w6_stealth_and_rotation_source_pins():
    src = ws._STEALTH_JS
    assert "0x9245" in src and "0x9246" in src          # WebGL debug pair spoof
    assert "Mesa Intel" in src                          # real-GPU identity
    assert "toDataURL" in src and "getImageData" in src  # canvas farbling
    assert "Math.random" in src                          # noise must be random
    curl_src = ws.inspect.getsource(ws._fetch_curl_cffi)
    assert "_impersonation_rotation()" in curl_src       # no hardcoded list


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return ws.Path(p.chromium.executable_path).exists()
    except Exception:
        return False


@pytest.mark.skipif(not _chromium_available(), reason="no playwright chromium")
def test_w6_browser_behavioral_stealth():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.add_init_script(ws._STEALTH_JS)
        page.goto("about:blank")
        res = page.evaluate(
            """() => {
                const out = {};
                out.webdriver = navigator.webdriver === undefined ? 'undef' : String(navigator.webdriver);
                const c = document.createElement('canvas');
                c.width = 64; c.height = 64;
                const ctx = c.getContext('2d');
                ctx.fillStyle = 'rgb(120,40,200)';
                ctx.fillRect(0, 0, 64, 64);
                out.d1 = c.toDataURL(); out.d2 = c.toDataURL();
                const gl = document.createElement('canvas').getContext('webgl');
                const ext = gl.getExtension('WEBGL_debug_renderer_info');
                out.renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'no-ext';
                return out;
            }"""
        )
        browser.close()
    assert res["webdriver"] == "undef"
    assert res["d1"] != res["d2"]                      # canvas noise live
    assert "SwiftShader" not in res["renderer"]        # not headless default
    assert "Mesa Intel" in res["renderer"]             # spoofed identity


# --------------------------------------------------------------------------- #
# WS9.1: stub text (< floor) must not terminate the cascade
# --------------------------------------------------------------------------- #

def test_ws91_stub_text_triggers_js_retry_not_early_return():
    shell = "<html><body><div id=root></div>" + "x" * 21_000 + "</body></html>"
    stub = "You need to enable JavaScript to run this app."
    curl = _FakeFetcher("curl_cffi", [_fr(shell, "curl_cffi")])
    pw = _FakeFetcher(
        "playwright",
        [None, _fr("<html><body>Excalidraw is a virtual collaborative "
                    "whiteboard for sketching hand-drawn like diagrams "
                    "and much more content here" + "y" * 300 +
                    "</body></html>", "playwright")],
    )
    runner = ws.ExtractCascade(
        fetchers=[curl, pw], extractor=_FakeExtractor([stub, "rendered " + "y" * 400]),
        cookie_store=_NullCookieStore(),
    )
    text = runner.run("https://spa.test/app", "spa.test")
    assert len(text) >= ws._JS_RENDER_MIN_TEXT
    assert "JavaScript" not in text  # stub was NOT returned
    assert pw.calls == 2  # regular layer + js_render_retry both ran


def test_ws91_short_text_from_playwright_is_terminal():
    tiny = _fr("<html><body>ok</body></html>", "playwright")
    pw = _FakeFetcher("playwright", [tiny])
    runner = ws.ExtractCascade(
        fetchers=[pw], extractor=_FakeExtractor(["ok"]),
        cookie_store=_NullCookieStore(),
    )
    assert runner.run("https://tiny.test/", "tiny.test") == "ok"


def test_ws91_best_effort_short_text_when_all_escalations_fail():
    shell = "<html><body><div id=root></div>" + "z" * 21_000 + "</body></html>"
    stub = "enable JavaScript stub"
    curl = _FakeFetcher("curl_cffi", [_fr(shell, "curl_cffi")])
    pw = _FakeFetcher("playwright", [None, None])  # both passes fail
    runner = ws.ExtractCascade(
        fetchers=[curl, pw],
        extractor=_FakeExtractor([stub, "", ""]),
        cookie_store=_NullCookieStore(),
    )
    text = runner.run("https://spa.test/dead", "spa.test")
    assert text == stub  # best-effort, not an exception
