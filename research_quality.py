"""Research quality helpers."""
from __future__ import annotations
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
    return (urlsplit(str(item.get("url") or item.get("href") or "")).hostname or "").lower().removeprefix("www.")

def compute_facet_coverage(facets: list[str], extracted: list[dict[str, Any]], min_domains: int = 2):
    detail = {}
    for facet in facets:
        wanted = terms(facet)
        required = max(1, min(3, round(len(wanted) * 0.25)))
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
