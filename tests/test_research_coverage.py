"""Tests for evidence-driven research-dimension coverage."""
from research_coverage import build_coverage_matrix
from source_intelligence import analyze_sources


def _source(url, text, title="Research result"):
    from urllib.parse import urlsplit
    return {
        "url": url, "href": url, "host": urlsplit(url).hostname,
        "title": title, "content": text, "tier": 3,
    }


def _coverage(docs, facets, **kwargs):
    profiles = analyze_sources(
        [{"url": d["url"], "title": d["title"], "host": d["host"], "tier": d.get("tier", 3)} for d in docs],
        docs,
    )["sources"]
    return build_coverage_matrix(facets, docs, profiles, **kwargs)


def test_two_distinct_evidence_origins_cover_a_facet():
    docs = [
        _source("https://agency-one.example/report", "Battery recycling regulations require licensed collection and certified processing facilities. The agency published new compliance rules."),
        _source("https://research-two.example/paper", "Battery recycling regulations influence collection systems, material recovery rates and environmental standards. The study analyses compliance outcomes."),
    ]
    result = _coverage(docs, ["battery recycling regulations"])
    facet = result["facets"][0]
    assert facet["status"] == "covered"
    assert facet["independent_origin_count"] == 2
    assert facet["term_coverage_ratio"] == 1.0


def test_two_pages_from_one_publisher_do_not_count_as_two_origins():
    docs = [
        _source("https://www.agency.example/report", "Battery recycling regulations require licensed collection and certified processing facilities."),
        _source("https://investors.agency.example/analysis", "Battery recycling regulations influence collection systems and environmental standards."),
    ]
    result = _coverage(docs, ["battery recycling regulations"])
    facet = result["facets"][0]
    assert facet["status"] == "partial"
    assert facet["publisher_count"] == 1
    assert facet["independent_origin_count"] == 1


def test_syndicated_copies_across_hosts_do_not_count_as_independent():
    text = (
        "Battery recycling regulations require licensed collection facilities and certified "
        "processing systems. The new rules establish reporting requirements and environmental "
        "standards for all operators in the national market."
    )
    docs = [
        _source("https://publisher-one.example/story", text),
        _source("https://publisher-two.example/story", text),
    ]
    result = _coverage(docs, ["battery recycling regulations"])
    facet = result["facets"][0]
    assert facet["publisher_count"] == 2
    assert facet["independent_origin_count"] == 1
    assert facet["status"] == "partial"


def test_no_matching_evidence_is_a_gap_with_recommended_query():
    docs = [
        _source("https://source.example/page", "The history of medieval architecture includes castles and churches.")
    ]
    result = _coverage(docs, ["battery recycling regulations"], original_query="Indian battery market")
    facet = result["facets"][0]
    assert facet["status"] == "gap"
    assert facet["missing_terms"] == ["battery", "recycling", "regulations"]
    assert "primary sources independent evidence" in facet["recommended_query"]


def test_missing_independence_profiles_do_not_falsely_satisfy_origin_requirement():
    docs = [
        _source("https://one.example/a", "Battery recycling regulations require certified collection."),
        _source("https://two.example/b", "Battery recycling regulations need national standards and controls."),
    ]
    result = build_coverage_matrix(["battery recycling regulations"], docs, [])
    facet = result["facets"][0]
    assert facet["evidence_page_count"] == 2
    assert facet["independent_origin_count"] == 0
    assert facet["status"] == "partial"


def test_primary_source_candidates_are_counted_separately():
    docs = [
        _source("https://www.sebi.gov.in/legal/circular.html", "Battery recycling regulations set out licensing and reporting requirements.", "Regulatory circular"),
        _source("https://news.example.com/story", "Battery recycling regulations are a topic of industry discussion and analysis.", "News story"),
    ]
    profiles = analyze_sources(
        [{"url": d["url"], "title": d["title"], "host": d["host"], "tier": d["tier"]} for d in docs],
        docs,
    )["sources"]
    result = build_coverage_matrix(["battery recycling regulations"], docs, profiles)
    assert result["facets"][0]["primary_source_candidate_count"] == 1


def test_facets_are_deduplicated_and_stable():
    docs = [
        _source("https://one.example/a", "Battery recycling regulations require certified collection and safe processing."),
        _source("https://two.example/b", "Battery recycling regulations require collection, safe processing and reporting."),
    ]
    result = _coverage(docs, ["Battery recycling regulations", " battery   recycling regulations ", "battery collection"])
    assert [row["facet_id"] for row in result["facets"]] == ["F01", "F02"]
    assert result["summary"]["facet_count"] == 2


def test_coverage_summary_distinguishes_covered_partial_and_gap():
    docs = [
        _source("https://one.example/a", "Battery recycling regulations require licensed collection."),
        _source("https://two.example/b", "Battery recycling regulations require national reporting and controls."),
    ]
    result = _coverage(
        docs,
        ["battery recycling regulations", "battery collection", "quantum computing processor"],
    )
    summary = result["summary"]
    assert summary["facet_count"] == 3
    assert summary["covered"] + summary["partial"] + summary["gap"] == 3
    assert summary["is_factual_confidence"] is False
    assert len(summary["facets_needing_research"]) >= 1


def test_evidence_excerpts_are_bounded():
    long = "Battery recycling regulations require licensed collection. " + ("Long supporting text. " * 100)
    docs = [
        _source("https://one.example/a", long),
        _source("https://two.example/b", long + " Additional source differences."),
    ]
    result = _coverage(docs, ["battery recycling regulations"])
    assert all(len(item["excerpt"]) <= 421 for item in result["facets"][0]["evidence"])


def test_coverage_threshold_can_be_tightened():
    docs = [
        _source("https://one.example/a", "Battery recycling regulation requires licensed collection and processing."),
        _source("https://two.example/b", "Battery recycling regulation provides rules for operators."),
    ]
    normal = _coverage(docs, ["battery recycling regulations"])
    strict = _coverage(docs, ["battery recycling regulations"], min_term_coverage=1.0)
    assert normal["facets"][0]["status"] == "covered" or normal["facets"][0]["status"] == "partial"
    assert strict["facets"][0]["term_coverage_ratio"] <= 1.0
