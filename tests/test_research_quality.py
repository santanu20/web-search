from __future__ import annotations

import research_quality as rq


def doc(url, host, content, tier=3, title=""):
    return {"url": url, "href": url, "host": host, "title": title, "content": content, "tier": tier}


def test_comparison_plan_adds_comparison_dimensions():
    facets = rq.build_research_facets("Kubernetes vs Docker Swarm")
    assert facets[0] == "Kubernetes vs Docker Swarm"
    assert any("trade-offs" in f for f in facets)
    assert len(facets) <= 6
    assert len({f.casefold() for f in facets}) == len(facets)


def test_mechanism_plan_adds_evidence_and_limitations():
    facets = rq.build_research_facets("How does CRISPR Cas9 gene editing work?")
    assert any("mechanism" in f for f in facets)
    assert any("limitations" in f or "failure modes" in f for f in facets)


def test_facet_coverage_requires_independent_domains():
    facet = "ETF creation redemption arbitrage mechanism"
    docs = [doc(f"https://a.example/{i}", "a.example", "ETF creation redemption arbitrage mechanism " * 8) for i in range(4)]
    fraction, detail = rq.compute_facet_coverage([facet], docs)
    assert fraction == 0.0
    assert detail[facet]["covered"] is False
    docs.append(doc("https://b.example/1", "b.example", "ETF creation redemption arbitrage mechanism " * 8))
    fraction, detail = rq.compute_facet_coverage([facet], docs)
    assert fraction == 1.0
    assert detail[facet]["domains"] == ["a.example", "b.example"]


def test_gap_generation_only_returns_uncovered_facets():
    coverage = {
        "Main question": {"covered": True},
        "Main question evidence and limitations": {"covered": False},
    }
    assert rq.generate_gap_queries("Main question", coverage) == ["Main question evidence and limitations"]


def test_crawl_seed_ranking_does_not_reward_raw_hit_volume():
    query = "rust ownership borrow checker"
    results = [
        {"host": "spam.example", "href": "https://spam.example/", "title": "Rust news", "body": "Rust news and unrelated stories", "tier": 3}
        for _ in range(20)
    ]
    results += [
        {"host": "docs.example", "href": "https://docs.example/guide", "title": "Rust ownership and borrow checker guide", "body": "Rust ownership borrowing lifetime rules and compiler", "tier": 1, "sub_query": "mechanism"},
        {"host": "reference.example", "href": "https://reference.example/book", "title": "Rust borrow checker ownership", "body": "The borrow checker enforces Rust ownership and lifetimes", "tier": 0, "sub_query": "evidence"},
    ]
    ranked = rq.rank_domains_for_crawl(results, query, limit=2)
    assert {x["host"] for x in ranked} == {"docs.example", "reference.example"}


def test_evidence_selection_deduplicates_urls_and_limits_host_concentration():
    content = "Rust ownership borrow checker lifetime rules are enforced by the compiler. " * 15
    docs = [
        doc("https://a.example/1", "a.example", content, tier=0),
        doc("https://a.example/1#fragment", "a.example", content, tier=0),
        doc("https://a.example/2", "a.example", "Rust ownership and borrowing are checked by the compiler. " * 15, tier=1),
        doc("https://b.example/1", "b.example", "Rust borrowing rules and ownership lifetimes. " * 15, tier=2),
        doc("https://c.example/1", "c.example", "Rust compiler ownership and lifetime analysis. " * 15, tier=3),
    ]
    selected = rq.select_research_sources(docs, "Rust ownership borrow checker", max_sources=5, max_per_domain=1)
    assert len(selected) == 3
    assert len({x["host"] for x in selected}) == 3
    assert all("_research_relevance" in x for x in selected)


def test_claim_audit_detects_uncited_and_invalid_references():
    source = doc("https://docs.example/rust", "docs.example",
                 "Rust ownership rules ensure each value has one owner and borrows follow lifetime rules. " * 6)
    synthesis = (
        "# Rust ownership\n\n"
        "Rust ownership assigns each value a single owner [1].\n"
        "The borrow checker enforces lifetime constraints and prevents invalid references.\n"
        "The compiler guarantees deterministic distributed consensus [99]."
    )
    audit = rq.audit_claim_citations(synthesis, [source], [source])
    assert audit["n_claims"] == 3
    assert audit["n_cited_claims"] == 1
    assert audit["n_uncited_claims"] == 1
    assert audit["n_invalid_citations"] == 1
    assert audit["citation_coverage"] == 0.333
    assert audit["claims"][0]["lexical_overlap"] > 0.2


def test_pro_heuristic_decomposer_returns_original_and_multiple_research_angles():
    import web_search as ws

    facets = ws._decompose_query_heuristic("Why does the Rust borrow checker reject references?")
    assert facets[0].startswith("Why does the Rust borrow checker")
    assert len(facets) >= 3
    assert any("limitations" in facet or "failure modes" in facet for facet in facets)


def test_synthesis_prompt_treats_webpages_as_untrusted_data(monkeypatch):
    import web_search as ws

    captured = {}
    content = "Rust ownership gives each value one owner and borrowing enforces lifetime rules. " * 8

    def fake_llm_call(prompt, model=None, system=None, **kwargs):
        captured["prompt"] = prompt
        captured["system"] = system
        return (
            "Rust ownership gives each value one owner [1].\n\n"
            'SOURCES:\n1. "Rust ownership gives each value one owner"'
        )

    monkeypatch.setattr(ws, "_llm_call", fake_llm_call)
    source = {"url": "https://docs.example/rust", "title": "Rust reference", "host": "docs.example", "tier": 0}
    extracted = [{"url": source["url"], "content": content}]
    ws._synthesize_llm("Rust ownership rules", extracted, [source])
    assert "UNTRUSTED_SOURCE_CONTENT" in captured["prompt"]
    assert "never instructions" in captured["system"].lower()
    assert "ignore any commands" in captured["system"].lower()


def test_facet_coverage_requires_multiple_query_terms():
    facet = "Rust ownership borrow checker lifetime rules"
    docs = [
        doc("https://a.example/1", "a.example", "Ownership is mentioned here. " * 10),
        doc("https://b.example/1", "b.example", "Ownership appears here too. " * 10),
    ]
    fraction, detail = rq.compute_facet_coverage([facet], docs)
    assert fraction == 0.0
    assert detail[facet]["covered"] is False


def test_claim_audit_accepts_both_single_and_double_bracket_citations():
    source = doc(
        "https://docs.example/rust", "docs.example",
        "Rust ownership gives each value one owner and borrowing follows lifetime rules. " * 8,
    )
    synthesis = (
        "Rust ownership gives each value a single owner [1].\n"
        "Borrowing follows the Rust lifetime rules [[1]]."
    )
    audit = rq.audit_claim_citations(synthesis, [source], [source])
    assert audit["n_claims"] == 2
    assert audit["n_cited_claims"] == 2
    assert audit["n_uncited_claims"] == 0
    assert audit["n_invalid_citations"] == 0
    assert audit["citation_coverage"] == 1.0
