"""Source identity, provenance, type signals and conservative independence grouping.

This module intentionally reports inspectable signals rather than an opaque
source-quality score. A different host is not assumed to be an independent
publisher, and a high-authority TLD is not assumed to make every page primary.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from collections import Counter, defaultdict
from typing import Any, Iterable
from urllib.parse import urlsplit

from research_evidence import build_evidence_sources, normalize_text, normalize_url

_COMPOUND_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.in", "firm.in", "net.in", "org.in", "gen.in", "ind.in", "gov.in", "ac.in", "nic.in",
    "com.br", "com.cn", "com.sg", "co.jp", "co.nz", "com.mx",
})
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "are", "was",
    "were", "has", "have", "had", "will", "would", "could", "should",
    "into", "over", "under", "after", "before", "during", "than", "then",
    "their", "they", "them", "its", "his", "her", "our", "your", "about",
    "between", "because", "which", "what", "when", "where", "who", "how",
    "why", "not", "but", "also", "can", "may", "more", "most", "such",
})
_RE_NEWS = re.compile(r"\b(news|journal|times|post|herald|wire|media)\b", re.I)
_RE_OPINION = re.compile(r"\b(opinion|editorial|commentary|op-ed|column)\b", re.I)
_RE_PRESS = re.compile(r"\b(press release|media release|news release)\b", re.I)
_RE_ANNUAL = re.compile(
    r"\b(annual report|10-k|10-q|20-f|annual filing|financial statements|"
    r"investor presentation|quarterly results)\b", re.I
)
_RE_ACADEMIC = re.compile(
    r"\b(doi:|arxiv|pubmed|journal article|research paper|peer.reviewed|"
    r"systematic review|meta.analysis|literature review)\b", re.I
)
_RE_TECHDOCS = re.compile(r"\b(documentation|developer guide|api reference|manual)\b", re.I)
_RE_REVIEW = re.compile(r"\b(review|survey|meta.analysis|systematic review)\b", re.I)
_RE_PRIMARY_REGULATOR = re.compile(
    r"\b(regulation|regulatory|notification|circular|order|gazette|"
    r"filing|court judgment|judgement|statute|act \d{4})\b", re.I
)
_RE_RESULTS = re.compile(r"\b(methods|methodology|results|dataset|sample size|experiment)\b", re.I)


def publisher_domain(host: str) -> str:
    """Best-effort registrable-domain key using a conservative suffix list.

    This is not a Public Suffix List implementation. Unknown/private suffixes
    remain host-scoped rather than being guessed across unrelated publishers.
    """
    value = str(host or "").lower().strip().rstrip(".")
    value = value.removeprefix("www.").removeprefix("m.").removeprefix("amp.")
    if not value:
        return ""
    try:
        ipaddress.ip_address(value.strip("[]"))
        return value
    except ValueError:
        pass
    labels = [part for part in value.split(".") if part]
    if len(labels) <= 2:
        return value
    suffix2 = ".".join(labels[-2:])
    if suffix2 in _COMPOUND_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _source_type(title: str, url: str, content: str) -> tuple[str, list[str], bool]:
    text = f"{title}\n{url}\n{content[:5000]}"
    host = (urlsplit(url).hostname or "").lower()
    path = urlsplit(url).path.lower()
    signals: list[str] = []
    primary = False

    official_gov = host.endswith(".gov") or host.endswith(".gov.in") or any(
        marker in host for marker in ("rbi.org.in", "sebi.gov.in", "mca.gov.in",
                                      "indiacode.nic.in", "egazette.nic.in")
    )
    if official_gov:
        signals.append("official_government_host")
    if _RE_PRIMARY_REGULATOR.search(title + " " + path):
        signals.append("regulatory_or_filing_document")
        primary = official_gov or any(x in host for x in ("sec.gov", "rbi.org.in", "sebi.gov.in"))
    if _RE_ANNUAL.search(title + " " + path):
        signals.append("corporate_or_financial_report")
        official_report_path = (
            "/investor" in path or "/annual-report" in path
            or "/annual_report" in path or "/filings/" in path
            or host.startswith(("ir.", "investor.", "investors."))
            or bool(re.search(r"/(10-k|10-q|20-f)(/|$)", path))
        )
        if official_report_path:
            signals.append("official_investor_or_filing_path")
            primary = True
        source_type = "financial_filing_or_report"
    elif _RE_PRESS.search(title + " " + content[:1200]):
        signals.append("publisher_press_release")
        source_type = "press_release"
        primary = True
    elif _RE_ACADEMIC.search(title + " " + url):
        signals.append("scholarly_publication_identifier")
        if _RE_REVIEW.search(title):
            source_type = "literature_review"
        else:
            source_type = "scholarly_work"
            if _RE_RESULTS.search(content[:6000]):
                signals.append("methods_or_results_section")
                primary = True
    elif _RE_TECHDOCS.search(title + " " + path):
        signals.append("technical_documentation_path_or_title")
        source_type = "technical_documentation"
        primary = True
    elif official_gov:
        source_type = "government_publication"
        primary = True
    elif _RE_OPINION.search(title):
        signals.append("opinion_or_editorial_label")
        source_type = "opinion"
    elif _RE_NEWS.search(host) or _RE_NEWS.search(title):
        signals.append("news_publisher_signal")
        source_type = "news_reporting"
    elif any(p in path for p in ("/blog/", "/blogs/", "/posts/")):
        signals.append("blog_path")
        source_type = "blog"
    elif any(p in path for p in ("/about", "/product", "/company")):
        signals.append("corporate_page_path")
        source_type = "corporate_page"
    else:
        source_type = "web_page"

    if _RE_REVIEW.search(title) and source_type == "scholarly_work":
        source_type = "literature_review"
        primary = False
        signals.append("review_title_signal")

    return source_type, sorted(set(signals)), primary


def _shingles(content: str, width: int = 5) -> set[tuple[str, ...]]:
    tokens = [
        token for token in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", normalize_text(content))
        if token not in _STOPWORDS
    ]
    if len(tokens) < width:
        return set()
    return {tuple(tokens[i:i + width]) for i in range(len(tokens) - width + 1)}


def _jaccard(left: set[Any], right: set[Any]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class _UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def analyze_sources(
    sources: Iterable[dict[str, Any]],
    extracted: Iterable[dict[str, Any]],
    *,
    max_sources: int = 50,
    near_duplicate_threshold: float = 0.82,
) -> dict[str, Any]:
    """Profile source metadata, infer cautious publisher groups and syndication.

    Returned labels are heuristics for triage, not source truth or a calibrated
    credibility score. Near-duplicate matching is quadratic but bounded by
    max_sources.
    """
    source_records, by_id = build_evidence_sources(
        sources, extracted, max_sources=max_sources
    )
    meta_by_url: dict[str, dict[str, Any]] = {}
    for item in sources:
        url = str(item.get("url") or item.get("href") or "")
        if url:
            meta_by_url[normalize_url(url)] = dict(item)

    profiles: list[dict[str, Any]] = []
    shingles_by_id: dict[str, set[tuple[str, ...]]] = {}
    publisher_by_id: dict[str, str] = {}
    exact_hash_groups: dict[str, list[str]] = defaultdict(list)

    for source in source_records:
        meta = meta_by_url.get(source.url, {})
        content = source.content
        source_type, signals, primary = _source_type(source.title, source.url, content)
        publisher = publisher_domain(source.host or (urlsplit(source.url).hostname or ""))
        publisher_by_id[source.source_id] = publisher or source.host
        if content:
            shingles_by_id[source.source_id] = _shingles(content)
            exact_hash_groups[source.content_hash].append(source.source_id)
        profiles.append({
            "source_id": source.source_id,
            "url": source.url,
            "title": source.title,
            "host": source.host,
            "publisher_id": publisher or source.host,
            "source_type": source_type,
            "primary_source_candidate": primary,
            "source_signals": signals,
            "domain_tier": source.tier,
            "content_available": bool(content),
            "content_hash": source.content_hash,
            "published_date": str(meta.get("date") or meta.get("published") or "")[:32],
            "duplicate_source_ids": [],
            "independence_cluster_id": "",
            "independence_reasons": [],
        })

    ids = [source.source_id for source in source_records]
    uf = _UnionFind(ids)
    ids_by_publisher: dict[str, list[str]] = defaultdict(list)
    for source_id, publisher in publisher_by_id.items():
        ids_by_publisher[publisher].append(source_id)
    for members in ids_by_publisher.values():
        for member in members[1:]:
            uf.union(members[0], member)

    duplicate_pairs: list[dict[str, Any]] = []
    duplicate_pair_keys: set[tuple[str, str]] = set()
    duplicate_neighbors: dict[str, set[str]] = defaultdict(set)
    for left, members in exact_hash_groups.items():
        if left and len(members) > 1:
            for member in members[1:]:
                uf.union(members[0], member)
                duplicate_neighbors[members[0]].add(member)
                duplicate_neighbors[member].add(members[0])
                pair_key = tuple(sorted((members[0], member)))
                duplicate_pair_keys.add(pair_key)
                duplicate_pairs.append({"source_a": pair_key[0], "source_b": pair_key[1], "similarity": 1.0, "method": "exact_content_hash"})

    for i, left in enumerate(ids):
        left_shingles = shingles_by_id.get(left, set())
        if len(left_shingles) < 8:
            continue
        for right in ids[i + 1:]:
            if publisher_by_id.get(left) == publisher_by_id.get(right):
                continue
            right_shingles = shingles_by_id.get(right, set())
            if len(right_shingles) < 8:
                continue
            similarity = _jaccard(left_shingles, right_shingles)
            if similarity >= near_duplicate_threshold:
                pair_key = tuple(sorted((left, right)))
                if pair_key in duplicate_pair_keys:
                    continue
                duplicate_pair_keys.add(pair_key)
                uf.union(left, right)
                duplicate_neighbors[left].add(right)
                duplicate_neighbors[right].add(left)
                duplicate_pairs.append({
                    "source_a": pair_key[0], "source_b": pair_key[1],
                    "similarity": round(similarity, 3), "method": "near_duplicate_shingles",
                })

    members_by_root: dict[str, list[str]] = defaultdict(list)
    for source_id in ids:
        members_by_root[uf.find(source_id)].append(source_id)
    cluster_id_by_source: dict[str, str] = {}
    for members in members_by_root.values():
        stable = hashlib.sha256("|".join(sorted(members)).encode("utf-8")).hexdigest()[:16]
        for member in members:
            cluster_id_by_source[member] = stable

    profiles_by_id = {item["source_id"]: item for item in profiles}
    for source_id, profile in profiles_by_id.items():
        profile["independence_cluster_id"] = cluster_id_by_source[source_id]
        profile["duplicate_source_ids"] = sorted(duplicate_neighbors.get(source_id, set()))
        reasons = []
        if sum(1 for sid in ids_by_publisher.get(publisher_by_id.get(source_id, ""), [])) > 1:
            reasons.append("same_registrable_publisher_domain")
        if duplicate_neighbors.get(source_id):
            reasons.append("exact_or_near_duplicate_content")
        profile["independence_reasons"] = reasons

    type_counts = Counter(item["source_type"] for item in profiles)
    summary = {
        "source_count": len(profiles),
        "extracted_source_count": sum(1 for item in profiles if item["content_available"]),
        "publisher_count": len(set(publisher_by_id.values())),
        "independence_cluster_count": len(members_by_root),
        "primary_source_candidates": sum(1 for item in profiles if item["primary_source_candidate"]),
        "duplicate_pair_count": len(duplicate_pairs),
        "source_type_counts": dict(sorted(type_counts.items())),
        "near_duplicate_threshold": near_duplicate_threshold,
        "classification_is_heuristic": True,
    }
    return {
        "summary": summary,
        "sources": profiles,
        "duplicate_pairs": duplicate_pairs,
    }
