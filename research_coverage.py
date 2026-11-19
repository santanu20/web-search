"""Dimension-level research coverage matrix using source lineage evidence.

The matrix is an evidence sufficiency diagnostic, not a truth/confidence score.
It uses lexical facet-to-page matching and conservative independence clusters
from source_intelligence; it never equates result volume with coverage.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any, Iterable
from urllib.parse import urlsplit

from research_evidence import normalize_text, normalize_url

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "are", "was",
    "were", "has", "have", "had", "will", "would", "could", "should",
    "into", "over", "under", "after", "before", "during", "than", "then",
    "their", "they", "them", "its", "his", "her", "our", "your", "about",
    "between", "because", "which", "what", "when", "where", "who", "how",
    "why", "not", "but", "also", "can", "may", "more", "most", "such",
})


def _terms(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9][a-z0-9'-]{2,}", normalize_text(value))
        if token not in _STOPWORDS
    }


def _best_excerpt(content: str, terms: set[str], max_chars: int = 420) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    if not paragraphs:
        paragraphs = [line.strip() for line in content.splitlines() if line.strip()]
    if not paragraphs:
        return ""
    best = max(
        paragraphs,
        key=lambda p: len(terms & _terms(p)),
        default="",
    )
    if len(best) > max_chars:
        best = best[:max_chars].rsplit(" ", 1)[0] + "…"
    return best


def build_coverage_matrix(
    facets: Iterable[str],
    extracted: Iterable[dict[str, Any]],
    source_profiles: Iterable[dict[str, Any]],
    *,
    original_query: str = "",
    min_independent_origins: int = 2,
    min_term_coverage: float = 0.5,
    max_evidence_per_facet: int = 5,
) -> dict[str, Any]:
    """Evaluate decomposed research facets against extracted evidence.

    A facet is covered only when the union of matched terms reaches the
    configured threshold and enough distinct evidence-origin clusters exist.
    Same-publisher pages and detected syndicated copies share a cluster.
    """
    profiles = {
        str(item.get("source_id")): item
        for item in source_profiles
        if item.get("source_id")
    }
    docs_by_id: dict[str, dict[str, Any]] = {}
    for item in extracted:
        url = str(item.get("url") or item.get("href") or "").strip()
        key = normalize_url(url)
        content = str(item.get("content") or "")
        if not key or not content:
            continue
        source_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        if source_id not in docs_by_id:
            docs_by_id[source_id] = {
                "source_id": source_id, "url": key,
                "title": str(item.get("title") or ""),
                "host": str(item.get("host") or urlsplit(key).hostname or ""),
                "content": content,
            }

    rows: list[dict[str, Any]] = []
    seen_facets: set[str] = set()
    for raw_facet in facets:
        facet = re.sub(r"\s+", " ", str(raw_facet or "")).strip()
        key_facet = normalize_text(facet)
        if not facet or key_facet in seen_facets:
            continue
        seen_facets.add(key_facet)
        facet_terms = _terms(facet)
        if not facet_terms:
            continue

        matches: list[dict[str, Any]] = []
        matched_union: set[str] = set()
        origin_cluster_ids: set[str] = set()
        publisher_ids: set[str] = set()
        primary_ids: set[str] = set()

        min_match = min(2, len(facet_terms))
        for source_id, doc in docs_by_id.items():
            content_terms = _terms(doc["content"])
            matched = facet_terms & content_terms
            if len(matched) < min_match:
                continue
            profile = profiles.get(source_id, {})
            publisher_id = str(profile.get("publisher_id") or "")
            cluster_id = str(profile.get("independence_cluster_id") or "")
            # Do not treat an unprofiled page as an independent origin. It can
            # still be surfaced as lexical evidence, but cannot satisfy the
            # source-independence requirement.
            if publisher_id:
                publisher_ids.add(publisher_id)
            if cluster_id:
                origin_cluster_ids.add(cluster_id)
            if profile.get("primary_source_candidate"):
                primary_ids.add(source_id)
            ratio = len(matched) / len(facet_terms)
            matched_union.update(matched)
            matches.append({
                "source_id": source_id,
                "url": doc["url"],
                "title": doc["title"],
                "host": doc["host"],
                "publisher_id": publisher_id or None,
                "independence_cluster_id": cluster_id or None,
                "source_type": profile.get("source_type", "unknown"),
                "primary_source_candidate": bool(profile.get("primary_source_candidate")),
                "matched_terms": sorted(matched),
                "term_coverage_ratio": round(ratio, 3),
                "excerpt": _best_excerpt(doc["content"], facet_terms),
            })

        matches.sort(
            key=lambda item: (
                -item["term_coverage_ratio"],
                not item["primary_source_candidate"],
                item["publisher_id"] or "",
                item["url"],
            )
        )
        # Evidence shown to the user prioritizes distinct evidence origins.
        selected: list[dict[str, Any]] = []
        seen_clusters: set[str] = set()
        for item in matches:
            cluster = item["independence_cluster_id"]
            if cluster and cluster in seen_clusters:
                continue
            selected.append(item)
            if cluster:
                seen_clusters.add(cluster)
            if len(selected) >= max(1, max_evidence_per_facet):
                break
        # Preserve one useful passage even when every candidate is in the same
        # publisher/duplicate cluster.
        if not selected and matches:
            selected = matches[:1]

        coverage_ratio = len(matched_union) / len(facet_terms)
        n_origins = len(origin_cluster_ids)
        if coverage_ratio >= min_term_coverage and n_origins >= min_independent_origins:
            status = "covered"
        elif matches:
            status = "partial"
        else:
            status = "gap"

        missing_terms = sorted(facet_terms - matched_union)
        recommended_query = ""
        if status != "covered":
            recommended_query = " ".join(
                part for part in (original_query, facet, "primary sources independent evidence") if part
            )[:400]
        rows.append({
            "facet_id": f"F{len(rows) + 1:02d}",
            "facet": facet,
            "status": status,
            "required_terms": sorted(facet_terms),
            "matched_terms": sorted(matched_union),
            "missing_terms": missing_terms,
            "term_coverage_ratio": round(coverage_ratio, 3),
            "evidence_page_count": len(matches),
            "publisher_count": len(publisher_ids),
            "independent_origin_count": n_origins,
            "primary_source_candidate_count": len(primary_ids),
            "minimum_independent_origins": max(1, min_independent_origins),
            "evidence": selected,
            "recommended_query": recommended_query,
        })

    total = len(rows)
    counts = {state: sum(1 for row in rows if row["status"] == state) for state in ("covered", "partial", "gap")}
    return {
        "summary": {
            "facet_count": total,
            **counts,
            "covered_facet_ratio": round(counts["covered"] / total, 3) if total else None,
            "facets_needing_research": [row["facet_id"] for row in rows if row["status"] != "covered"],
            "minimum_independent_origins": max(1, min_independent_origins),
            "classification": "lexical evidence sufficiency diagnostic",
            "is_factual_confidence": False,
        },
        "facets": rows,
    }
