"""Tests for source provenance, publisher identity and independence grouping."""
from source_intelligence import analyze_sources, publisher_domain


def _article(url, title, content, host=None, tier=3):
    from urllib.parse import urlsplit
    return {
        "url": url, "title": title, "content": content,
        "host": host or urlsplit(url).hostname, "tier": tier,
    }


def test_publisher_domain_normalizes_www_mobile_and_compound_suffixes():
    assert publisher_domain("www.example.com") == "example.com"
    assert publisher_domain("m.example.co.in") == "example.co.in"
    assert publisher_domain("ir.company.co.uk") == "company.co.uk"
    assert publisher_domain("example.org.in") == "example.org.in"


def test_subdomains_of_one_publisher_share_one_independence_cluster():
    sources = [
        {"url": "https://www.example.com/a", "title": "One"},
        {"url": "https://investors.example.com/b", "title": "Two"},
    ]
    extracted = [
        _article("https://www.example.com/a", "One", "Different first content with enough details to avoid duplicate detection."),
        _article("https://investors.example.com/b", "Two", "Completely different second content with unrelated technical details."),
    ]
    result = analyze_sources(sources, extracted)
    profiles = result["sources"]
    assert profiles[0]["publisher_id"] == profiles[1]["publisher_id"]
    assert profiles[0]["independence_cluster_id"] == profiles[1]["independence_cluster_id"]
    assert result["summary"]["publisher_count"] == 1


def test_compound_domains_are_not_collapsed_to_shared_suffix():
    sources = [
        {"url": "https://alpha.co.in/report", "title": "Alpha"},
        {"url": "https://beta.co.in/report", "title": "Beta"},
    ]
    extracted = [
        _article("https://alpha.co.in/report", "Alpha", "Alpha company data and different report details."),
        _article("https://beta.co.in/report", "Beta", "Beta company data and different report details."),
    ]
    result = analyze_sources(sources, extracted)
    assert result["summary"]["publisher_count"] == 2
    assert result["summary"]["independence_cluster_count"] == 2


def test_government_regulatory_document_is_flagged_as_primary_candidate():
    url = "https://www.sebi.gov.in/legal/circulars/2026/circular.html"
    text = "This circular sets out regulatory requirements and an effective date."
    result = analyze_sources(
        [{"url": url, "title": "Regulatory circular"}],
        [_article(url, "Regulatory circular", text, tier=0)],
    )
    profile = result["sources"][0]
    assert profile["source_type"] == "web_page" or profile["source_type"] == "government_publication"
    assert profile["primary_source_candidate"] is True
    assert "official_government_host" in profile["source_signals"]


def test_annual_report_candidate_requires_official_investor_or_filing_path():
    sources = [
        {"url": "https://news.example.com/story", "title": "Company annual report analysis"},
        {"url": "https://investors.example.com/annual-report-2025", "title": "Annual report FY2025"},
    ]
    extracted = [
        _article(sources[0]["url"], sources[0]["title"], "A journalist discusses an annual report and explains the market."),
        _article(sources[1]["url"], sources[1]["title"], "Annual report financial statements and corporate disclosures."),
    ]
    result = analyze_sources(sources, extracted)
    assert result["sources"][0]["primary_source_candidate"] is False
    assert result["sources"][1]["primary_source_candidate"] is True


def test_syndicated_near_duplicate_articles_are_clustered_across_hosts():
    base = (
        "The company announced a new manufacturing facility in Pune with a planned "
        "capacity of two million units each year. The investment is expected to create "
        "new jobs and expand the regional supply chain. Officials said construction "
        "will begin in the next quarter and production is scheduled for the following year. "
    )
    sources = [
        {"url": "https://publisher-one.example/news", "title": "Facility announced"},
        {"url": "https://publisher-two.example/business", "title": "Facility announced"},
    ]
    extracted = [
        _article(sources[0]["url"], sources[0]["title"], base + "The statement was issued on Tuesday."),
        _article(sources[1]["url"], sources[1]["title"], base + "The statement was issued on Wednesday."),
    ]
    result = analyze_sources(sources, extracted, near_duplicate_threshold=0.75)
    assert result["summary"]["duplicate_pair_count"] >= 1
    assert result["sources"][0]["independence_cluster_id"] == result["sources"][1]["independence_cluster_id"]


def test_exact_duplicate_content_is_detected():
    same = "A long enough source document contains several repeated facts and detailed evidence about the research topic."
    sources = [
        {"url": "https://one.example/article", "title": "One"},
        {"url": "https://two.example/article", "title": "Two"},
    ]
    result = analyze_sources(sources, [_article(sources[0]["url"], "One", same), _article(sources[1]["url"], "Two", same)])
    assert result["summary"]["duplicate_pair_count"] == 1
    assert result["duplicate_pairs"][0]["method"] == "exact_content_hash"


def test_empty_extractions_are_not_misclassified_as_duplicate():
    sources = [
        {"url": "https://one.example/a", "title": "One"},
        {"url": "https://two.example/a", "title": "Two"},
    ]
    result = analyze_sources(sources, [])
    assert result["summary"]["duplicate_pair_count"] == 0
    assert result["summary"]["extracted_source_count"] == 0


def test_profiles_expose_signals_instead_of_opaque_credibility_score():
    url = "https://investors.example.com/annual-report-2025"
    result = analyze_sources(
        [{"url": url, "title": "Annual report FY2025", "tier": 1}],
        [_article(url, "Annual report FY2025", "Financial statements and annual report disclosure.", tier=1)],
    )
    profile = result["sources"][0]
    assert profile["source_type"] == "financial_filing_or_report"
    assert profile["primary_source_candidate"] is True
    assert "source_score" not in profile
    assert result["summary"]["classification_is_heuristic"] is True


def test_source_limit_bounds_pairwise_comparison():
    sources = [{"url": f"https://p{i}.example/a", "title": f"Source {i}"} for i in range(8)]
    extracted = [_article(item["url"], item["title"], "Content for source number " + str(i)) for i, item in enumerate(sources)]
    result = analyze_sources(sources, extracted, max_sources=4)
    assert result["summary"]["source_count"] == 4
