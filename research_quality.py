"""Research quality helpers."""
from __future__ import annotations
import math
import re
from typing import Any

STOP = set("a an and are as at be been between by can could did do does for from had has have how in into is it its more most of on or our should so than that the their them then there these this those to was we were what when where which who why will with would".split())

def terms(text: str) -> set[str]:
    return {x.lower() for x in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9.+#-]*", str(text or "")) if len(x) > 1 and x.lower() not in STOP}

def build_research_facets(query: str, max_facets: int = 6) -> list[str]:
    q = " ".join(str(query or "").split())
    if not q:
        return [q]
    out = [q]
    low = q.lower()
    if any(x in low for x in (" vs ", " versus ", "compare", "difference")):
        extra = [
            f"{q} similarities and key differences",
            f"{q} performance cost security and reliability trade-offs",
            f"{q} use cases adoption and ecosystem",
            f"{q} independent benchmarks limitations and criticism",
        ]
    elif any(x in low for x in ("how ", "why ", "mechanism", "process", "architecture")):
        extra = [
            f"{q} underlying mechanism and causes",
            f"{q} primary evidence and observed results",
            f"{q} alternative explanations limitations and failure modes",
            f"{q} practical implications and open questions",
        ]
    else:
        extra = [
            f"{q} key facts and authoritative primary sources",
            f"{q} supporting evidence data and real-world examples",
            f"{q} counterarguments limitations and uncertainties",
            f"{q} implications and independent expert analysis",
        ]
    for item in extra:
        if item.lower() not in {x.lower() for x in out} and len(out) < max_facets:
            out.append(item)
    return out

def _host(item: dict[str, Any]) -> str:
    host = str(item.get("host") or "").strip().lower().removeprefix("www.")
    if host:
        return host
    from urllib.parse import urlsplit
    try:
        return (urlsplit(str(item.get("url") or item.get("href") or "")).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""

def compute_facet_coverage(facets: list[str], extracted: list[dict[str, Any]], min_domains: int = 2):
    detail = {}
    for facet in facets:
        wanted = terms(facet)
        required = max(1, min(4, math.ceil(len(wanted) * 0.4)))
        domains, urls, matched = set(), set(), set()
        for e in extracted:
            url = str(e.get("url") or e.get("href") or "")
            host = _host(e)
            content = str(e.get("title") or "") + " " + str(e.get("content") or "")
            found = wanted & terms(content[:30000])
            if url and host and len(found) >= required:
                urls.add(url); domains.add(host); matched.update(found)
        detail[facet] = {"covered": len(domains) >= min_domains, "domains": sorted(domains), "source_count": len(urls), "matching_terms": sorted(matched)}
    return (sum(x["covered"] for x in detail.values()) / len(detail) if detail else 0.0), detail


def generate_gap_queries(original_query: str, facet_coverage: dict[str, dict[str, Any]], limit: int = 4) -> list[str]:
    original = " ".join(str(original_query or "").split())
    out = []
    for facet, detail in facet_coverage.items():
        if detail.get("covered"):
            continue
        if facet.lower() == original.lower():
            candidate = original + " independent primary sources"
        elif facet.lower().startswith(original.lower()):
            candidate = original + " " + facet[len(original):].strip(" ,:;-")
        else:
            candidate = facet
        if candidate and candidate.lower() not in {x.lower() for x in out}:
            out.append(candidate[:240])
        if len(out) >= max(1, limit):
            break
    return out


def rank_domains_for_crawl(results: list[dict[str, Any]], query: str, blocked_hosts=frozenset(), limit: int = 3) -> list[dict[str, Any]]:
    wanted = terms(query)
    blocked = {str(x).lower().removeprefix("www.") for x in blocked_hosts}
    stats = {}
    for item in results:
        host = _host(item)
        if not host or host in blocked:
            continue
        rec = stats.setdefault(host, {"host": host, "n_results": 0, "relevance": 0.0, "tier": 3, "facets": set(), "seed_url": ""})
        rec["n_results"] += 1
        body = " ".join(str(item.get(k) or "") for k in ("title", "body", "snippet", "description"))
        overlap = len(wanted & terms(body)) / len(wanted) if wanted else 0.0
        rec["relevance"] = max(rec["relevance"], overlap)
        try:
            rec["tier"] = min(rec["tier"], max(0, min(3, int(item.get("tier", 3)))))
        except (TypeError, ValueError):
            pass
        if item.get("sub_query"):
            rec["facets"].add(str(item["sub_query"]).lower())
        if not rec["seed_url"]:
            rec["seed_url"] = str(item.get("href") or item.get("canonical_url") or item.get("url") or "")
    ranked = []
    for rec in stats.values():
        authority = (3 - rec["tier"]) / 3.0
        breadth = min(len(rec["facets"]), 3) / 3.0
        score = 0.72 * rec["relevance"] + 0.18 * authority + 0.10 * breadth
        ranked.append({"host": rec["host"], "n_results": rec["n_results"], "score": round(score, 4), "best_relevance": round(rec["relevance"], 4), "tier": rec["tier"], "facet_count": len(rec["facets"]), "seed_url": rec["seed_url"]})
    ranked.sort(key=lambda x: (-x["score"], -x["best_relevance"], -min(x["n_results"], 3), x["host"]))
    return ranked[:max(0, limit)]


def select_research_sources(extracted: list[dict[str, Any]], query: str, max_sources: int = 20, max_per_domain: int = 2) -> list[dict[str, Any]]:
    if max_sources <= 0:
        return []
    wanted = terms(query)
    candidates, seen = [], set()
    for raw in extracted:
        url = str(raw.get("url") or raw.get("href") or "").strip()
        content = str(raw.get("content") or "")
        if not url or len(content) < 200:
            continue
        canonical = url.split("#", 1)[0].rstrip("/").lower()
        if canonical in seen:
            continue
        seen.add(canonical)
        doc_terms = terms(str(raw.get("title") or "") + " " + content[:12000])
        relevance = len(wanted & doc_terms) / len(wanted) if wanted else 0.0
        try:
            tier = max(0, min(3, int(raw.get("tier", 3))))
        except (TypeError, ValueError):
            tier = 3
        authority = (3 - tier) / 3.0
        length = min(len(content), 6000) / 6000.0
        item = dict(raw)
        item["_rq_terms"] = doc_terms
        item["_rq_base"] = 0.82 * relevance + 0.13 * authority + 0.05 * length
        item["_rq_relevance"] = round(relevance, 4)
        candidates.append(item)
    chosen, per_host = [], defaultdict(int)
    while candidates and len(chosen) < max_sources:
        best_idx, best_score, best_novelty = -1, -1.0, 1.0
        for idx, candidate in enumerate(candidates):
            host = _host(candidate)
            if host and per_host[host] >= max(1, max_per_domain):
                continue
            doc_terms = candidate["_rq_terms"]
            max_sim = 0.0
            for prior in chosen:
                prior_terms = prior["_rq_terms"]
                union = doc_terms | prior_terms
                sim = len(doc_terms & prior_terms) / len(union) if union else 0.0
                max_sim = max(max_sim, sim)
            novelty = 1.0 - max_sim
            score = 0.86 * candidate["_rq_base"] + 0.14 * novelty
            if score > best_score:
                best_idx, best_score, best_novelty = idx, score, novelty
        if best_idx < 0:
            break
        item = candidates.pop(best_idx)
        item["_research_relevance"] = item.pop("_rq_relevance")
        item["_research_novelty"] = round(best_novelty, 4)
        item.pop("_rq_terms", None)
        item.pop("_rq_base", None)
        chosen.append(item)
        host = _host(item)
        if host:
            per_host[host] += 1
    return chosen


def audit_claim_citations(synthesis: str, sources: list[dict[str, Any]], extracted: list[dict[str, Any]]) -> dict[str, Any]:
    """Report claim citation presence and lexical overlap, not entailment."""
    content_by_url = {}
    for item in extracted:
        url = str(item.get("url") or item.get("href") or "")
        if url:
            content_by_url[url] = str(item.get("title") or "") + " " + str(item.get("content") or "")
    claims = []
    body = str(synthesis or "").split("SOURCES:", 1)[0]
    for raw in body.splitlines():
        line = re.sub(r"^\s*(?:#{1,6}\s*|[-*+]\s*|\d+[.)]\s*)", "", raw).strip()
        if not line or line.lower().startswith(("coverage", "uncovered", "no substantive evidence")):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", line):
            sentence = sentence.strip()
            if len(sentence) < 30:
                continue
            ids = sorted({int(n) for n in re.findall(r"\[\[?(\d+)\]\]?", sentence)})
            if not ids:
                claims.append({"text": sentence[:300], "citation_indices": [], "valid_citations": [], "invalid_citations": [], "lexical_overlap": None, "status": "uncited"})
                continue
            valid = [n for n in ids if 1 <= n <= len(sources)]
            invalid = [n for n in ids if n < 1 or n > len(sources)]
            claim_terms = terms(sentence)
            overlaps = []
            for source_idx in valid:
                url = str(sources[source_idx - 1].get("url") or sources[source_idx - 1].get("href") or "")
                source_terms = terms(content_by_url.get(url, ""))
                overlaps.append(len(claim_terms & source_terms) / len(claim_terms) if claim_terms else 0.0)
            claims.append({"text": sentence[:300], "citation_indices": ids, "valid_citations": valid, "invalid_citations": invalid, "lexical_overlap": round(max(overlaps), 3) if overlaps else None, "status": "invalid" if invalid else ("cited" if valid else "invalid")})
    count = len(claims)
    cited = sum(1 for c in claims if c["valid_citations"])
    return {
        "n_claims": count,
        "n_cited_claims": cited,
        "n_uncited_claims": sum(1 for c in claims if c["status"] == "uncited"),
        "n_invalid_citations": sum(len(c["invalid_citations"]) for c in claims),
        "citation_coverage": round(cited / count, 3) if count else None,
        "claims": claims,
        "interpretation": "Citation presence and lexical overlap are diagnostics, not proof of semantic entailment or factual correctness.",
    }
