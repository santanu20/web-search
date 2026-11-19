"""Temporal provenance and event timeline for extracted research sources.

The module keeps publication, effective, updated and discovery dates distinct.
Date extraction is conservative and every inferred date carries its basis and
confidence. A page's date is not silently treated as the date an event occurred.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

from research_evidence import normalize_url

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7,
    "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_ISO_RE = re.compile(r"\b(19\d{2}|20\d{2}|21\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])\b")
_YM_RE = re.compile(r"\b(19\d{2}|20\d{2}|21\d{2})[-/](0?[1-9]|1[0-2])\b")
_NAMED_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) +
    r")\.?\s+([0-3]?\d)(?:st|nd|rd|th)?[,]?\s+(19\d{2}|20\d{2}|21\d{2})\b",
    re.I,
)
_DMY_RE = re.compile(
    r"\b([0-3]?\d)(?:st|nd|rd|th)?\s+(" +
    "|".join(sorted(_MONTHS, key=len, reverse=True)) +
    r")\.?[,]?\s+(19\d{2}|20\d{2}|21\d{2})\b",
    re.I,
)
_PUBLISHED_RE = re.compile(
    r"\b(?:published|publication date|posted|released|issue date|date of publication)"
    r"\s*(?:on|:|-|was)?\s*",
    re.I,
)
_UPDATED_RE = re.compile(
    r"\b(?:last updated|updated|modified|last revised|revised on|last modified)"
    r"\s*(?:on|:|-|was)?\s*",
    re.I,
)
_EFFECTIVE_RE = re.compile(
    r"\b(?:effective(?:\s+date)?|comes?\s+into\s+force|come into force|"
    r"shall\s+come\s+into\s+force|will\s+take\s+effect|takes?\s+effect|"
    r"effective\s+from|with\s+effect\s+from)\s*(?:on|from|:|-)?\s*",
    re.I,
)


def _valid_date(year: str | int, month: str | int, day: str | int = 1) -> str | None:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except (TypeError, ValueError):
        return None


def parse_date(value: Any) -> str | None:
    """Parse a date-like value to ISO date, preserving month-only precision."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _ISO_RE.search(text)
    if match:
        return _valid_date(*match.groups())
    match = _NAMED_RE.search(text)
    if match:
        month = _MONTHS.get(match.group(1).lower().rstrip("."))
        return _valid_date(match.group(3), month, match.group(2)) if month else None
    match = _DMY_RE.search(text)
    if match:
        month = _MONTHS.get(match.group(2).lower().rstrip("."))
        return _valid_date(match.group(3), month, match.group(1)) if month else None
    match = _YM_RE.search(text)
    if match:
        return _valid_date(match.group(1), match.group(2))
    return None


def _labelled_date(text: str, label_re: re.Pattern[str]) -> str | None:
    for match in label_re.finditer(text[:30000]):
        candidate = text[match.end():match.end() + 100]
        parsed = parse_date(candidate)
        if parsed:
            return parsed
    return None


def extract_temporal_metadata(
    title: str,
    url: str,
    content: str,
    *,
    search_date: Any = None,
    search_published: Any = None,
    search_updated: Any = None,
    discovered_at: str | None = None,
) -> dict[str, Any]:
    """Extract temporal metadata with explicit evidence basis; never conflate dates."""
    content = str(content or "")
    title = str(title or "")
    page_text = title + "\n" + content
    publication = parse_date(search_published) or parse_date(search_date)
    publication_basis = (
        "search_result_published_field" if parse_date(search_published)
        else "search_result_date_field" if parse_date(search_date)
        else ""
    )
    if not publication:
        publication = _labelled_date(page_text, _PUBLISHED_RE)
        if publication:
            publication_basis = "labelled_page_text"

    updated = parse_date(search_updated)
    updated_basis = "search_result_updated_field" if updated else ""
    if not updated:
        updated = _labelled_date(page_text, _UPDATED_RE)
        if updated:
            updated_basis = "labelled_page_text"

    effective = _labelled_date(page_text, _EFFECTIVE_RE)
    effective_basis = "labelled_effective_date_text" if effective else ""

    observed = discovered_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "publication_date": publication,
        "publication_date_basis": publication_basis or "unknown",
        "updated_date": updated,
        "updated_date_basis": updated_basis or "unknown",
        "effective_date": effective,
        "effective_date_basis": effective_basis or "unknown",
        "discovered_at": observed,
        "date_precision": {
            field: ("day" if value and len(value) == 10 else "month" if value else "unknown")
            for field, value in (
                ("publication_date", publication),
                ("updated_date", updated),
                ("effective_date", effective),
            )
        },
    }


def build_temporal_timeline(
    sources: Iterable[dict[str, Any]],
    extracted: Iterable[dict[str, Any]],
    search_results: Iterable[dict[str, Any]] = (),
    *,
    max_sources: int = 100,
) -> dict[str, Any]:
    """Create a source-centric timeline and distinguish event-effective dates."""
    results_by_url: dict[str, dict[str, Any]] = {}
    for result in search_results:
        url = normalize_url(str(result.get("canonical_url") or result.get("href") or result.get("url") or ""))
        if url:
            results_by_url[url] = dict(result)
    content_by_url: dict[str, str] = {}
    for item in extracted:
        url = normalize_url(str(item.get("url") or item.get("href") or ""))
        if url and item.get("content"):
            content_by_url.setdefault(url, str(item.get("content") or ""))
    profiles: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sources:
        url = normalize_url(str(item.get("url") or item.get("href") or ""))
        if not url or url in seen:
            continue
        seen.add(url)
        result = results_by_url.get(url, {})
        content = content_by_url.get(url, "")
        title = str(item.get("title") or result.get("title") or "")
        host = str(item.get("host") or result.get("host") or urlsplit(url).hostname or "")
        source_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        metadata = extract_temporal_metadata(
            title,
            url,
            content,
            search_date=item.get("date") or result.get("date"),
            search_published=item.get("published") or result.get("published"),
            search_updated=item.get("updated") or result.get("updated"),
        )
        profile = {
            "source_id": source_id,
            "url": url,
            "title": title,
            "host": host,
            **metadata,
        }
        profiles.append(profile)
        for field, event_type, basis_key in (
            ("publication_date", "publication", "publication_date_basis"),
            ("effective_date", "effective", "effective_date_basis"),
            ("updated_date", "updated", "updated_date_basis"),
        ):
            value = metadata.get(field)
            if value:
                events.append({
                    "event_id": hashlib.sha256(f"{source_id}|{event_type}|{value}".encode()).hexdigest()[:16],
                    "source_id": source_id,
                    "url": url,
                    "title": title,
                    "host": host,
                    "date": value,
                    "date_type": event_type,
                    "date_basis": metadata.get(basis_key),
                    "precision": metadata["date_precision"].get(field),
                })
        if len(profiles) >= max(1, max_sources):
            break
    events.sort(key=lambda item: (item["date"], item["date_type"], item["host"], item["url"]))
    counts = {
        "sources_with_publication_date": sum(bool(p["publication_date"]) for p in profiles),
        "sources_with_effective_date": sum(bool(p["effective_date"]) for p in profiles),
        "sources_with_updated_date": sum(bool(p["updated_date"]) for p in profiles),
    }
    return {
        "summary": {
            "source_count": len(profiles),
            "event_count": len(events),
            **counts,
            "date_fields_are_distinct": True,
            "inferred_dates_are_not_event_facts": True,
        },
        "sources": profiles,
        "events": events,
    }
