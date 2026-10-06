#!/usr/bin/env -S uv run python
"""web_search.py — SOTA web search infrastructure via ddgs 9.x.

PURE INFRASTRUCTURE: fetch results, eliminate rate limits, return clean
structured data. The AI agent does all cognition (analysis, fact extraction,
verification, synthesis, query crafting, decomposition).

Rate-limit ELIMINATION stack (highest priority first):
  1. DHT network cache  (--dht; canonical upstream fix; 90% faster repeats)
  2. Local TTL cache    (file-backed, atomic writes, negative cache)
  3. Single-flight      (coalesces concurrent identical queries → 1 fetch)
  4. Adaptive limiter   (AIMD: additive up, multiplicative down on rate-limit)
  5. Circuit breaker    (per-backend; auto-quarantines failing backends)
  6. Latency-aware select (prefers fast backends via p50/p99 tracking)
  7. Exponential backoff (+jitter; bounded; RatelimitException/TimeoutException)
  8. Backend rotation   (text category; shuffled for load distribution)
  9. Proxy support      (http/https/socks5; credentials auto-redacted)

Result hygiene (objective metadata, NOT analysis):
  canonicalize URLs → dedupe → domain-tier tag (T0 official > T1 news >
  T3 unclassified) → diversify (cap per-domain) → structured JSON output

The agent reads the JSON and does ALL analysis: fact extraction, cross-source
verification, answer synthesis, sentiment, query refinement, decomposition.

Config: ALL knobs env-driven (WEB_SEARCH_* prefix). No hardcoded values.

Output contract:
  stdout = ONE JSON document (schema-versioned; the payload the agent parses)
  stderr = structured logs (one JSON object per line)
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import heapq
import inspect
import ipaddress
import json
import logging
import atexit
import os
import random
import re
import gzip
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse
from urllib import request as urllib_request
from urllib import error as urllib_error
from urllib.robotparser import RobotFileParser

# --------------------------------------------------------------------------- #
# Skill-local venv auto-use (pipx-style re-exec)
#   If a .venv exists next to this script, re-exec with its python so the skill
#   is self-contained — works from any cwd without polluting project venvs.
#   Set WEB_SEARCH_NO_SKILL_VENV=1 to disable (force cwd's python).
# --------------------------------------------------------------------------- #
# Hard deps — single source of truth (hoisted ABOVE the __main__ bootstrap so
# the venv auto-installer and every hint derive from ONE list; the 2026-08-21
# audit's F-01 was exactly this class of drift).
REQUIRED_DEPS: tuple[tuple[str, str], ...] = (
    ("ddgs", "ddgs"),
    ("curl_cffi", "curl_cffi"),
    ("playwright", "playwright"),
    ("trafilatura", "trafilatura"),
    ("crawl4ai", "crawl4ai"),
    ("bs4", "beautifulsoup4"),
    ("fastembed", "fastembed"),
    ("readability", "readability-lxml"),
    ("html2text", "html2text"),
    ("rank_bm25", "rank-bm25"),
    ("htmldate", "htmldate"),
)
_REQUIRED_PIP_PACKAGES = " ".join(pip for _mod, pip in REQUIRED_DEPS)

# --------------------------------------------------------------------------- #
if __name__ == "__main__" and not os.environ.get("WEB_SEARCH_NO_SKILL_VENV"):
    _SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
    _SKILL_VENV = os.path.join(_SKILL_DIR, ".venv")
    _SKILL_VENV_PY = os.path.join(_SKILL_VENV, "bin", "python")

    # Auto-bootstrap: if skill venv missing + not disabled, create + install on first run.
    # One-time setup → zero hassle on every subsequent invocation.
    if not os.path.exists(_SKILL_VENV_PY) and not os.environ.get(
        "WEB_SEARCH_NO_BOOTSTRAP"
    ):
        import subprocess

        try:
            sys.stderr.write("Bootstrapping skill venv (one-time setup)...\n")
            subprocess.run(
                ["uv", "venv", _SKILL_VENV, "--python", "3.13"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    _SKILL_VENV_PY,
                ]
                + _REQUIRED_PIP_PACKAGES.split(),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [_SKILL_VENV_PY, "-m", "playwright", "install", "chromium"],
                check=True,
                capture_output=True,
            )
            sys.stderr.write("Skill venv ready.\n")
        except (subprocess.CalledProcessError, FileNotFoundError) as ex:
            sys.stderr.write(
                f"Failed to auto-bootstrap skill venv: {ex}\n"
                "Create manually:\n"
                f"  uv venv {_SKILL_VENV} --python 3.13\n"
                f"  uv pip install --python {_SKILL_VENV_PY} "
                f"{_REQUIRED_PIP_PACKAGES}\n"
                f"  {_SKILL_VENV_PY} -m playwright install chromium\n"
                "Or set WEB_SEARCH_NO_BOOTSTRAP=1 + WEB_SEARCH_NO_SKILL_VENV=1 "
                "to use cwd venv.\n"
            )
            sys.exit(2)

    # Re-exec into skill venv (compare sys.prefix — uv venvs symlink same base python)
    if os.path.exists(_SKILL_VENV_PY) and os.path.normpath(
        sys.prefix
    ) != os.path.normpath(_SKILL_VENV):
        os.environ["WEB_SEARCH_NO_SKILL_VENV"] = "1"  # prevent recursion
        os.execv(
            _SKILL_VENV_PY, [_SKILL_VENV_PY, os.path.abspath(__file__)] + sys.argv[1:]
        )

# --------------------------------------------------------------------------- #
# Config: ALL knobs env-driven.
# --------------------------------------------------------------------------- #


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Hard deps — fail fast if missing. No degraded fallbacks, no AVAILABLE flags.
# Per AGENTS.md: lazy import where heavy is OK; silent swallowing is not.
# --------------------------------------------------------------------------- #

# All deps are HARD — fail fast if missing (see REQUIRED_DEPS above — single
# source). No degraded fallbacks, no AVAILABLE flags (per AGENTS.md).


def _install_hint(skill_dir: str) -> str:
    """Derived install hint — shared by the bootstrap failure path and the
    dep-check FATAL message so the two can never diverge again."""
    return (
        f"  uv venv {skill_dir}/.venv --python 3.13\n"
        f"  uv pip install --python {skill_dir}/.venv/bin/python "
        f"{_REQUIRED_PIP_PACKAGES}\n"
        f"  {skill_dir}/.venv/bin/python -m playwright install chromium\n"
    )


def _check_required_deps() -> None:
    """Verify all hard deps present. Exit 2 with install hint if any missing."""
    missing = []
    for mod, _pip in REQUIRED_DEPS:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        _skill_dir = os.path.dirname(os.path.abspath(__file__))
        sys.stderr.write(
            f"FATAL: missing required deps: {', '.join(missing)}\n"
            "Install in skill venv (recommended — auto-used via re-exec):\n"
            f"{_install_hint(_skill_dir)}\n"
        )
        sys.exit(2)


# --------------------------------------------------------------------------- #
# Error model — vendor-agnostic exception hierarchy.
# DDGS/curl_cffi/Playwright exceptions are TRANSLATED into these at the
# adapter boundary so the core never knows which transport threw. This
# decouples resilience/retry/SSRF logic from any specific vendor library.
# --------------------------------------------------------------------------- #


class SearchError(Exception):
    """Base for all web_search errors."""


class BackendUnavailable(SearchError):
    """Search backend library missing or failed to import."""


class RateLimited(SearchError):
    """Backend returned 429 / rate-limit signal (transient)."""


class FetchTimeout(SearchError):
    """Network fetch exceeded the deadline (transient)."""


class FetchBlocked(SearchError):
    """Bot-block exhausted every transport layer (Cloudflare etc.)."""


class InvalidURL(SearchError):
    """Malformed URL or disallowed scheme."""


class SSRFBlocked(SearchError):
    """URL resolves to a forbidden target (private/loopback/link-local/
    loopback/multicast/metadata endpoint). Includes DNS-rebinding rejects
    (post-redirect IP re-validation)."""

    def __init__(
        self, url: str, reason: str, resolved_ips: list[str] | None = None
    ) -> None:
        self.url = url
        self.reason = reason
        self.resolved_ips = resolved_ips or []
        detail = f"{reason} @ {url}"
        if resolved_ips:
            detail += f" (resolved: {', '.join(resolved_ips)})"
        super().__init__(detail)


class BudgetExceeded(SearchError):
    """A byte/char/page/time budget was exhausted — guards against memory
    exhaustion (decompression bombs, pathological HTML, runaway crawls)."""

    def __init__(
        self, budget: str, limit: int, actual: int | None = None, url: str | None = None
    ) -> None:
        self.budget = budget
        self.limit = limit
        self.actual = actual
        self.url = url
        detail = f"{budget} exceeded: limit={limit}"
        if actual is not None:
            detail += f", actual={actual}"
        if url:
            detail += f" @ {url}"
        super().__init__(detail)


class BinaryDocument(SearchError):
    """Non-HTML content (PDF/DOCX/etc.) at URL — extractors cannot parse.
    Caller (AI agent) should download + route to the pdf-ocr skill."""

    def __init__(self, url: str, content_type: str) -> None:
        self.url = url
        self.content_type = content_type
        super().__init__(f"binary content ({content_type}) at {url}")


class ExtractionFailed(SearchError):
    """All extractors (trafilatura + crawl4ai) returned empty for an HTML
    page that was successfully fetched — signals a parser gap, not a fetch
    failure."""


# --------------------------------------------------------------------------- #
# Boundary domain types — typed models at system edges (P2.1).
#   These define the INPUT (SearchRequest) and OUTPUT (SearchResponse +
#   SearchResult + Attempt) contracts. The internal pipeline still uses
#   dict[str, Any] (unchanged); these types are available for new code, tests,
#   and a future REST API surface. Frozen → immutable, hashable, asdict-able.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SearchRequest:
    """Typed input for a search operation. CLI args parse into this; tests
    construct it directly. Maps to the existing _search_with_defense kwargs."""

    query: str
    category: str  # "text" | "news"
    region: str = "us-en"
    safesearch: str = "moderate"
    timelimit: str | None = None
    max_results: int = 10
    page: int = 1
    backend: str = "auto"
    sort: str = "latest"
    extract_top_n: int = 0  # 0 = no extraction
    max_chars: int = 0  # 0 = unlimited


@dataclass(frozen=True)
class Attempt:
    """One backend attempt within a search call. Surfaces in meta.attempts[]."""

    backend: str
    ok: bool
    latency_s: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "ok": self.ok,
            "latency_s": self.latency_s,
            "error": self.error,
        }


@dataclass(frozen=True)
class SearchResult:
    """One search hit. Matches the existing JSON results[] schema field-for-field."""

    title: str
    href: str
    body: str = ""
    canonical_url: str = ""
    host: str = ""
    tier: int = 3
    rank: int = 0
    date: str = ""
    age_days: int | None = None
    language: str = ""
    relevance_score: int = 0
    bm25_score: float = 0.0
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "href": self.href,
            "body": self.body,
            "canonical_url": self.canonical_url or self.href,
            "host": self.host,
            "tier": self.tier,
            "rank": self.rank,
            "date": self.date,
            "age_days": self.age_days,
            "language": self.language,
            "relevance_score": self.relevance_score,
            "bm25_score": self.bm25_score,
            "source": self.source,
        }


@dataclass(frozen=True)
class SearchResponse:
    """Full search output. Matches the existing JSON top-level schema."""

    query: str
    category: str
    results: tuple[SearchResult, ...] = ()
    citations: tuple[dict[str, Any], ...] = ()
    cache_status: str = "miss"
    served_from: str = "live"
    attempts: tuple[Attempt, ...] = ()
    backends_tried: tuple[str, ...] = ()
    rate_limited: bool = False
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": OUTPUT_SCHEMA_VERSION,
            "query": self.query,
            "category": self.category,
            "results": [r.to_dict() for r in self.results],
            "citations": list(self.citations),
            "meta": {
                "cache_status": self.cache_status,
                "served_from": self.served_from,
                "attempts": [a.to_dict() for a in self.attempts],
                "backends_tried": list(self.backends_tried),
                "rate_limited": self.rate_limited,
                "timed_out": self.timed_out,
            },
        }


# --------------------------------------------------------------------------- #
# Settings — structured, validated config snapshot from env.
#   Single source of truth. DEFAULT_* module constants below are thin aliases
#   of _settings fields, so existing call sites work unchanged. Tests can
#   construct Settings with overrides via env vars before from_env().
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SearchSettings:
    region: str
    safesearch: str
    max_results: int
    backend: str
    timeout: int
    proxy: str | None
    max_retries: int
    backoff_base: float
    backoff_cap: float
    backoff_jitter: float


@dataclass(frozen=True)
class RateSettings:
    initial: float
    burst: int
    cap: float
    floor: float
    increase: float
    decrease: float


@dataclass(frozen=True)
class CircuitSettings:
    failure_threshold: int
    cooldown: float


@dataclass(frozen=True)
class CacheSettings:
    ttl: int
    neg_ttl: int
    dir: Path
    enabled: bool


@dataclass(frozen=True)
class SecuritySettings:
    max_response_bytes: int
    max_html_bytes: int
    max_markdown_bytes: int
    max_document_chars: int
    max_crawl_bytes: int
    max_redirects: int
    max_links_per_page: int
    max_total_links: int
    max_browser_time_s: float


@dataclass(frozen=True)
class AgenticSettings:
    max_iter: int
    max_results_per_iter: int
    top_n_extract: int
    relevance_domains: int
    max_per_domain: int
    extract_chars: int
    convergence_new_min: int
    convergence_window: int


@dataclass(frozen=True)
class LlmSettings:
    """Local-LLM config for `pro` mode (Perplexity-style agentic research).
    All values env-driven — NO hardcoded model names. `pro` works without a
    model (heuristic path); --use-llm opts into the LLM path."""

    model: str  # generation model (WEB_SEARCH_LLM_MODEL)
    embed_model: str  # embedding model for semantic coverage (WEB_SEARCH_EMBED_MODEL)
    ollama_host: (
        str  # LLM REST base — any runtime (WEB_SEARCH_LLM_HOST alias honored below)
    )
    max_tokens: int  # response cap per call (WEB_SEARCH_LLM_MAX_TOKENS)
    timeout: float  # per-request timeout s (WEB_SEARCH_LLM_TIMEOUT)
    think_default: bool  # enable chain-of-thought by default (WEB_SEARCH_LLM_THINK)
    provider: str = "auto"  # auto | ollama | openai (WEB_SEARCH_LLM_PROVIDER)
    light_model: str = ""  # small model for routing stages (WEB_SEARCH_LLM_LIGHT_MODEL; empty = auto-pick)


# WS8: rate/politeness presets. WEB_SEARCH_PROFILE selects the DEFAULT for
# each tuning knob; an explicit WEB_SEARCH_* env var still wins (existing
# precedence unchanged). "standard" is byte-identical to the pre-preset
# literals (pinned by test) — profiles only reshape defaults, never override
# explicit user intent.
_PROFILE_PRESETS: dict[str, dict[str, float]] = {
    # Polite single-host crawls and strict hosts (Google): slow ramp, hard
    # back-off, short fuse on failures.
    "polite": {
        "rate_initial": 0.5,
        "rate_cap": 1.0,
        "rate_floor": 0.05,
        "rate_increase": 0.05,
        "max_retries": 3,
        "backoff_base": 3.0,
        "backoff_cap": 60.0,
        "cb_failure_threshold": 2,
        "cb_cooldown": 120.0,
    },
    # Byte-exact match of the historic hardcoded defaults.
    "standard": {
        "rate_initial": 2.0,
        "rate_cap": 10.0,
        "rate_floor": 0.2,
        "rate_increase": 0.2,
        "max_retries": 4,
        "backoff_base": 1.5,
        "backoff_cap": 30.0,
        "cb_failure_threshold": 3,
        "cb_cooldown": 60.0,
    },
    # Batch coverage across many backends: hot start, higher ceiling,
    # tolerant circuit breaker.
    "aggressive": {
        "rate_initial": 5.0,
        "rate_cap": 20.0,
        "rate_floor": 0.5,
        "rate_increase": 0.5,
        "max_retries": 6,
        "backoff_base": 1.0,
        "backoff_cap": 15.0,
        "cb_failure_threshold": 5,
        "cb_cooldown": 20.0,
    },
}


@dataclass(frozen=True)
class Settings:
    """Top-level config snapshot. Frozen → immutable at runtime. Grouped by
    concern for readability + test injection."""

    search: SearchSettings
    rate: RateSettings
    circuit: CircuitSettings
    cache: CacheSettings
    security: SecuritySettings
    agentic: AgenticSettings
    llm: LlmSettings
    call_budget: float
    concurrency: int
    ddgs_threads: int
    max_per_domain: int
    extract_chars: int
    dht: bool
    dht_api_url: str
    log_level: str
    output_schema_version: str

    @classmethod
    def from_env(cls) -> "Settings":
        """Read all WEB_SEARCH_* env vars in one place. Single source of truth
        — the DEFAULT_* aliases below read from the resulting instance."""
        # WS8: profile resolves FIRST and only rewrites knob defaults; env
        # vars below still take precedence per-knob. Invalid profile fails
        # fast at startup listing the valid values (§5 H1 — no silent fall
        # back to standard on a typo).
        profile = os.environ.get("WEB_SEARCH_PROFILE", "standard").strip().lower()
        if profile not in _PROFILE_PRESETS:
            raise ValueError(
                f"WEB_SEARCH_PROFILE={profile!r} is invalid. "
                f"Valid profiles: {', '.join(sorted(_PROFILE_PRESETS))}."
            )
        preset = _PROFILE_PRESETS[profile]
        proxy = os.environ.get("WEB_SEARCH_PROXY") or os.environ.get("DDGS_PROXY")
        cache_dir = Path(
            _env("WEB_SEARCH_CACHE_DIR", str(Path.home() / ".cache" / "web-search"))
        )
        return cls(
            search=SearchSettings(
                region=_env("WEB_SEARCH_REGION", "us-en"),
                safesearch=_env("WEB_SEARCH_SAFESEARCH", "moderate"),
                max_results=_env_int("WEB_SEARCH_MAX_RESULTS", 10),
                backend=_env("WEB_SEARCH_BACKEND", "auto"),
                timeout=_env_int("WEB_SEARCH_TIMEOUT", 10),
                proxy=proxy,
                max_retries=_env_int(
                    "WEB_SEARCH_MAX_RETRIES", int(preset["max_retries"])
                ),
                backoff_base=_env_float(
                    "WEB_SEARCH_BACKOFF_BASE", preset["backoff_base"]
                ),
                backoff_cap=_env_float("WEB_SEARCH_BACKOFF_CAP", preset["backoff_cap"]),
                backoff_jitter=_env_float("WEB_SEARCH_BACKOFF_JITTER", 0.3),
            ),
            rate=RateSettings(
                initial=_env_float("WEB_SEARCH_RATE_LIMIT", preset["rate_initial"]),
                burst=_env_int("WEB_SEARCH_RATE_BURST", 5),
                cap=_env_float("WEB_SEARCH_RATE_CAP", preset["rate_cap"]),
                floor=_env_float("WEB_SEARCH_RATE_FLOOR", preset["rate_floor"]),
                increase=_env_float(
                    "WEB_SEARCH_RATE_INCREASE", preset["rate_increase"]
                ),
                decrease=_env_float("WEB_SEARCH_RATE_DECREASE_FACTOR", 0.5),
            ),
            circuit=CircuitSettings(
                failure_threshold=_env_int(
                    "WEB_SEARCH_CB_FAILURE_THRESHOLD",
                    int(preset["cb_failure_threshold"]),
                ),
                cooldown=_env_float("WEB_SEARCH_CB_COOLDOWN", preset["cb_cooldown"]),
            ),
            cache=CacheSettings(
                ttl=_env_int("WEB_SEARCH_CACHE_TTL", 86400),
                neg_ttl=_env_int("WEB_SEARCH_NEG_CACHE_TTL", 1800),
                dir=cache_dir,
                enabled=not _env_bool("WEB_SEARCH_NO_CACHE", False),
            ),
            security=SecuritySettings(
                max_response_bytes=_env_int(
                    "WEB_SEARCH_MAX_RESPONSE_BYTES", 200_000_000
                ),
                max_html_bytes=_env_int("WEB_SEARCH_MAX_HTML_BYTES", 100_000_000),
                max_markdown_bytes=_env_int(
                    "WEB_SEARCH_MAX_MARKDOWN_BYTES", 50_000_000
                ),
                max_document_chars=_env_int(
                    "WEB_SEARCH_MAX_DOCUMENT_CHARS", 20_000_000
                ),
                max_crawl_bytes=_env_int("WEB_SEARCH_MAX_CRAWL_BYTES", 2_000_000_000),
                max_redirects=_env_int("WEB_SEARCH_MAX_REDIRECTS", 10),
                max_links_per_page=_env_int("WEB_SEARCH_MAX_LINKS_PER_PAGE", 500),
                max_total_links=_env_int("WEB_SEARCH_MAX_TOTAL_LINKS", 10_000),
                max_browser_time_s=_env_float("WEB_SEARCH_MAX_BROWSER_TIME_S", 60.0),
            ),
            agentic=AgenticSettings(
                max_iter=_env_int("WEB_SEARCH_AGENTIC_MAX_ITER", 8),
                max_results_per_iter=_env_int(
                    "WEB_SEARCH_AGENTIC_MAX_RESULTS_PER_ITER", 15
                ),
                top_n_extract=_env_int("WEB_SEARCH_AGENTIC_TOP_N_EXTRACT", 12),
                relevance_domains=_env_int("WEB_SEARCH_AGENTIC_RELEVANCE_DOMAINS", 5),
                max_per_domain=_env_int("WEB_SEARCH_AGENTIC_MAX_PER_DOMAIN", 3),
                extract_chars=_env_int("WEB_SEARCH_AGENTIC_EXTRACT_CHARS", 0),
                convergence_new_min=_env_int(
                    "WEB_SEARCH_AGENTIC_CONVERGENCE_NEW_MIN", 2
                ),
                convergence_window=_env_int("WEB_SEARCH_AGENTIC_CONVERGENCE_WINDOW", 2),
            ),
            llm=LlmSettings(
                model=_env("WEB_SEARCH_LLM_MODEL", "qwen3.5:9b"),
                embed_model=_env("WEB_SEARCH_EMBED_MODEL", "nomic-embed-text"),
                # WEB_SEARCH_LLM_HOST is the runtime-agnostic name;
                # WEB_SEARCH_OLLAMA_HOST kept for backward compat.
                ollama_host=_env(
                    "WEB_SEARCH_LLM_HOST",
                    _env("WEB_SEARCH_OLLAMA_HOST", "http://localhost:11434"),
                ),
                max_tokens=_env_int("WEB_SEARCH_LLM_MAX_TOKENS", 4096),
                timeout=_env_float("WEB_SEARCH_LLM_TIMEOUT", 120.0),
                think_default=_env_bool("WEB_SEARCH_LLM_THINK", True),
                provider=_env("WEB_SEARCH_LLM_PROVIDER", "auto").lower(),
                light_model=_env("WEB_SEARCH_LLM_LIGHT_MODEL", ""),
            ),
            call_budget=_env_float("WEB_SEARCH_CALL_BUDGET", 600.0),
            concurrency=_env_int("WEB_SEARCH_CONCURRENCY", 3),
            ddgs_threads=_env_int("WEB_SEARCH_DDGS_THREADS", 0),
            max_per_domain=_env_int("WEB_SEARCH_MAX_PER_DOMAIN", 3),
            extract_chars=_env_int("WEB_SEARCH_EXTRACT_CHARS", 0),
            dht=_env_bool("WEB_SEARCH_DHT", False),
            dht_api_url=_env("WEB_SEARCH_DHT_API_URL", "http://localhost:4479"),
            log_level=_env("WEB_SEARCH_LOG_LEVEL", "INFO").upper(),
            output_schema_version="2.0",
        )


_settings = Settings.from_env()


def get_settings() -> Settings:
    """Access the validated config snapshot. Frozen — immutable at runtime."""
    return _settings


# --------------------------------------------------------------------------- #
# DEFAULT_* aliases — backward-compatible module constants.
#   Source of truth is _settings (Settings.from_env()) above. These thin
#   aliases keep existing call sites working with zero migration. New code
#   should prefer `get_settings().<section>.<field>`.
# --------------------------------------------------------------------------- #

# Search
DEFAULT_REGION = _settings.search.region
DEFAULT_SAFESEARCH = _settings.search.safesearch
DEFAULT_MAX_RESULTS = _settings.search.max_results
DEFAULT_BACKEND = _settings.search.backend
DEFAULT_TIMEOUT = _settings.search.timeout
DEFAULT_PROXY = _settings.search.proxy

# Rate-limit defense
DEFAULT_MAX_RETRIES = _settings.search.max_retries
DEFAULT_BACKOFF_BASE = _settings.search.backoff_base
DEFAULT_BACKOFF_CAP = _settings.search.backoff_cap
DEFAULT_BACKOFF_JITTER = _settings.search.backoff_jitter

# Adaptive token bucket (AIMD)
DEFAULT_RATE_LIMIT = _settings.rate.initial
DEFAULT_RATE_BURST = _settings.rate.burst
DEFAULT_RATE_CAP = _settings.rate.cap
DEFAULT_RATE_FLOOR = _settings.rate.floor
DEFAULT_RATE_INCREASE = _settings.rate.increase
DEFAULT_RATE_DECREASE = _settings.rate.decrease

# Circuit breaker
DEFAULT_CB_FAILURE_THRESHOLD = _settings.circuit.failure_threshold
DEFAULT_CB_COOLDOWN = _settings.circuit.cooldown

# Budget
DEFAULT_CALL_BUDGET = _settings.call_budget

# Security budgets — guard against memory exhaustion (decompression bombs,
# pathological HTML, runaway crawls). All env-overridable. Defaults are
# generous for legitimate pages but bounded enough to prevent OOM kill.
DEFAULT_MAX_RESPONSE_BYTES = (
    _settings.security.max_response_bytes
)  # 200 MB per HTTP response
DEFAULT_MAX_HTML_BYTES = _settings.security.max_html_bytes  # 100 MB HTML body cap
DEFAULT_MAX_MARKDOWN_BYTES = (
    _settings.security.max_markdown_bytes
)  # 50 MB extracted markdown
DEFAULT_MAX_DOCUMENT_CHARS = (
    _settings.security.max_document_chars
)  # 20M chars post-extract
DEFAULT_MAX_CRAWL_BYTES = _settings.security.max_crawl_bytes  # 2 GB total per crawl
DEFAULT_MAX_REDIRECTS = _settings.security.max_redirects  # redirect hops
DEFAULT_MAX_LINKS_PER_PAGE = (
    _settings.security.max_links_per_page
)  # links extracted per page
DEFAULT_MAX_TOTAL_LINKS = _settings.security.max_total_links  # crawl frontier cap
DEFAULT_MAX_BROWSER_TIME_S = (
    _settings.security.max_browser_time_s
)  # Playwright nav deadline

# Cache
DEFAULT_CACHE_TTL = _settings.cache.ttl
DEFAULT_NEG_CACHE_TTL = _settings.cache.neg_ttl
DEFAULT_CACHE_DIR = _settings.cache.dir
DEFAULT_CACHE_ENABLED = _settings.cache.enabled

# F-48: informativeness floor for search results with extracted content.
# Same 200-char bar the scoring/passage paths already use; engines return
# boilerplate-only pages (nav footers) instead of true-zero on junk queries,
# which masked genuine-empty (exit 3) and polluted results with 146-char
# footer "articles".
DEFAULT_MIN_CONTENT_CHARS = _env_int("WEB_SEARCH_MIN_CONTENT_CHARS", 200)
# F-51/F-50 tuning: markdown-scaffolding ratio for the content floor and the
# slot-score multiplier applied to flagged (noise/discovery) results.
DEFAULT_FLOOR_LINK_RATIO = _env_float("WEB_SEARCH_FLOOR_LINK_RATIO", 0.5)
DEFAULT_NOISE_PENALTY = _env_float("WEB_SEARCH_NOISE_PENALTY", 0.3)
# F-59: citations[] duplicate results[] fields (url/title/host/tier/date/
# snippet). Agents that read results directly can set 0 to reclaim the bytes;
# default ON keeps the synthesis-friendly payload.
DEFAULT_EMIT_CITATIONS = _env_bool("WEB_SEARCH_EMIT_CITATIONS", True)
# F-63: AIMD rate + open-circuit cooldowns survive process restarts. Agents
# spawn a fresh CLI per question; without persistence every run re-learns
# rate from zero and re-trips soft-bans the previous run already paid for.
DEFAULT_PERSIST_STATE = _env_bool("WEB_SEARCH_PERSIST_STATE", True)
DEFAULT_STATE_TTL_S = _env_int("WEB_SEARCH_STATE_TTL_S", 1800)

# DHT
DEFAULT_DHT = _settings.dht
DEFAULT_DHT_API_URL = _settings.dht_api_url

# Batch / threads
DEFAULT_CONCURRENCY = _settings.concurrency
DEFAULT_DDGS_THREADS = _settings.ddgs_threads

# Agentic
DEFAULT_AGENTIC_MAX_ITER = _settings.agentic.max_iter
DEFAULT_AGENTIC_MAX_RESULTS_PER_ITER = _settings.agentic.max_results_per_iter
DEFAULT_AGENTIC_TOP_N_EXTRACT = _settings.agentic.top_n_extract
DEFAULT_AGENTIC_RELEVANCE_DOMAINS = _settings.agentic.relevance_domains
DEFAULT_AGENTIC_MAX_PER_DOMAIN = _settings.agentic.max_per_domain
DEFAULT_AGENTIC_EXTRACT_CHARS = _settings.agentic.extract_chars
# F1: convergence-based early-stop. Loop terminates after N consecutive
# iterations with n_new < CONVERGENCE_NEW_MIN. Replaces the old T0-host-count
# stop which was gameable by Yandex returning i18n spam (Hebrew Wikipedia,
# gov.in redirects) that scored relevance>0 on common keywords.
DEFAULT_AGENTIC_CONVERGENCE_NEW_MIN = _settings.agentic.convergence_new_min
DEFAULT_AGENTIC_CONVERGENCE_WINDOW = _settings.agentic.convergence_window

# Result post-processing
DEFAULT_MAX_PER_DOMAIN = _settings.max_per_domain
DEFAULT_EXTRACT_CHARS = _settings.extract_chars

# Log
DEFAULT_LOG_LEVEL = _settings.log_level

OUTPUT_SCHEMA_VERSION = _settings.output_schema_version

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _redact_proxy(proxy: str | None) -> str:
    if not proxy:
        return "<none>"
    if "://" in proxy and "@" in proxy:
        scheme, rest = proxy.split("://", 1)
        if "@" in rest:
            _creds, host = rest.rsplit("@", 1)
            return f"{scheme}://***@{host}"
    return proxy


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        skip = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "taskName",
        }
        for k, v in record.__dict__.items():
            if k not in skip:
                payload[k] = v
        return json.dumps(payload, default=str, ensure_ascii=False)


class _KwargsLogger(logging.Logger):
    """Logger accepting arbitrary kwargs → forwarded as extra fields."""

    def _log(  # type: ignore[override]
        self,
        level,
        msg,
        args,
        *,
        exc_info=None,
        extra=None,
        stack_info=False,
        stacklevel=1,
        **kwargs,
    ):
        merged = dict(extra or {})
        merged.update(kwargs)
        super()._log(
            level,
            msg,
            args,
            exc_info=exc_info,
            extra=merged or None,
            stack_info=stack_info,
            stacklevel=stacklevel,
        )


logging.setLoggerClass(_KwargsLogger)


def _setup_logger(level: str = DEFAULT_LOG_LEVEL) -> logging.Logger:
    lg = logging.getLogger("web_search")
    if lg.handlers:
        return lg
    lg.setLevel(getattr(logging, level, logging.INFO))
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    lg.addHandler(h)
    lg.propagate = False
    return lg


log = _setup_logger()

# --------------------------------------------------------------------------- #
# ddgs import
# --------------------------------------------------------------------------- #


# Module-level DDGS exception types — set once by _import_ddgs().
# Referenced directly by _do_search's retry loop via _ddgs_exc(). This kills the
# old anti-pattern of threading RatelimitException/TimeoutException/DDGSException
# as function PARAMETERS through ~34 call sites. _do_search translates them to
# SearchError subclasses (RateLimited/FetchTimeout/BackendUnavailable) at its
# output boundary so callers never see vendor types.
_DDGSException: type | None = None
_RatelimitException: type | None = None
_TimeoutException: type | None = None


def _import_ddgs() -> tuple:
    """Import ddgs + cache its exception types module-level. Returns the tuple
    (DDGS, DDGSException, RatelimitException, TimeoutException) for the ONE
    caller (main) that still unpacks it; everything else uses the globals."""
    global _DDGSException, _RatelimitException, _TimeoutException
    try:
        from ddgs import DDGS
        from ddgs.exceptions import (
            DDGSException,
            RatelimitException,
            TimeoutException,
        )
    except ImportError as ex:
        sys.stderr.write(
            json.dumps(
                {
                    "ts": round(time.time(), 3),
                    "level": "ERROR",
                    "msg": "ddgs library not importable",
                    "error": str(ex),
                    "fix": "Run `uv add ddgs` in your project venv.",
                }
            )
            + "\n"
        )
        sys.exit(2)
    _DDGSException = DDGSException
    _RatelimitException = RatelimitException
    _TimeoutException = TimeoutException
    return DDGS, DDGSException, RatelimitException, TimeoutException


# --------------------------------------------------------------------------- #
# URL canonicalization + domain tiering (objective metadata)
# --------------------------------------------------------------------------- #

_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "fbclid",
        "gclid",
        "msclkid",
        "mc_eid",
        "mc_cid",
        "_hsenc",
        "_hsmi",
        "igshid",
        "ref",
        "ref_src",
        "ref_url",
        "feature",
        "sr_share",
        "vero_id",
        "wickedid",
        "yclid",
        "ocid",
    }
)


def _parse_domain_list(env_var: str) -> frozenset[str]:
    """Parse a comma-separated domain list from env. Empty by default —
    NO hardcoded domain bias. Users configure authoritative sources for
    THEIR domain via WEB_SEARCH_TIER0_DOMAINS / WEB_SEARCH_TIER1_DOMAINS.
    Example: WEB_SEARCH_TIER0_DOMAINS=nih.gov,pubmed.ncbi.nlm.nih.gov,who.int"""
    raw = os.environ.get(env_var, "")
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())


# T0/T1 are USER-CONFIGURED — empty by default (all domains equal, no bias).
# The generic TLD heuristics in domain_tier() (.gov/.edu/.mil/.ac.) still
# apply structurally — those aren't domain-specific.
_T0_DOMAINS = _parse_domain_list("WEB_SEARCH_TIER0_DOMAINS")
_T1_DOMAINS = _parse_domain_list("WEB_SEARCH_TIER1_DOMAINS")


# --------------------------------------------------------------------------- #
# SSRF / egress security — SafeURLResolver
#   Validates that a URL's resolved IPs are NOT in forbidden ranges before
#   any network fetch. MUST be re-invoked after every redirect hop (DNS
#   rebinding: attacker.com can 302 → 169.254.169.254).
#   Pure stdlib (ipaddress + socket); zero new deps.
# --------------------------------------------------------------------------- #

# Networks that must NEVER be an egress target. Covers RFC1918 private space,
# loopback, link-local (includes ALL cloud metadata at 169.254.0.0/16), CGNAT
# (includes Alibaba metadata at 100.100.100.200), TEST-NETs, ULA, etc.
_FORBIDDEN_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),  # "this network"
    ipaddress.ip_network("10.0.0.0/8"),  # RFC1918 private
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT + Alibaba metadata (100.100.100.200)
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network(
        "169.254.0.0/16"
    ),  # link-local + AWS/GCP/Azure/Oracle metadata
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918 private
    ipaddress.ip_network("192.0.0.0/24"),  # IANA protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),  # RFC1918 private
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network(
        "fc00::/7"
    ),  # IPv6 unique-local (incl. AWS fd00:ec2::254 metadata)
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
)

# Cloud-metadata hosts that must always be blocked even if a trusted-hosts
# list attempted to allowlist them (defense in depth).
_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS/GCP/Azure/Oracle/DigitalOcean
        "100.100.100.200",  # Alibaba
        "metadata.google.internal",
        "metadata.azure.com",
    }
)

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


@dataclass(frozen=True)
class ResolvedTarget:
    """Result of a validated URL — the actual connection target after DNS
    resolution + SSRF policy check. Carries resolved IPs so redirect loops
    can re-validate without a redundant DNS lookup."""

    original_url: str
    final_url: str
    scheme: str
    host: str
    port: int
    resolved_ips: tuple[str, ...]
    is_trusted: bool


class SafeURLResolver:
    """Validates that a URL's resolved IPs are not in forbidden ranges.

    Prevents SSRF and DNS-rebinding. Idempotent and thread-safe. Call
    `resolve_and_validate` on the initial URL AND after every redirect hop.
    """

    def __init__(
        self,
        trusted_hosts: set[str] | None = None,
        dns_ttl: float = 60.0,
        resolve_timeout: float = 5.0,
    ) -> None:
        self._trusted = {
            h.lower()
            for h in (trusted_hosts or set()) - _METADATA_HOSTS
            for h in [h.lower()]
            if h
        }
        from collections import OrderedDict

        self._dns_cache: OrderedDict[str, tuple[float, tuple[str, ...]]] = OrderedDict()
        self._dns_lock = threading.Lock()
        self._dns_ttl = dns_ttl
        self._resolve_timeout = resolve_timeout
        self._dns_cache_max = 2048  # bounded (F-34): FIFO-evict beyond this

    def resolve_and_validate(self, url: str) -> ResolvedTarget:
        """Parse + DNS-resolve + IP-policy-check a URL. Raises InvalidURL for
        malformed/disallowed-scheme URLs, SSRFBlocked for forbidden targets."""
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise InvalidURL(url, f"scheme '{scheme}' not allowed (http/https only)")
        host = (parsed.hostname or "").lower()
        if not host:
            raise InvalidURL(url, "no hostname")
        if host in _METADATA_HOSTS:
            raise SSRFBlocked(url, "cloud-metadata hostname", [host])
        port = parsed.port or (443 if scheme == "https" else 80)
        is_trusted = host in self._trusted
        ips = self._resolve(host)
        if not ips:
            raise SearchError(f"DNS resolution returned no addresses for {host}")
        if not is_trusted:
            for ip in ips:
                blocked, reason = _check_ip_forbidden(ip)
                if blocked:
                    raise SSRFBlocked(url, reason, list(ips))
        return ResolvedTarget(
            original_url=url,
            final_url=url,
            scheme=scheme,
            host=host,
            port=port,
            resolved_ips=ips,
            is_trusted=is_trusted,
        )

    def validate_redirect_target(self, new_url: str) -> ResolvedTarget:
        """Re-validate a URL surfaced by a Location header. Same policy as
        resolve_and_validate — factored so the redirect-loop code reads clearly."""
        return self.resolve_and_validate(new_url)

    def _resolve(self, host: str) -> tuple[str, ...]:
        """Resolve host → tuple of IP strings. Short-TTL cache to avoid
        repeated lookups during a multi-page crawl. Returns () on failure
        or timeout.

        F-10: getaddrinfo runs in a daemon helper thread with the configured
        timeout — a hung resolver can no longer block every fetch thread
        forever. Cache access is lock-guarded (not the lookup itself)."""
        now = time.monotonic()
        with self._dns_lock:
            cached = self._dns_cache.get(host)
        if cached and (now - cached[0]) < self._dns_ttl:
            return cached[1]
        # getaddrinfo handles both hostnames and IP literals uniformly.
        infos: list | None = None

        def _lookup() -> None:
            nonlocal infos
            try:
                infos = socket.getaddrinfo(host, None)
            except socket.gaierror:
                infos = []

        # Daemon thread + join(timeout): unlike ThreadPoolExecutor (whose
        # with-exit JOINS the worker and would block on a hung getaddrinfo),
        # a daemon thread is abandoned on timeout and dies with the process.
        t = threading.Thread(target=_lookup, daemon=True)
        t.start()
        t.join(timeout=self._resolve_timeout)
        if t.is_alive():
            log.warning(
                "dns_resolve_timeout", host=host[:80], timeout=self._resolve_timeout
            )
            return ()
        if infos is None:
            return ()
        seen: list[str] = []
        for info in infos:
            ip = info[4][0]
            if ip not in seen:
                seen.append(ip)
        ips = tuple(seen)
        with self._dns_lock:
            self._dns_cache[host] = (now, ips)
            self._dns_cache.move_to_end(host)
            while len(self._dns_cache) > self._dns_cache_max:
                self._dns_cache.popitem(last=False)
        return ips


def _check_ip_forbidden(ip: str) -> tuple[bool, str]:
    """Returns (blocked, reason). Blocked=True means the IP is in a forbidden
    range or is a known metadata endpoint. Handles IPv4-mapped IPv6
    (::ffff:127.0.0.1) which would otherwise bypass IPv4 network checks."""
    if ip in _METADATA_HOSTS:
        return True, f"cloud-metadata IP {ip}"
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True, f"invalid IP literal '{ip}'"
    # Check the address as-is against ALL networks (IPv4 and IPv6).
    for net in _FORBIDDEN_NETWORKS:
        if addr in net:
            return True, f"IP {ip} in forbidden range {net}"
    # IPv4-mapped IPv6 bypass guard: ::ffff:127.0.0.1 is an IPv6Address that
    # maps to 127.0.0.1 — the check above misses it (IPv6 not in IPv4 net).
    # Extract the mapped IPv4 and check it against the forbidden networks too.
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped
        if mapped is not None:
            for net in _FORBIDDEN_NETWORKS:
                if mapped in net:
                    return True, f"IPv4-mapped IPv6 {ip} → {mapped} in {net}"
    return False, ""


_resolver_instance: SafeURLResolver | None = None
_resolver_lock = threading.Lock()


def get_safe_url_resolver() -> SafeURLResolver:
    """Lazy singleton — trusted hosts from WEB_SEARCH_TRUSTED_HOSTS env
    (comma-separated). Metadata hosts always blocked regardless."""
    global _resolver_instance
    if _resolver_instance is None:
        with _resolver_lock:
            if _resolver_instance is None:
                raw = os.environ.get("WEB_SEARCH_TRUSTED_HOSTS", "")
                trusted = {h.strip().lower() for h in raw.split(",") if h.strip()}
                _resolver_instance = SafeURLResolver(trusted_hosts=trusted)
    return _resolver_instance


def canonicalize_url(url: str) -> str:
    """Strip tracking params, normalize scheme/host. Dedup key + clean output."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        scheme = "https" if p.scheme in ("http", "https") else (p.scheme or "https")
        host = _strip_host_prefix((p.netloc or "").lower())
        qs = [
            (k, v)
            for k, v in parse_qsl(p.query, keep_blank_values=False)
            if k.lower() not in _TRACKING_PARAMS
        ]
        qs.sort()
        path = p.path.rstrip("/") or "/"
        return urlunparse((scheme, host, path, "", urlencode(qs), ""))
    except (ValueError, TypeError):
        return url


def _strip_host_prefix(host: str) -> str:
    """Strip leading www. / m. / mobile. prefixes (NOT character-set lstrip)."""
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            return host[len(prefix) :]
    return host


def _host_of(url: str) -> str:
    if not url:
        return ""
    try:
        netloc = urlparse(url if "://" in url else "https://" + url).netloc.lower()
        return _strip_host_prefix(netloc).split(":")[0]
    except (ValueError, TypeError):
        return ""


def domain_tier(host: str) -> int:
    """Authority tier: T0 (government/academic/official) > T1 (user-configured
    major sources) > T3 (unclassified).

    T0/T1 lists are USER-CONFIGURED via env vars (empty by default = no bias):
      WEB_SEARCH_TIER0_DOMAINS=nih.gov,pubmed.ncbi.nlm.nih.gov,who.int
      WEB_SEARCH_TIER1_DOMAINS=reuters.com,nature.com,arxiv.org

    Generic TLD heuristics (.gov/.edu/.mil/.ac.) apply structurally regardless
    of config — these aren't domain-specific."""
    if not host:
        return 3
    h = _strip_host_prefix(host.lower())
    for d in _T0_DOMAINS:
        if h == d or h.endswith("." + d):
            return 0
    for d in _T1_DOMAINS:
        if h == d or h.endswith("." + d):
            return 1
    if h.endswith((".gov", ".edu", ".mil")):
        return 0
    if ".gov." in h or ".edu." in h or ".ac." in h:
        return 0
    return 3


# --------------------------------------------------------------------------- #
# Adaptive token bucket (AIMD rate limiter — self-tuning)
# --------------------------------------------------------------------------- #


class AdaptiveTokenBucket:
    """Self-tuning rate limiter. AIMD: additive increase on success,
    multiplicative decrease on rate-limit. Eliminates manual tuning."""

    def __init__(
        self, initial_rate, burst, rate_cap, rate_floor, increase_step, decrease_factor
    ):
        self.rate = max(rate_floor, min(rate_cap, initial_rate))
        self.burst = max(1, burst)
        self.rate_cap = rate_cap
        self.rate_floor = rate_floor
        self.increase_step = increase_step
        self.decrease_factor = decrease_factor
        self.tokens = float(self.burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, tokens=1.0, timeout=None):
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(
                    self.burst, self.tokens + (now - self.last) * self.rate
                )
                self.last = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True
                wait = (tokens - self.tokens) / max(self.rate_floor, self.rate)
            if deadline is not None and time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 0.5))

    def on_success(self):
        with self.lock:
            self.rate = min(self.rate_cap, self.rate + self.increase_step)

    def on_rate_limit(self):
        with self.lock:
            self.rate = max(self.rate_floor, self.rate * self.decrease_factor)

    def snapshot(self):
        with self.lock:
            return {
                "current_rate": round(self.rate, 2),
                "burst": self.burst,
                "cap": self.rate_cap,
                "floor": self.rate_floor,
            }


# --------------------------------------------------------------------------- #
# Runtime-state persistence (F-63): AIMD rate + open circuits across restarts
# --------------------------------------------------------------------------- #


def _runtime_state_path() -> Path:
    return DEFAULT_CACHE_DIR / "state.json"


def _save_runtime_state() -> None:
    """Snapshot the learned rate + open circuits to disk (atomic write).

    Circuit cooldowns are monotonic-timestamped internally; persisted as
    REMAINING seconds and re-armed relative to restore time."""
    if not DEFAULT_PERSIST_STATE:
        return
    try:
        cb = _get_cb()
        circuits = {
            key: round(float(item.get("open_for_s", 0)), 1)
            for key, item in cb.snapshot().items()
            if item.get("state") == "OPEN" and item.get("open_for_s", 0) > 0
        }
        payload = {
            "schema": 1,
            "saved_at": time.time(),
            "rate": round(_get_rate_limiter().rate, 3),
            "circuits": circuits,
        }
        DEFAULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _runtime_state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _runtime_state_path())
    except OSError as ex:
        log.debug("state_save_failed", error=str(ex))


def _restore_runtime_state() -> None:
    """Reload persisted runtime state if fresh enough (TTL-guarded).

    Corrupt or unreadable state degrades silently to a fresh start - a lost
    throttle memory must never become a crash (§9 S5)."""
    if not DEFAULT_PERSIST_STATE:
        return
    path = _runtime_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as ex:
        log.debug("state_restore_corrupt", error=str(ex))
        return
    age = time.time() - float(data.get("saved_at", 0))
    if age > DEFAULT_STATE_TTL_S:
        log.info("runtime_state_expired", age_s=round(age))
        return

    rl = _get_rate_limiter()
    try:
        rl.rate = max(rl.rate_floor, min(rl.rate_cap, float(data.get("rate", rl.rate))))
    except (TypeError, ValueError):
        pass

    cb = _get_cb()
    now = time.monotonic()
    restored = 0
    for key, remain_saved in (data.get("circuits") or {}).items():
        try:
            remain = float(remain_saved) - age  # cooldown decays with wall time too
        except (TypeError, ValueError):
            continue
        if remain > 0:
            cb._open_until[key] = now + remain
            restored += 1
    log.info(
        "runtime_state_restored",
        rate=round(rl.rate, 2),
        open_circuits=restored,
        age_s=round(age, 1),
    )


_rate_limiter: AdaptiveTokenBucket | None = None
_rate_lock = threading.Lock()


def _get_rate_limiter() -> AdaptiveTokenBucket:
    global _rate_limiter
    if _rate_limiter is None:
        with _rate_lock:
            if _rate_limiter is None:
                _rate_limiter = AdaptiveTokenBucket(
                    DEFAULT_RATE_LIMIT,
                    DEFAULT_RATE_BURST,
                    DEFAULT_RATE_CAP,
                    DEFAULT_RATE_FLOOR,
                    DEFAULT_RATE_INCREASE,
                    DEFAULT_RATE_DECREASE,
                )
    return _rate_limiter


# --------------------------------------------------------------------------- #
# Single-flight: coalesces concurrent identical requests → 1 network fetch
# --------------------------------------------------------------------------- #


class SingleFlight:
    """Per-key lock manager. Concurrent callers for same key block until the
    first completes, then hit cache. Prevents duplicate rate-limit-consuming
    fetches when multiple threads search the same query."""

    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def acquire(self, key: str) -> threading.Lock:
        with self._guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]


_single_flight = SingleFlight()


# --------------------------------------------------------------------------- #
# Latency tracker (p50/p99 per backend → prefer fast backends)
# --------------------------------------------------------------------------- #


class LatencyTracker:
    def __init__(self, window=50):
        self._window = window
        self._data: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self._lock = threading.Lock()

    def record(self, key: str, latency_s: float):
        with self._lock:
            self._data[key].append(latency_s)

    def percentile(self, key: str, p: float):
        with self._lock:
            samples = sorted(self._data.get(key, []))
        if len(samples) < 3:
            return None
        return round(samples[min(len(samples) - 1, int(len(samples) * p / 100))], 3)

    def snapshot(self):
        with self._lock:
            keys = list(self._data)
        return {
            k: {
                "p50": self.percentile(k, 50),
                "p99": self.percentile(k, 99),
                "n": len(self._data.get(k, [])),
            }
            for k in keys
        }


_latency: LatencyTracker | None = None


def _get_latency() -> LatencyTracker:
    global _latency
    if _latency is None:
        _latency = LatencyTracker()
    return _latency


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


class CircuitBreaker:
    def __init__(self, failure_threshold, cooldown_s, window_s=60.0):
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_s = max(1.0, cooldown_s)
        self.window_s = window_s
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._open_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_open(self, key):
        with self._lock:
            until = self._open_until.get(key, 0.0)
            now = time.monotonic()
            if until and now < until:
                return True
            if until:
                self._open_until.pop(key, None)
            return False

    def record_failure(self, key):
        with self._lock:
            now = time.monotonic()
            wins = [t for t in self._failures[key] if now - t < self.window_s]
            wins.append(now)
            self._failures[key] = wins
            if len(wins) >= self.failure_threshold:
                self._open_until[key] = now + self.cooldown_s
                self._failures[key] = []

    def record_success(self, key):
        with self._lock:
            self._failures.pop(key, None)
            self._open_until.pop(key, None)

    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            return {
                k: {
                    "state": "OPEN" if now < v else "CLOSED",
                    "open_for_s": max(0, round(v - now, 1)) if now < v else 0,
                }
                for k, v in self._open_until.items()
                if now < v
            }


_cb: CircuitBreaker | None = None


def _get_cb() -> CircuitBreaker:
    global _cb
    if _cb is None:
        _cb = CircuitBreaker(DEFAULT_CB_FAILURE_THRESHOLD, DEFAULT_CB_COOLDOWN)
    return _cb


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.requests = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.cache_stale = 0
        self.neg_cache_hits = 0
        self.coalesced = 0
        self.rate_limited = 0
        self.timeouts = 0
        self.retries = 0
        self.circuit_blocks = 0
        self.extracts = 0
        self.per_backend: dict[str, dict[str, int]] = defaultdict(
            lambda: {"ok": 0, "fail": 0}
        )

    _COUNTERS = frozenset(
        {
            "requests",
            "cache_hits",
            "cache_misses",
            "cache_stale",
            "neg_cache_hits",
            "coalesced",
            "rate_limited",
            "timeouts",
            "retries",
            "circuit_blocks",
            "extracts",
        }
    )

    def incr(self, name, n=1):
        if name not in self._COUNTERS:
            # Fail loud on typos instead of silently creating phantom counters.
            raise ValueError(f"unknown metric counter: {name!r}")
        with self._lock:
            setattr(self, name, getattr(self, name, 0) + n)

    def backend_result(self, backend, ok):
        with self._lock:
            self.per_backend[backend]["ok" if ok else "fail"] += 1

    def snapshot(self):
        with self._lock:
            return {
                "requests": self.requests,
                "cache_hits": self.cache_hits,
                "cache_misses": self.cache_misses,
                "cache_stale": self.cache_stale,
                "neg_cache_hits": self.neg_cache_hits,
                "coalesced": self.coalesced,
                "rate_limited": self.rate_limited,
                "timeouts": self.timeouts,
                "retries": self.retries,
                "circuit_blocks": self.circuit_blocks,
                "extracts": self.extracts,
                "per_backend": {k: dict(v) for k, v in self.per_backend.items()},
            }


_metrics: Metrics | None = None


def _get_metrics() -> Metrics:
    global _metrics
    if _metrics is None:
        _metrics = Metrics()
    return _metrics


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def _cache_key(category: str, params: dict[str, Any]) -> str:
    canon = json.dumps(
        {"category": category, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    return DEFAULT_CACHE_DIR / key[:2] / f"{key}.json"


def _cache_read(
    key: str, ttl_pos: int, ttl_neg: int
) -> tuple[dict[str, Any] | None, str]:
    if not DEFAULT_CACHE_ENABLED:
        return None, "miss"
    p = _cache_path(key)
    if not p.exists():
        return None, "miss"
    try:
        age = time.time() - p.stat().st_mtime
        payload = json.loads(p.read_text(encoding="utf-8"))
        is_neg = payload.get("__neg__", False)
        ttl = ttl_neg if is_neg else ttl_pos
        if age <= ttl:
            return payload, "neg" if is_neg else "fresh"
        return payload, "stale" if not is_neg else "miss"
    except (OSError, json.JSONDecodeError) as ex:
        log.warning("cache_read_failed", key=key[:12], error=str(ex))
        return None, "miss"


def _cache_delete(key: str) -> None:
    """Remove a cache entry (e.g. poisoned/stale content discovered on read)."""
    if not DEFAULT_CACHE_ENABLED:
        return
    try:
        _cache_path(key).unlink(missing_ok=True)
    except OSError as ex:
        log.warning("cache_delete_failed", key=key[:12], error=str(ex))


def _cache_write(key: str, payload: dict[str, Any], negative: bool = False) -> None:
    if not DEFAULT_CACHE_ENABLED:
        return
    p = _cache_path(key)
    tmp = p.with_suffix(".json.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        body = dict(payload)
        if negative:
            body["__neg__"] = True
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except OSError as ex:
        log.warning("cache_write_failed", key=key[:12], error=str(ex))
        try:
            tmp.unlink(missing_ok=True)
        except OSError as ex:
            log.debug("cache_tmp_cleanup_failed", key=key[:12], error=str(ex))


def _cache_cleanup(max_age_s: int) -> int:
    if not DEFAULT_CACHE_DIR.exists():
        return 0
    cutoff = time.time() - max_age_s
    removed = 0
    try:
        for f in DEFAULT_CACHE_DIR.rglob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    removed += 1
            except OSError as ex:
                log.debug("cache_file_unlink_failed", path=str(f), error=str(ex))
    except OSError as ex:
        log.debug("cache_walk_failed", dir=str(DEFAULT_CACHE_DIR), error=str(ex))
    return removed


def _backoff_sleep(attempt: int) -> float:
    delay = min(DEFAULT_BACKOFF_CAP, DEFAULT_BACKOFF_BASE * (2**attempt))
    jitter = delay * DEFAULT_BACKOFF_JITTER * (2 * random.random() - 1)
    sleep_s = min(DEFAULT_BACKOFF_CAP, max(0.1, delay + jitter))
    time.sleep(sleep_s)
    return sleep_s


# --------------------------------------------------------------------------- #
# Result hygiene (objective metadata only — NO analysis)
# --------------------------------------------------------------------------- #

# Date extraction regexes (mechanical parsing, NOT cognitive analysis)
_RE_DATE_ISO = re.compile(
    r"\b((?:19|20)\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])(?!\d)"
)
_RE_DATE_NAMED = re.compile(
    r"\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?))"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_RE_DATE_DMY = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?))"
    r",?\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_RE_DATE_YM = re.compile(r"\b((?:19|20)\d{2})[-/](0?[1-9]|1[0-2])\b")
_RE_RELATIVE = re.compile(r"\b(\d+)\s+(day|week|month|year)s?\s+ago\b", re.IGNORECASE)
_RE_PUBLISHED_PREFIX = re.compile(
    r"(?:published|updated|posted|date)[:\s]+", re.IGNORECASE
)

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _extract_date(text: str) -> str:
    """Extract first date from text as ISO 8601 (YYYY-MM-DD or YYYY-MM).

    Two-layer: (1) htmldate.find_date — SOTA extraction over JSON-LD,
    Open Graph, meta tags, schema.org, extensive text patterns (was installed
    but unused — fixes the freshness spoofing risk where page-text regex
    picked wrong dates); (2) regex fallback (ISO > named-month > DMY >
    relative > year-month) for snippets without HTML structure.
    Returns "" if no date found.
    """
    if not text:
        return ""

    # Layer 1: htmldate (SOTA). Handles HTML snippets and plain text; uses
    # metadata first (article:published_time, JSON-LD datePublished, OG),
    # then extensive text patterns. Lazy import — heavy module.
    try:
        from htmldate import find_date  # hard dep (transitive via trafilatura)

        # htmldate expects HTML; wrap text to look like a minimal doc so the
        # meta-tag pathways don't fire spuriously. find_date is fail-fast on
        # non-HTML input and falls back to its regex extractor.
        candidate = text if "<" in text else f"<p>{text}</p>"
        d = find_date(candidate, outputformat="%Y-%m-%d", extensive_search=True)
        if d and len(d) == 10 and d.startswith(("19", "20")):
            return d
        d = find_date(candidate, outputformat="%Y-%m", extensive_search=True)
        if d and len(d) == 7 and d.startswith(("19", "20")):
            return d
    except Exception as ex:
        log.debug("htmldate_failed", error=str(ex)[:80])

    # Layer 2: regex fallback (kept — handles snippet text without HTML)
    stripped = _RE_PUBLISHED_PREFIX.sub(" ", text)

    # ISO: 2025-03-15
    m = _RE_DATE_ISO.search(stripped)
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    # Named: March 15, 2025
    m = _RE_DATE_NAMED.search(stripped)
    if m:
        mo_name, d, y = m.groups()
        mo = _MONTHS.get(mo_name.lower()[:3] if len(mo_name) > 3 else mo_name.lower())
        if mo:
            return f"{int(y):04d}-{mo:02d}-{int(d):02d}"

    # DMY: 15 March 2025
    m = _RE_DATE_DMY.search(stripped)
    if m:
        d, mo_name, y = m.groups()
        mo = _MONTHS.get(mo_name.lower()[:3] if len(mo_name) > 3 else mo_name.lower())
        if mo:
            return f"{int(y):04d}-{mo:02d}-{int(d):02d}"

    # Relative: "2 days ago"
    m = _RE_RELATIVE.search(stripped)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"day": n, "week": n * 7, "month": n * 30, "year": n * 365}.get(unit, 0)
        ref = time.time() - days * 86400
        dt = time.gmtime(ref)
        return f"{dt.tm_year:04d}-{dt.tm_mon:02d}-{dt.tm_mday:02d}"

    # Year-Month: 2025-03
    m = _RE_DATE_YM.search(stripped)
    if m:
        y, mo = m.groups()
        return f"{int(y):04d}-{int(mo):02d}"

    return ""


def _date_to_timestamp(date_str: str) -> float:
    """Parse ISO date (YYYY-MM-DD or YYYY-MM) to UTC timestamp. 0 if unparseable.
    Uses calendar.timegm — time.mktime interprets the tuple in LOCAL time while
    every comparison target (time.time()) is UTC, skewing age_days by the tz
    offset (F-15)."""
    import calendar

    if not date_str:
        return 0.0
    try:
        parts = date_str.split("-")
        if len(parts) >= 3:
            y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
            return calendar.timegm((y, mo, d, 0, 0, 0))
        elif len(parts) == 2:
            y, mo = int(parts[0]), int(parts[1])
            return calendar.timegm((y, mo, 1, 0, 0, 0))
    except (ValueError, OverflowError):
        pass
    return 0.0


def _compute_age_days(date_str: str) -> int | None:
    """Days from the date to now. None if no date."""
    ts = _date_to_timestamp(date_str)
    if ts <= 0:
        return None
    return max(0, int((time.time() - ts) / 86400))


def _detect_language(text: str) -> str:
    """Heuristic language detection via Unicode block ranges.

    Mechanical character-range counting — NOT NLP. Returns ISO 639-1 code.
    Covers: Latin (en fallback), CJK Han (zh), Hiragana/Katakana (ja), Hangul
    (ko), Cyrillic (ru), Devanagari (hi), Arabic (ar), Hebrew (he), Greek (el).
    Limitation: Latin-script non-English (Dutch/German/French) returns 'en'
    because the script doesn't disambiguate. For skill's filtering use case
    (kill non-Latin spam from English queries) this is adequate; agents
    discard Latin-script off-topic results in-context.
    """
    if not text:
        return "unknown"
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return "unknown"
    n = len(alpha)
    cjk = sum(1 for c in alpha if "\u4e00" <= c <= "\u9fff")
    hiragana = sum(1 for c in alpha if "\u3040" <= c <= "\u309f")
    katakana = sum(1 for c in alpha if "\u30a0" <= c <= "\u30ff")
    hangul = sum(1 for c in alpha if "\uac00" <= c <= "\ud7af")
    cyrillic = sum(1 for c in alpha if "\u0400" <= c <= "\u04ff")
    devanagari = sum(1 for c in alpha if "\u0900" <= c <= "\u097f")
    arabic = sum(1 for c in alpha if "\u0600" <= c <= "\u06ff")
    hebrew = sum(1 for c in alpha if "\u0590" <= c <= "\u05ff")
    greek = sum(1 for c in alpha if "\u0370" <= c <= "\u03ff")
    # Japanese: any kana presence → ja (even with kanji overlap)
    if hiragana + katakana > n * 0.05:
        return "ja"
    if cjk > n * 0.15:
        return "zh"
    if hangul > n * 0.15:
        return "ko"
    if cyrillic > n * 0.15:
        return "ru"
    if devanagari > n * 0.15:
        return "hi"
    if arabic > n * 0.15:
        return "ar"
    if hebrew > n * 0.15:
        return "he"
    if greek > n * 0.15:
        return "el"
    if all(ord(c) < 128 for c in alpha):
        return "en"
    return "other"


def _bm25_rerank(results: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Add a `bm25_score` field to each result via BM25Okapi fit on title+body.

    BM25 is the SOTA *lexical* reranker — IDF-weighted term frequency with
    document-length normalization. Beats naive keyword-count (old
    `_score_relevance`) which gave same weight to common words ("India",
    "tax") as rare ones ("LTCG", "STT"). rank_bm25 is pure-Python, no model
    download. Neural rerankers (cross-encoder, mxbai) would be SOTA-er but
    require 200MB+ model files (§7c dep gate — skipped).
    """
    if not results or not query:
        return results
    try:
        from rank_bm25 import BM25Okapi  # hard dep (already installed)
    except ImportError:
        # No reranker — fall back silently, leave bm25_score unset
        return results

    query_tokens = [t.lower() for t in re.split(r"\s+", query) if len(t) > 1]
    if not query_tokens:
        return results

    docs = []
    for r in results:
        text = f"{r.get('title', '')} {r.get('body', '')} {r.get('href', '')}".lower()
        docs.append([t for t in re.split(r"\s+", text) if len(t) > 1])
    if not any(docs):
        return results

    bm25 = BM25Okapi(docs)
    scores = bm25.get_scores(query_tokens)
    for r, s in zip(results, scores):
        r["bm25_score"] = float(s)
    return results


# --------------------------------------------------------------------------- #
# Semantic reranker — fastembed bi-encoder (P2.5).
#   Opt-in via WEB_SEARCH_SEMANTIC_RERANK=1. Uses BAAI/bge-small-en-v1.5
#   (ONNX, 67MB, lazy_load) — a bi-encoder that embeds query + docs separately
#   and ranks by cosine similarity. Chosen over a cross-encoder after empirical
#   testing: bi-encoders RESCUE the documented BM25 synonym-miss failure mode
#   (e.g. "repo rate" query vs "Reserve Bank holds key lending rate" doc —
#   BM25 scores 0.000, bi-encoder scores 0.67), while cross-encoders
#   (ms-marco-MiniLM-L-6-v2) scored the synonym 0.0 — they do fine-grained
#   relevance but NOT synonym/paraphrase expansion.
#
#   Hybrid blend: FinalScore = w_bm25 * bm25_norm + w_sem * sem_norm.
#   Weights env-configurable; default favors semantic (0.4/0.6) since BM25
#   already runs first as a candidate filter.
#
#   Fail-hard (H2): if fastembed missing when flag is ON → BackendUnavailable.
#   No silent fallback to BM25-only — the user explicitly opted in.
# --------------------------------------------------------------------------- #


class SemanticReranker:
    """Hybrid BM25 + bi-encoder semantic reranker. Lazy-loads the ONNX model
    on first rerank() call (~0.3-0.5s warm; first-ever run downloads 67MB to
    ~/.cache/fastembed/). Embeds query + each doc title+body, ranks by cosine
    similarity, then blends with BM25 via configurable weights."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        w_bm25: float | None = None,
        w_sem: float | None = None,
    ) -> None:
        self._model_name = model_name
        if w_bm25 is None:
            w_bm25 = _env_float("WEB_SEARCH_SEMANTIC_WEIGHT_BM25", 0.4)
        if w_sem is None:
            w_sem = _env_float("WEB_SEARCH_SEMANTIC_WEIGHT_SEM", 0.6)
        total = w_bm25 + w_sem
        self._w_bm25 = w_bm25 / total if total > 0 else 0.4
        self._w_sem = w_sem / total if total > 0 else 0.6
        self._model: Any = None
        # Per-doc embedding cache: hash(doc_text) → embedding vector. Survives
        # across rerank() calls within one process so the agentic loop doesn't
        # re-embed docs that already appeared in a prior iteration.
        # Bounded (F-35): FIFO-evict beyond the cap, like the DNS cache (F-34).
        from collections import OrderedDict

        self._embed_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embed_cache_max = 2048

    def _embed_cache_put(self, key: str, emb: list[float]) -> None:
        """Insert into the bounded embed cache; FIFO-evict oldest beyond cap (F-35)."""
        self._embed_cache[key] = emb
        self._embed_cache.move_to_end(key)
        while len(self._embed_cache) > self._embed_cache_max:
            self._embed_cache.popitem(last=False)

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from fastembed import TextEmbedding
        except ImportError as ex:
            raise BackendUnavailable(
                f"fastembed not installed but WEB_SEARCH_SEMANTIC_RERANK=1. "
                f"Install: uv pip install fastembed. Error: {ex}"
            )
        log.info("semantic_reranker_loading", model=self._model_name)
        self._model = TextEmbedding(model_name=self._model_name)

    @staticmethod
    def _cosine(a, b) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def rerank(
        self, query: str, results: list[dict[str, Any]], top_n: int | None = None
    ) -> list[dict[str, Any]]:
        """Rerank by hybrid BM25+semantic score. Adds `semantic_score` (0-1
        normalized cosine) and `hybrid_score` fields. Returns a NEW sorted list."""
        if not results or not query:
            return results
        self._load()

        # Embed query + docs. BGE models use a "query:" prefix for asymmetric search.
        docs_text = [
            f"{r.get('title', '')} {r.get('body', '')}".strip() or r.get("href", "")
            for r in results
        ]
        q_emb = list(self._model.query_embed(query))[0]
        # Embedding cache: skip docs whose text was embedded before in this
        # process. Embed only the misses, then merge back in original order.
        d_embs: list[list[float] | None] = [None] * len(docs_text)
        to_embed: list[str] = []
        to_embed_idx: list[int] = []
        for i, text in enumerate(docs_text):
            h = hashlib.md5(text.encode("utf-8")).hexdigest()
            cached = self._embed_cache.get(h)
            if cached is not None:
                d_embs[i] = cached
            else:
                to_embed.append(text)
                to_embed_idx.append((i, h))
        if to_embed:
            new_embs = list(self._model.embed(to_embed))
            for (i, h), emb in zip(to_embed_idx, new_embs):
                self._embed_cache_put(h, list(emb))
                d_embs[i] = list(emb)
        raw_sem = [self._cosine(q_emb, d) if d is not None else 0.0 for d in d_embs]

        # Min-max normalize to [0, 1].
        smin, smax = min(raw_sem), max(raw_sem)
        s_rng = (smax - smin) or 1.0
        sem_norm = [(s - smin) / s_rng for s in raw_sem]

        raw_bm25 = [
            float(r.get("bm25_score", r.get("relevance_score", 0))) for r in results
        ]
        bmin, bmax = min(raw_bm25), max(raw_bm25)
        b_rng = (bmax - bmin) or 1.0
        bm25_norm = [(b - bmin) / b_rng for b in raw_bm25]

        for i, r in enumerate(results):
            r["semantic_score"] = round(sem_norm[i], 4)
            r["hybrid_score"] = round(
                self._w_bm25 * bm25_norm[i] + self._w_sem * sem_norm[i], 4
            )

        ranked = sorted(results, key=lambda r: -r.get("hybrid_score", 0))
        log.info(
            "semantic_rerank_done",
            n=len(ranked),
            top_hybrid=ranked[0].get("hybrid_score") if ranked else 0,
            w_bm25=round(self._w_bm25, 2),
            w_sem=round(self._w_sem, 2),
        )
        return ranked[:top_n] if top_n else ranked


_reranker_instance: SemanticReranker | None = None
_reranker_lock = threading.Lock()


def _get_semantic_reranker() -> SemanticReranker:
    """Lazy singleton for the cross-encoder reranker."""
    global _reranker_instance
    if _reranker_instance is None:
        with _reranker_lock:
            if _reranker_instance is None:
                _reranker_instance = SemanticReranker()
    return _reranker_instance


def _semantic_rerank_if_enabled(
    query: str, results: list[dict[str, Any]], top_n: int | None = None
) -> list[dict[str, Any]]:
    """Apply semantic reranking ONLY when WEB_SEARCH_SEMANTIC_RERANK=1.
    When OFF (default): pass through unchanged, zero overhead.
    When ON + fastembed missing: raise BackendUnavailable (fail-hard, H2)."""
    if not results:
        return results
    if not _env_bool("WEB_SEARCH_SEMANTIC_RERANK", False):
        return results
    reranker = _get_semantic_reranker()
    return reranker.rerank(query, results, top_n=top_n)


def _simhash(text: str, bits: int = 64) -> int:
    """Charikar's SimHash — locality-sensitive fingerprint for near-dup detect.

    Near-duplicate texts (AP wire story syndicated across news sites with
    minor edits) produce hashes within small Hamming distance. Pure
    hashlib, no deps. Used by `_dedup_by_content` to drop syndicated dupes
    that survive URL-canonical dedup (different URLs, same article body).

    Tokenization: word tokens with punctuation stripped (so "india:" matches
    "india"). For very short text (<50 tokens) threshold should be 5-8;
    longer text tolerates threshold 3.
    """
    if not text:
        return 0
    # Strip punctuation edges: "india:" → "india", "(ETF)" → "ETF"
    raw_tokens = re.split(r"\s+", text.lower())
    tokens = [t.strip(".,;:!?()[]\"'`/—–-") for t in raw_tokens]
    tokens = [t for t in tokens if len(t) > 1]
    if not tokens:
        return 0
    v = [0] * bits
    for tok in tokens:
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if v[i] > 0:
            out |= 1 << i
    return out


def _hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _dedup_by_content(
    results: list[dict[str, Any]], threshold: int = 3
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop near-duplicate results by SimHash on title+body.

    Catches syndicated content (same AP wire story on reuters.com, finance.yahoo.com,
    moneycontrol.com — different canonical URLs but ~95% identical body text).
    Returns (kept, dropped). When two results collide, keeps the higher-tier
    (lower number = more authoritative) one; on tier tie, keeps the higher
    bm25_score / relevance_score.

    O(n²) on the result set — fine for n≤200 (agentic yields ≤100).
    """
    if len(results) < 2:
        return list(results), []
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    # Pre-compute simhash for each result. BODY-ONLY (not title+body) —
    # syndicated content (AP wire, same article re-published across sites)
    # has identical bodies but editorialized headlines. Title inclusion
    # inflated Hamming distance and missed real syndication. Body-only
    # SimHash catches the actual duplicate content signal.
    for r in results:
        body = (r.get("body", "") or "").strip()
        r["_simhash"] = _simhash(body)
    for r in results:
        h = r.get("_simhash", 0)
        is_dup = False
        for k in kept:
            kh = k.get("_simhash", 0)
            if h and kh and _hamming_distance(h, kh) <= threshold:
                is_dup = True
                # Tie-break: lower tier wins; tier tie → higher bm25/relevance wins
                r_score = r.get("bm25_score", r.get("relevance_score", 0))
                k_score = k.get("bm25_score", k.get("relevance_score", 0))
                if r.get("tier", 3) < k.get("tier", 3) or (
                    r.get("tier", 3) == k.get("tier", 3) and r_score > k_score
                ):
                    kept.remove(k)
                    dropped.append(k)
                    kept.append(r)
                else:
                    dropped.append(r)
                break
        if not is_dup:
            kept.append(r)
    return kept, dropped


def _score_relevance(result: dict[str, Any], query_terms: set[str]) -> int:
    """Coverage-weighted keyword overlap for diversification ordering.

    F-60: the raw overlap count let partial matches rank alongside full
    matches ("Unity Incremental Garbage Collection" scored 3/9 vs a real
    cargo answer's 4/9 for a rust-cargo query — common terms carried it).
    Score = matched x coverage-ratio so a result must cover MOST of the
    query's terms to score highly; matching 2 scattered common words no
    longer competes with matching 7 of 9.
    """
    text = " ".join(
        str(result.get(k, ""))
        for k in ("title", "body", "href", "source", "description", "snippet")
    ).lower()
    if not query_terms:
        return 0
    matched = sum(1 for t in query_terms if t.lower() in text)
    return int(round(matched * (matched / len(query_terms))))


# F-50: discovery/navigation URL shapes are deployment-dependent; extend via
# WEB_SEARCH_EXTRA_NONCONTENT_PATTERNS (comma-separated regex fragments,
# OR-joined raw - power-user knob, not escaped).
_BASE_NONCONTENT = (
    r"/(discover|pin|ideas|search)(/|\?|$)"
    r"|pinterest\.[a-z.]+|^https?://(www\.)?(google|bing)\.[a-z.]+"
)
_EXTRA_NONCONTENT = [
    p.strip()
    for p in os.environ.get("WEB_SEARCH_EXTRA_NONCONTENT_PATTERNS", "").split(",")
    if p.strip()
]
_NONCONTENT_PATH_RE = re.compile(
    "|".join([_BASE_NONCONTENT, *_EXTRA_NONCONTENT]),
    re.I,
)


def _lang_gate(results: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """F-59: drop results whose detected language mismatches the query's.

    Live failure (2026-09-15): yahoo+google returned no results, rotation
    fell through to yandex (Russian-locale engine) which injected ru/zh
    tutorials into English technical queries — 4 of top-10 were habr.com /
    selectel.ru / metanit.com install guides. `_detect_language` annotated
    each result correctly but NOTHING consumed the field: language was
    metadata, not a filter.

    Gate is symmetric + fail-open:
    - Query's own script language wins; results in a DIFFERENT script
      language are dropped only when >= floor same-language results
      survive (floor avoids emptying rare-query result sets).
    - "unknown" language results always kept (can't judge).
    - Query itself undetermined ("unknown"/"other") -> no gating.
    - Script detection can't split Latin-script languages (de/fr detect
      as "en") — documented limitation of _detect_language.
    Env knobs: WEB_SEARCH_LANG_GATE=0 disables; WEB_SEARCH_LANG_FLOOR=N
    (default 5) sets the fail-open floor.
    """
    if not results or not query:
        return results
    flag = os.environ.get("WEB_SEARCH_LANG_GATE", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return results
    q_lang = _detect_language(query)
    if q_lang in ("unknown", "other"):
        return results
    try:
        floor = max(1, int(os.environ.get("WEB_SEARCH_LANG_FLOOR", "5")))
    except ValueError:
        floor = 5
    keep_langs = {q_lang, "unknown"}
    kept = [r for r in results if r.get("language") in keep_langs]
    if len(kept) < floor:
        return results  # fail-open: not enough same-language evidence
    dropped = len(results) - len(kept)
    if dropped:
        log.info(
            "lang_gate_dropped",
            query_lang=q_lang,
            dropped=dropped,
            kept=len(kept),
            floor=floor,
        )
    return kept


def _normalize_results(
    results: list[dict[str, Any]],
    query_terms: set[str],
    max_per_domain: int,
    source: str = "",
    query: str = "",
) -> list[dict[str, Any]]:
    """Canonicalize → dedupe → enrich → diversify. NO sort/rank (applied later).

    Cache stores enriched unsorted data; sort is a VIEW concern applied via
    `_apply_sort()` after cache read, so different sort strategies reuse
    the same cache entry.
    """
    seen: set[str] = set()
    annotated = []
    for r in results:
        href = r.get("href") or r.get("url") or r.get("image") or ""
        canon = canonicalize_url(href)
        if canon and canon in seen:
            continue
        if canon:
            seen.add(canon)
        host = _host_of(canon or href)
        r2 = dict(r)
        if canon:
            r2["canonical_url"] = canon
        r2["host"] = host
        r2["tier"] = domain_tier(host)
        r2["relevance_score"] = _score_relevance(r, query_terms)
        # F-50: social-discovery/search-result pages are navigation by
        # construction — halve their score so real content outranks them.
        if _NONCONTENT_PATH_RE.search(canon or href):
            r2["relevance_score"] = int(r2["relevance_score"] * 0.5)
            r2["low_quality_source"] = True
        # F-49: stats-methods pages matching polysemous terms ("confidence")
        # get the same demotion unless the query asks for methods. A TITLE
        # declaring methods vocabulary condemns by itself.
        if _title_declares_stats_methods(
            str(r.get("title", ""))
        ) or _is_stats_methods_noise(
            str(r.get("title", "")) + " " + str(r.get("body", "")), query_terms
        ):
            r2["relevance_score"] = int(r2["relevance_score"] * 0.5)
            r2["stats_methods_noise"] = True
        if source:
            r2["source"] = source
        # Date enrichment: use existing date or extract from title+body
        date_val = str(r.get("date") or r.get("published") or "")
        if date_val:
            extracted = _extract_date(date_val)
            date_val = extracted or date_val[:10]
        else:
            snippet = str(r.get("title", "")) + " " + str(r.get("body", ""))
            date_val = _extract_date(snippet)
        r2["date"] = date_val
        r2["age_days"] = _compute_age_days(date_val)
        lang_text = str(r.get("title", "")) + " " + str(r.get("body", ""))
        r2["language"] = _detect_language(lang_text)
        annotated.append(r2)

    # Authority-weighted pre-sort for diversification only
    annotated.sort(key=lambda r: (r.get("tier", 3), -r.get("relevance_score", 0)))
    counts: dict[str, int] = defaultdict(int)
    out = []
    for r in annotated:
        if counts[r["host"]] < max_per_domain:
            out.append(r)
            counts[r["host"]] += 1
    return _lang_gate(out, query) if query else out


def _apply_sort(results: list[dict[str, Any]], strategy: str) -> list[dict[str, Any]]:
    """Sort + assign rank. Applied AFTER cache read (view concern).

    latest:     dated first (newest), then undated by authority
    authority:  tier-first, then date, then relevance
    relevance:  keyword overlap first, then authority, then date
    """
    for r in results:
        r["_ts"] = _date_to_timestamp(r.get("date", ""))

    if strategy == "authority":
        results.sort(
            key=lambda r: (
                r.get("tier", 3),
                -r["_ts"],
                -r.get("relevance_score", 0),
                r.get("host", ""),
            )
        )
    elif strategy == "relevance":
        results.sort(
            key=lambda r: (
                -r.get("relevance_score", 0),
                r.get("tier", 3),
                -r["_ts"],
                r.get("host", ""),
            )
        )
    else:  # "latest" (default)
        results.sort(
            key=lambda r: (
                0 if r["_ts"] > 0 else 1,
                -r["_ts"],
                r.get("tier", 3),
                -r.get("relevance_score", 0),
                r.get("host", ""),
            )
        )

    for r in results:
        r.pop("_ts", None)
    for i, r in enumerate(results):
        r["rank"] = i + 1
    return results


def _build_citation(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": r.get("canonical_url") or r.get("href") or r.get("url") or "",
        "title": r.get("title") or r.get("name") or "",
        "host": r.get("host", ""),
        "tier": r.get("tier", 3),
        "date": r.get("date") or r.get("published") or "",
        "source": r.get("source", ""),
        "snippet": (r.get("body") or r.get("description") or "")[:500],
    }


_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _strip_md_syntax(text: str) -> str:
    """F-52: collapse markdown images and [text](url) links to their text so
    passages carry prose, not syntax noise."""
    text = _MD_IMAGE_RE.sub("", text)
    return _MD_LINK_RE.sub(r"\1", text)


# F-49: polysemous terms ("confidence", "significance") route statistics-METHODS
# pages into subject queries. Two-tier vocabulary:
#   STRONG - pure methods constructs; ONE mention in a title is decisive,
#            one in a passage marks it methods-flavored.
#   WEAK   - statistics that legitimate subject-research REPORTS inline
#            (findings cite p-values); needs >=2 distinct hits to condemn.
# Domain-knowledge heuristics (English research-writing conventions), NOT
# deployment config: kept as code constants deliberately per the dynamic-
# implementation rule's boundary - behavior PARAMETERS are env/Settings-driven
# (see DEFAULT_* above), while domain KNOWLEDGE lives in versioned code where
# it is testable and auditable. Extend via pins when new patterns emerge.
_STATS_STRONG_RE = re.compile(
    r"confidence intervals?|standard error|margin of error|null hypothesis"
    r"|coefficient of determination|sampling distribution",
    re.I,
)
_STATS_WEAK_RE = re.compile(
    r"p\s*[<>]\s*0?\.\d|p-value|regression analysis|regression model|\bt-test\b"
    r"|\banova\b|z-score|r\^2|statistically significant",
    re.I,
)
_STATS_CONTEXT_TERMS = {
    "statistics",
    "statistical",
    "stats",
    "regression",
    "interval",
    "intervals",
    "p-value",
    "significance",
    "error",
    "anova",
    "t-test",
    "sampling",
    "methodology",
    "methods",
}


def _query_wants_stats_methods(query_terms: set[str]) -> bool:
    # Substring match so multiword terms ("confidence interval") count.
    q = " ".join(query_terms).lower()
    return any(t in q for t in _STATS_CONTEXT_TERMS)


def _is_stats_methods_noise(text: str, query_terms: set[str]) -> bool:
    """True when a passage/result is about statistics METHODS while the query
    is not. Strong marker x1 condemns; weak markers need >=2 distinct hits."""
    if _query_wants_stats_methods(query_terms):
        return False
    strong = bool(_STATS_STRONG_RE.search(text))
    weak = {m.group(0).lower() for m in _STATS_WEAK_RE.finditer(text)}
    return strong or len(weak) >= 2


def _title_declares_stats_methods(title: str) -> bool:
    """F-49 result-level rule: a page whose TITLE declares methods vocabulary
    is a methods page regardless of body drenching."""
    return bool(_STATS_STRONG_RE.search(title))


def find_key_passage(content: str, query_terms: set[str]) -> str:
    """Find paragraph with most keyword overlap. Simple signal for the AI,
    NOT a substitute for semantic understanding."""
    if not content or not query_terms:
        return ""
    paras = [p.strip() for p in re.split(r"\n\s*\n", content) if len(p.strip()) > 40]
    if not paras:
        return ""
    best, best_score = "", -1
    for p in paras[:50]:
        if _is_stats_methods_noise(p, query_terms):
            continue  # F-49: methods-math passage, not subject evidence
        score = sum(1 for t in query_terms if t.lower() in p.lower())
        if score > best_score:
            best_score, best = score, p
    return _strip_md_syntax(best)[:600] if best_score >= 0 else ""


# --------------------------------------------------------------------------- #
# DDGS construction + backend discovery
# --------------------------------------------------------------------------- #


def _build_ddgs(
    dht: bool,
    proxy: str | None,
    timeout: int,
    verify: bool | str = True,
) -> Any:
    DDGS, *_ = _import_ddgs()
    if DEFAULT_DDGS_THREADS > 0:
        DDGS.threads = DEFAULT_DDGS_THREADS
    # ddgs builds disagree on optional constructor kwargs (api_url /
    # spawn_api exist only in some versions) — pass each only when the
    # installed signature accepts it, so a venv refresh cannot break
    # every search.
    import inspect

    try:
        accepted = inspect.signature(DDGS.__init__).parameters
    except (TypeError, ValueError):
        accepted = {}
    kwargs: dict[str, Any] = {"proxy": proxy, "timeout": timeout}
    if "verify" in accepted:
        kwargs["verify"] = verify
    if "api_url" in accepted:
        kwargs["api_url"] = DEFAULT_DHT_API_URL if dht else None
    if "spawn_api" in accepted:
        kwargs["spawn_api"] = dht
    return DDGS(**kwargs)


TEXT_BACKENDS: list[str] = []


def _get_text_backends() -> list[str]:
    global TEXT_BACKENDS
    if TEXT_BACKENDS:
        return TEXT_BACKENDS
    try:
        from ddgs.engines import ENGINES

        bk = sorted(ENGINES.get("text", {}).keys())
        if bk:
            TEXT_BACKENDS = bk
            return TEXT_BACKENDS
    except ImportError as ex:
        log.debug("engines_registry_unavailable, using fallback", error=str(ex))
    TEXT_BACKENDS = [
        "duckduckgo",
        "google",
        "brave",
        "yandex",
        "yahoo",
        "mojeek",
        "wikipedia",
        "startpage",
        "grokipedia",
    ]
    return TEXT_BACKENDS


# --------------------------------------------------------------------------- #
# Defense-orchestrated search (single-flight + adaptive + latency-aware)
# --------------------------------------------------------------------------- #


def _clean_backend_spec(spec: str, known: set[str]) -> tuple[list[str], list[str]]:
    """F-58: split an explicit backend spec into (valid, unknown).

    Unknown registry (import failure) = everything passes through — graceful
    degradation beats blocking search over a missing registry."""
    requested = [b.strip() for b in spec.split(",") if b.strip()]
    if not known:
        return requested, []
    good = [b for b in requested if b in known]
    bad = [b for b in requested if b not in known]
    return good, bad


def _pick_backend(
    pool: list[str],
    cb: CircuitBreaker,
    category: str,
    latency: LatencyTracker,
) -> tuple[str, list[str]]:
    """Pick best available backend + return rotated pool (picked moves to end).

    Rotation ensures consecutive retries hit DIFFERENT backends immediately,
    without waiting for the circuit breaker to open (3 failures).
    """
    available = [b for b in pool if not cb.is_open(f"{category}:{b}")]
    if not available:
        available = list(pool)

    def _sort_key(b):
        p50 = latency.percentile(f"{category}:{b}", 50)
        return (0, p50) if p50 is not None else (1, 999.0)

    available.sort(key=_sort_key)
    picked = available[0]
    # Rotate: move picked to end of the ORIGINAL pool so next call sees it last
    rotated = [b for b in pool if b != picked] + [picked]
    return picked, rotated


class ResilientExecutor:
    """Cache + single-flight coordinator around an injected operation.

    Extracted from _search_with_defense (P1.3) so the cache+coalescing pattern
    is named, reusable, and testable in isolation. The rate-limiter, circuit
    breaker, and latency tracker stay inside _do_search (the retry loop's
    concern) — this class is the OUTER cache+dedup shell that wraps any
    operation needing request coalescing.

    Usage:
        executor = ResilientExecutor(metrics, single_flight)
        payload = executor.execute(
            cache_key=key, cache_ttl=ttl, neg_cache_ttl=neg_ttl,
            category=cat, query=q, params=p,
            operation=lambda cached, status: _do_search(...),
        )
    """

    def __init__(self, metrics: "Metrics", single_flight: "SingleFlight") -> None:
        self._metrics = metrics
        self._sf = single_flight

    def execute(
        self,
        *,
        cache_key: str,
        cache_ttl: int,
        neg_cache_ttl: int,
        category: str,
        query: str,
        params: dict[str, Any],
        operation,
    ) -> dict[str, Any]:
        """Cache read → single-flight acquire/coalesce → run operation → release.
        `operation(cached, status)` is called only on cache miss/stale and must
        return the full payload dict (it owns the retry loop + limiter + cb)."""
        cached, status = _cache_read(cache_key, cache_ttl, neg_cache_ttl)
        if cached is not None and status == "fresh":
            self._metrics.incr("cache_hits")
            cached["meta"]["cache_status"] = "fresh"
            cached["meta"]["served_from"] = "cache"
            return cached
        if cached is not None and status == "neg":
            self._metrics.incr("neg_cache_hits")
            return self._empty_payload(category, query, params, "neg", "neg_cache")
        if cached is not None and status == "stale":
            self._metrics.incr("cache_stale")
        else:
            self._metrics.incr("cache_misses")

        # Single-flight coalescing: concurrent identical queries wait for the
        # first caller, then hit cache. Prevents duplicate rate-limit-consuming
        # fetches when the agent fires the same query from multiple threads.
        sf_lock = self._sf.acquire(cache_key)
        sf_acquired = sf_lock.acquire(blocking=False)
        if not sf_acquired:
            self._metrics.incr("coalesced")
            sf_lock.acquire()
            sf_lock.release()
            cached2, status2 = _cache_read(cache_key, cache_ttl, neg_cache_ttl)
            if cached2 is not None and status2 in ("fresh", "neg"):
                self._metrics.incr("cache_hits")
                if status2 == "neg":
                    return self._empty_payload(
                        category, query, params, "neg", "neg_cache"
                    )
                cached2["meta"]["cache_status"] = "fresh"
                cached2["meta"]["served_from"] = "coalesced_cache"
                return cached2
            sf_lock = self._sf.acquire(cache_key)
            sf_lock.acquire()

        try:
            return operation(cached, status)
        finally:
            try:
                sf_lock.release()
            except RuntimeError:
                # Lock may have been released by a coalesced caller path — safe no-op.
                log.debug(
                    "single_flight_lock_already_released", cache_key=cache_key[:12]
                )

    @staticmethod
    def _empty_payload(
        category: str,
        query: str,
        params: dict[str, Any],
        cache_status: str,
        served_from: str,
    ) -> dict[str, Any]:
        return {
            "schema": OUTPUT_SCHEMA_VERSION,
            "query": query,
            "category": category,
            "params": params,
            "results": [],
            "citations": [],
            "meta": {
                "cache_status": cache_status,
                "served_from": served_from,
                "attempts": [],
                "backends_tried": [],
                "rate_limited": False,
                "timed_out": False,
            },
        }


def _search_with_defense(
    ddgs,
    category,
    query,
    *,
    region,
    safesearch,
    timelimit,
    max_results,
    page,
    backend,
    cache_ttl,
    neg_cache_ttl,
    call_deadline,
    proxy_for_log,
    max_retries: int | None = None,
) -> dict[str, Any]:
    """Full defense stack: cache → single-flight → limiter → circuit → search.
    Delegates the cache+single-flight shell to ResilientExecutor (P1.3); the
    retry loop + limiter + circuit + latency stay in _do_search. DDGS exception
    types are NO LONGER threaded as params — _do_search references them via
    module-level globals set by _import_ddgs."""
    metrics = _get_metrics()
    cb = _get_cb()
    limiter = _get_rate_limiter()
    latency = _get_latency()

    params = {
        "query": query,
        "region": region,
        "safesearch": safesearch,
        "timelimit": timelimit,
        "max_results": max_results,
        "page": page,
        "backend": backend,
    }
    key = _cache_key(category, params)

    executor = ResilientExecutor(metrics, _single_flight)

    def operation(cached, status) -> dict[str, Any]:
        return _do_search(
            ddgs,
            category,
            query,
            params,
            key,
            cached,
            status,
            metrics,
            cb,
            limiter,
            latency,
            cache_ttl,
            call_deadline,
            proxy_for_log,
            max_retries=max_retries,
        )

    payload = executor.execute(
        cache_key=key,
        cache_ttl=cache_ttl,
        neg_cache_ttl=neg_cache_ttl,
        category=category,
        query=query,
        params=params,
        operation=operation,
    )
    # F-48 serve-side guard: pre-floor positive-cache entries may still hold
    # boilerplate junk; validity is a property of the payload, not its age.
    payload["results"] = _drop_below_content_floor(payload.get("results", []))
    return payload


def _search_exit_code(payload: dict[str, Any], extract_ran: bool) -> int:
    """F-05/F-48 exit decision for text/news searches.

    3 = genuine empty: no results at all, OR extraction ran and every
    extraction failed to yield usable content (below quality floor / fetch
    errors). Binary records count as usable — they carry an actionable
    pdf-ocr route, not emptiness."""
    if not payload.get("results"):
        return 3
    if extract_ran:
        extracted = payload.get("extracted") or []
        if extracted and not any(
            e.get("content") is not None or e.get("binary") for e in extracted
        ):
            log.warning(
                "genuine_empty_no_usable_content",
                n_results=len(payload["results"]),
                n_extracted=len(extracted),
            )
            return 3
    return 0


def _rank_adaptive_seeds(
    results: list[dict[str, Any]], query_terms: set[str], k: int = 5
) -> list[str]:
    """F-54/F-65: rank adaptive-crawl seed candidates best-first by
    query-term overlap over the top-k hits; ties break toward authoritative
    tiers. Callers fall through the list when a seed yields nothing - scoring
    is PRE-crawl knowledge and cannot see yield quality (live case: a BIF
    panorama titled perfectly but yielding zero crawlable pages)."""

    def _score(r: dict[str, Any]) -> int:
        # F-62 follow-up: score TITLE/BODY only - URL slugs create false
        # friends (live case: /image/bif matched query token "bif").
        text = " ".join(str(r.get(f) or "") for f in ("title", "body")).lower()
        return sum(1 for t in query_terms if t in text)

    candidates = [r for r in results[:k] if (r.get("href") or r.get("canonical_url"))]
    # F-50 follow-up: never anchor the browser on flagged discovery/noise
    # pages while an honest result exists in the window.
    clean = [
        r
        for r in candidates
        if not (r.get("low_quality_source") or r.get("stats_methods_noise"))
    ]
    pool = clean or candidates
    # Zero-overlap pages are pointless adaptive seeds when anything matches;
    # keep them only as last resort.
    scored = [(_score(r), int(r.get("tier", 3)), r) for r in pool]
    positive = [t for t in scored if t[0] > 0]
    ranked_items = sorted(positive or scored, key=lambda t: (-t[0], t[1]))
    urls: list[str] = []
    for _, _, r in ranked_items:
        u = r.get("href") or r.get("canonical_url")
        if u and u not in urls:
            urls.append(u)
    return urls


def _content_floor_ok(result: dict[str, Any]) -> bool:
    """F-48: extracted content must clear the informativeness floor
    (DEFAULT_MIN_CONTENT_CHARS). Un-extracted results carry no evidence to
    judge and pass through. F-51: content whose bulk is markdown link/image
    scaffolding (nav footers, social-link lists) is boilerplate even when the
    raw char count clears the bar."""
    content = str(result.get("content") or "")
    if not content:
        return True
    plain = _strip_md_syntax(content)
    if len(content) >= DEFAULT_MIN_CONTENT_CHARS and len(
        plain
    ) < DEFAULT_FLOOR_LINK_RATIO * len(content):
        return False
    return len(content.strip()) >= DEFAULT_MIN_CONTENT_CHARS


def _drop_below_content_floor(
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop search results whose extraction is boilerplate-only junk.
    Structured warning keeps the drop observable (§6i)."""
    kept = [r for r in results if _content_floor_ok(r)]
    dropped = len(results) - len(kept)
    if dropped:
        log.warning(
            "low_quality_results_dropped",
            dropped=dropped,
            kept=len(kept),
            min_chars=DEFAULT_MIN_CONTENT_CHARS,
            category=None if not results else results[0].get("source"),
        )
    return kept


def _wikipedia_api_search(
    query: str, *, max_results: int = 8, timeout: float = 5.0
) -> list[dict[str, Any]]:
    """WS3 resilience floor: MediaWiki search API, direct — no ddgs involved.

    When every scraped backend is blocked/rate-limited/dead, this still
    answers: Wikipedia's API is a first-class public endpoint with no key
    and no bot wall. Encyclopedia-grade only (weak for niche technical
    queries), but a weak floor beats zero results. Returns ddgs-shaped
    results (title/body/href) so the normal normalize/sort pipeline applies.
    A descriptive User-Agent is REQUIRED — Wikipedia 403s default urllib UAs.
    """
    params = urlencode(
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": max_results,
            "srprop": "snippet",
            "format": "json",
        }
    )
    req = urllib_request.Request(
        f"https://en.wikipedia.org/w/api.php?{params}",
        headers={"User-Agent": _LLM_UA},
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib_error.URLError, OSError, json.JSONDecodeError) as ex:
        log.warning("wikipedia_floor_failed", error=str(ex)[:80])
        return []
    out: list[dict[str, Any]] = []
    for hit in payload.get("query", {}).get("search", []):
        title = str(hit.get("title", "")).strip()
        if not title:
            continue
        # Snippet ships search-highlight HTML — strip tags for plain text.
        snippet = re.sub(r"<[^>]+>", "", str(hit.get("snippet", "")))
        out.append(
            {
                "title": title,
                "body": snippet,
                "href": (
                    # Parens/apostrophes/bangs stay literal: MediaWiki canonical
                    # URLs keep them, and percent-encoding them would break
                    # dedup against the same page arriving via ddgs.
                    "https://en.wikipedia.org/wiki/"
                    + quote(title.replace(" ", "_"), safe="()'!")
                ),
            }
        )
    return out


# WS4: freshness-seeking vocabulary → auto timelimit. Strong markers want
# the last day, medium the last week, and explicit current/prior years the
# last month. Years are computed from the system clock so the pattern never
# rots. Only applied when the user passed NO --timelimit (explicit intent
# always wins) and only for text/news (images/videos have no time filter).
_RECENCY_STRONG_RE = re.compile(
    r"\b(today|tonight|yesterday|right now|breaking|just announced|"
    r"latest|newest|this week)\b",
    re.IGNORECASE,
)
_RECENCY_MEDIUM_RE = re.compile(
    r"\b(this month|last week|recently|new release|just released|"
    r"fresh|upcoming)\b",
    re.IGNORECASE,
)


def _detect_recency_intent(query: str) -> str | None:
    """Return the auto timelimit ('d' | 'w' | 'm') for freshness-seeking
    queries, None otherwise (WS4)."""
    if _RECENCY_STRONG_RE.search(query):
        return "d"
    if _RECENCY_MEDIUM_RE.search(query):
        return "w"
    this_year = time.localtime().tm_year
    years = "|".join(str(y) for y in (this_year, this_year - 1))
    if re.search(rf"\b(?:in\s+)?({years})\b", query):
        return "m"
    return None


def _do_search(
    ddgs,
    category,
    query,
    params,
    key,
    cached,
    status,
    metrics,
    cb,
    limiter,
    latency,
    cache_ttl,
    call_deadline,
    proxy_for_log,
    max_retries: int | None = None,
) -> dict[str, Any]:
    # DDGS exception types are module-level globals (set by _import_ddgs).
    # Referenced directly here — no param threading. Translated to SearchError
    # subclasses at the output boundary so callers are vendor-agnostic.
    text_backends = _get_text_backends()
    if params["backend"] in {"auto", "all"}:
        # ddgs resolves auto/all itself; only text gets the full rotated pool.
        pool = text_backends[:] if category == "text" else [params["backend"]]
        if category == "text":
            random.shuffle(pool)
    else:
        # F-58: ddgs silently falls back on unknown backend names; validate
        # explicit specs so routing goes where asked or fails loud.
        try:
            from ddgs.engines import ENGINES

            known = set(ENGINES.get(category, {}).keys())
        except ImportError:
            known = set()
        good, bad = _clean_backend_spec(params["backend"], known)
        if bad:
            log.warning(
                "unknown_backends_dropped",
                category=category,
                unknown=bad,
                kept=good,
            )
        if not good:
            raise SearchError(
                f"no valid {category} backends in {params['backend']!r}; "
                f"known: {sorted(known)[:8]}"
            )
        params["backend"] = ",".join(good)
        pool = good

    query_terms = {t for t in re.split(r"\s+", query) if len(t) > 2}
    attempts, backends_tried = [], []
    last_err = None
    was_rate_limited = was_timed_out = False

    # WS4: recency-intent auto timelimit — visible in the echoed params and
    # logged; never overrides an explicit user --timelimit.
    recency_intent: str | None = None
    if not params.get("timelimit") and category in ("text", "news"):
        recency_intent = _detect_recency_intent(query)
        if recency_intent:
            params = {**params, "timelimit": recency_intent}
            log.info(
                "recency_intent",
                query=query[:60],
                timelimit=recency_intent,
            )

    # --retries (F-58 follow-up): total attempts across rotation; a pinned
    # single backend receives all of them.
    retries_cap = max_retries if max_retries is not None else DEFAULT_MAX_RETRIES
    for attempt_idx in range(retries_cap + 1):
        cur_backend, pool = _pick_backend(pool, cb, category, latency)
        if call_deadline - time.monotonic() <= 0:
            break
        if not limiter.acquire(timeout=min(call_deadline - time.monotonic(), 30.0)):
            break

        t0 = time.monotonic()
        attempt = {
            "index": attempt_idx,
            "backend": cur_backend,
            "started": round(time.time(), 3),
        }
        metrics.incr("requests")

        try:
            method = getattr(ddgs, category)
            raw = method(
                query,
                region=params["region"],
                safesearch=params["safesearch"],
                timelimit=params["timelimit"],
                max_results=params["max_results"],
                page=params["page"],
                backend=cur_backend,
            )
            results = _drop_below_content_floor(
                _normalize_results(
                    raw or [],
                    query_terms,
                    DEFAULT_MAX_PER_DOMAIN,
                    source=cur_backend,
                    query=query,
                )
            )
            elapsed = time.monotonic() - t0
            latency.record(f"{category}:{cur_backend}", elapsed)
            limiter.on_success()
            cb.record_success(f"{category}:{cur_backend}")
            metrics.backend_result(cur_backend, ok=True)
            attempt.update(ok=True, n_results=len(results), latency_s=round(elapsed, 3))
            attempts.append(attempt)
            backends_tried.append(cur_backend)
            payload = {
                "schema": OUTPUT_SCHEMA_VERSION,
                "query": query,
                "category": category,
                "params": params,
                "results": results,
                "citations": (
                    [_build_citation(r) for r in results]
                    if DEFAULT_EMIT_CITATIONS
                    else []
                ),
                "meta": {
                    "cache_status": "miss",
                    "served_from": "live",
                    "attempts": attempts,
                    "backends_tried": backends_tried,
                    "rate_limited": False,
                    "timed_out": False,
                    "stale_returned": status == "stale",
                    "recency_intent": recency_intent,
                },
            }
            _cache_write(key, payload, negative=(len(results) == 0))
            log.info(
                "search_ok",
                category=category,
                query=query,
                backend=cur_backend,
                n_results=len(results),
                attempt=attempt_idx,
                latency_s=round(elapsed, 3),
                proxy=_redact_proxy(proxy_for_log),
            )
            return payload

        except _RatelimitException as ex:  # type: ignore[misc] — global set by _import_ddgs
            last_err = f"RatelimitException: {ex}"
            was_rate_limited = True
            metrics.incr("rate_limited")
            metrics.backend_result(cur_backend, ok=False)
            cb.record_failure(f"{category}:{cur_backend}")
            limiter.on_rate_limit()
            attempt.update(ok=False, error=last_err)
            attempts.append(attempt)
            backends_tried.append(cur_backend)
            if attempt_idx < retries_cap:
                metrics.incr("retries")
                slept = _backoff_sleep(attempt_idx)
                log.warning(
                    "rate_limited_backoff",
                    category=category,
                    query=query,
                    backend=cur_backend,
                    attempt=attempt_idx,
                    sleep_s=round(slept, 2),
                    new_rate=round(limiter.rate, 2),
                )
            continue

        except _TimeoutException as ex:  # type: ignore[misc] — global set by _import_ddgs
            last_err = f"TimeoutException: {ex}"
            was_timed_out = True
            metrics.incr("timeouts")
            metrics.backend_result(cur_backend, ok=False)
            cb.record_failure(f"{category}:{cur_backend}")
            attempt.update(ok=False, error=last_err)
            attempts.append(attempt)
            backends_tried.append(cur_backend)
            if attempt_idx < retries_cap:
                metrics.incr("retries")
                slept = _backoff_sleep(attempt_idx)
                log.warning(
                    "timeout_backoff",
                    category=category,
                    query=query,
                    backend=cur_backend,
                    attempt=attempt_idx,
                    sleep_s=round(slept, 2),
                )
            continue

        except _DDGSException as ex:  # type: ignore[misc] — global set by _import_ddgs
            err_str = str(ex)
            is_empty = "no results" in err_str.lower()
            attempt.update(ok=False, error=f"DDGSException: {ex}")
            attempts.append(attempt)
            backends_tried.append(cur_backend)
            metrics.backend_result(cur_backend, ok=False)
            cb.record_failure(f"{category}:{cur_backend}")
            last_err = f"DDGSException: {ex}"
            if is_empty:
                log.info(
                    "no_results_backend",
                    category=category,
                    query=query,
                    backend=cur_backend,
                    attempt=attempt_idx,
                )
            else:
                log.error(
                    "ddgs_error",
                    category=category,
                    query=query,
                    backend=cur_backend,
                    error=err_str,
                )
            if not is_empty and cached is not None and status == "stale":
                cached["meta"].update(
                    cache_status="stale",
                    attempts=attempts,
                    backends_tried=backends_tried,
                )
                return cached
            if attempt_idx < retries_cap:
                continue
            break

    if cached is not None and status == "stale":
        cached["meta"].update(
            cache_status="stale",
            attempts=attempts,
            backends_tried=backends_tried,
            rate_limited=was_rate_limited,
            timed_out=was_timed_out,
        )
        return cached

    # WS3 resilience floor: every backend attempt failed and there is no
    # stale cache. Try Wikipedia's first-class API before declaring defeat —
    # independent of ddgs health, no key, no bot wall. Emergency payloads
    # are NOT cached: the floor bridges an outage, it must not mask it for
    # a full TTL.
    if cached is None and category == "text":
        wiki = _wikipedia_api_search(query)
        if wiki:
            results = _drop_below_content_floor(
                _normalize_results(
                    wiki,
                    query_terms,
                    DEFAULT_MAX_PER_DOMAIN,
                    source="wikipedia-api",
                    query=query,
                )
            )
            if results:
                payload = {
                    "schema": OUTPUT_SCHEMA_VERSION,
                    "query": query,
                    "category": category,
                    "params": params,
                    "results": results,
                    "citations": (
                        [_build_citation(r) for r in results]
                        if DEFAULT_EMIT_CITATIONS
                        else []
                    ),
                    "meta": {
                        "cache_status": "miss",
                        "served_from": "live",
                        "emergency_backend": "wikipedia-api",
                        "attempts": attempts,
                        "backends_tried": backends_tried,
                        "rate_limited": was_rate_limited,
                        "timed_out": was_timed_out,
                        "recency_intent": recency_intent,
                    },
                }
                log.warning(
                    "wikipedia_floor_served",
                    query=query[:60],
                    n_results=len(results),
                )
                return payload

    # Translate the vendor exception to a vendor-agnostic SearchError so
    # callers never need to know about DDGS.
    raise BackendUnavailable(
        f"All {len(attempts)} attempts failed for {category!r} {query!r}. Last: {last_err}"
    )


# --------------------------------------------------------------------------- #
# Content extraction with bot-block bypass (ddgs → curl_cffi fallback)
# --------------------------------------------------------------------------- #

_BOT_BLOCK_INDICATORS = [
    "verifying that you are",
    "just a moment",
    "checking your browser",
    "challenge-platform",
    "cf-browser-verification",
    "cf_chl",
    "enable javascript and cookies",
    "ddos protection",
    "please verify you are a human",
    "ray id",
    "captcha-bypass",
    "attention required! | cloudflare",
    "_cf_chl",
]


# Common binary extensions → inferred MIME. Pre-check avoids wasting the
# trafilatura/crawl4ai cascade on bytes it would silently return empty on.
_BINARY_EXT_MAP = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".bz2": "application/x-bzip2",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".epub": "application/epub+zip",
    ".mobi": "application/x-mobipocket-ebook",
}

# Content-Type prefixes that indicate binary/non-HTML responses. Parameter
# stripped before matching (e.g. "text/html; charset=utf-8" → "text/html").
_BINARY_CT_PREFIXES = (
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats",
    "application/vnd.ms-",
    "application/vnd.rar",
    "application/zip",
    "application/x-tar",
    "application/gzip",
    "application/x-bzip2",
    "application/x-7z",
    "application/x-rar",
    "application/epub",
    "application/x-mobipocket",
    "image/",
    "video/",
    "audio/",
)


def _binary_url_inference(url: str) -> str | None:
    """Return inferred content_type if URL path ends with a binary extension."""
    path = urlparse(url).path.lower()
    if "." not in path:
        return None
    ext = "." + path.rsplit(".", 1)[-1]
    return _BINARY_EXT_MAP.get(ext)


def _is_binary_content_type(content_type: str) -> bool:
    """Check if a Content-Type header indicates non-HTML binary content."""
    if not content_type:
        return False
    ct_lower = content_type.lower().split(";", 1)[0].strip()
    return any(ct_lower.startswith(p) for p in _BINARY_CT_PREFIXES)


# F-02: magic-byte signatures for common binary containers. Content-Type from
# the ddgs layer is fabricated ("text/plain") and redirect chains can lie —
# the first bytes of the body cannot.
_BINARY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"%PS-", "application/postscript"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"PK\x07\x08", "application/zip"),
    (b"\xd0\xcf\x11\xe0", "application/x-ole-storage"),  # legacy MS Office
    (b"\x1f\x8b", "application/gzip"),
    (b"BZh", "application/x-bzip2"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"Rar!\x1a\x07", "application/vnd.rar"),
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"BM", None),  # ambiguous (.bmp vs text starting "BM") — skip
)


def _sniff_binary(body: str) -> str | None:
    """Return the inferred content_type if the body starts with a known
    binary magic signature, else None. Operates on the first bytes only."""
    if not body:
        return None
    head = body[:16].encode("utf-8", errors="ignore")
    for magic, ctype in _BINARY_MAGIC:
        if ctype and head.startswith(magic):
            return ctype
    return None


def _is_bot_blocked(content: str) -> bool:
    """Detect Cloudflare/bot-challenge interstitial pages.

    F-03: scans the first 4 KB of the page regardless of total size. The old
    `len(content) > 2000 → False` guard assumed challenge pages are tiny;
    real Cloudflare interstitials carry ~6-8 KB of inline JS and sailed
    straight past this check (live-verified on nowsecure.nl)."""
    if not content:
        return False
    lower = content[:4096].lower()
    return any(ind in lower for ind in _BOT_BLOCK_INDICATORS)


def _fetch_curl_cffi(url: str, cookies_header: str = "") -> tuple[str | None, str, str]:
    """Fetch URL with TLS fingerprint impersonation + complete browser headers.

    SSRF-safe: validates resolved IP before fetch AND after every redirect hop
    (DNS-rebinding guard). Enforces MAX_RESPONSE_BYTES via Content-Length
    pre-check + post-download body-size check.

    Returns (html, cookies_header_from_response, content_type). Lazy import.
    Raises InvalidURL / SSRFBlocked / BudgetExceeded on policy violation.
    Returns (None, cookies_header, "") on transport failure (existing contract).
    """
    from curl_cffi import requests as cffi_req  # hard dep

    resolver = get_safe_url_resolver()
    # Validate the initial URL — raises InvalidURL/SSRFBlocked before any network call.
    resolver.resolve_and_validate(url)

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        # WS9: Sec-Ch-Ua must match the advertised Chrome major everywhere
        # (_CHROME_UA_MAJOR) — a 120 header on a chrome124 TLS fingerprint
        # was a free bot tell.
        "Sec-Ch-Ua": (
            f'"Chromium";v="{_CHROME_UA_MAJOR}", "Not(A:Brand";v="24", '
            f'"Google Chrome";v="{_CHROME_UA_MAJOR}"'
        ),
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Linux"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    if cookies_header:
        headers["Cookie"] = cookies_header

    for profile in _impersonation_rotation():
        # WS9: Firefox/Safari impersonations must NOT carry Chrome-only
        # client hints — real Firefox/Safari never send Sec-Ch-Ua*. Stripping
        # them per-profile kills the header/fingerprint mismatch.
        prof_headers = dict(headers)
        if not profile.startswith("chrome"):
            for h in ("Sec-Ch-Ua", "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua-Platform"):
                prof_headers.pop(h, None)
        # Manual redirect loop: re-validate IP at every hop to defeat DNS rebinding.
        current_url = url
        # F-09: per-origin cookie jars. Cookies set by origin A are never
        # replayed to a different origin B down the redirect chain (classic
        # cross-origin leakage), and merging preserves cookies across hops
        # on the SAME origin (the old replace semantics dropped them).
        cookie_jars: dict[str, dict[str, str]] = {}

        def _jar_for(u: str) -> dict[str, str]:
            host = _host_of(u)
            return cookie_jars.setdefault(host, {})

        # Seed with caller-provided cookies for the ORIGINAL host only.
        if cookies_header:
            for pair in cookies_header.split(";"):
                if "=" in pair:
                    k, v = pair.strip().split("=", 1)
                    _jar_for(url)[k] = v
        try:
            for _hop in range(DEFAULT_MAX_REDIRECTS + 1):
                r = cffi_req.get(
                    current_url,
                    impersonate=profile,
                    headers=prof_headers,
                    timeout=15,
                    allow_redirects=False,
                )
                # Merge set-cookie into THIS origin's jar; replay only that jar.
                if hasattr(r, "cookies") and r.cookies:
                    jar = _jar_for(current_url)
                    for k, v in r.cookies.items():
                        jar[k] = v
                jar = _jar_for(current_url)
                if jar:
                    prof_headers["Cookie"] = "; ".join(
                        f"{k}={v}" for k, v in jar.items()
                    )
                else:
                    prof_headers.pop("Cookie", None)

                # Redirect? Re-validate the target before following.
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location") or r.headers.get("location")
                    if not loc:
                        # F-32: malformed/challenge-style redirect (no Location).
                        # CF-shaped challenges expect a same-URL retry carrying
                        # the just-harvested cookies (merged above), not chain
                        # abandonment. Bounded by the hop budget below.
                        log.debug(
                            "curl_cffi_redirect_no_location",
                            url=current_url[:60],
                            profile=profile,
                            hop=_hop,
                        )
                        continue
                    new_url = urljoin(current_url, loc)
                    # DNS-rebinding guard: re-resolve + re-check IP policy.
                    resolver.validate_redirect_target(new_url)
                    current_url = new_url
                    continue

                # Non-redirect response. Check Content-Length budget pre-accept.
                cl = r.headers.get("Content-Length") or r.headers.get("content-length")
                if cl:
                    try:
                        cl_int = int(cl)
                        if cl_int > DEFAULT_MAX_RESPONSE_BYTES:
                            raise BudgetExceeded(
                                "MAX_RESPONSE_BYTES",
                                DEFAULT_MAX_RESPONSE_BYTES,
                                cl_int,
                                current_url,
                            )
                    except ValueError:
                        pass  # malformed CL — rely on post-download check

                content_type = ""
                try:
                    content_type = (
                        r.headers.get("Content-Type", "")
                        or r.headers.get("content-type", "")
                        or ""
                    )
                except Exception:
                    pass

                if r.status_code == 200:
                    body = r.text or ""
                    # Post-download size check catches a lying Content-Length.
                    if (
                        len(body.encode("utf-8", errors="ignore"))
                        > DEFAULT_MAX_RESPONSE_BYTES
                    ):
                        raise BudgetExceeded(
                            "MAX_RESPONSE_BYTES",
                            DEFAULT_MAX_RESPONSE_BYTES,
                            len(body),
                            current_url,
                        )
                    if len(body) > 500:
                        log.info(
                            "curl_cffi_bypass_ok",
                            url=url[:60],
                            final_url=current_url[:60],
                            profile=profile,
                            chars=len(body),
                            content_type=content_type.split(";")[0][:60],
                        )
                        return body, headers.get("Cookie", ""), content_type
                break  # non-200 terminal — try next profile
        except (InvalidURL, SSRFBlocked, BudgetExceeded):
            raise  # policy errors propagate immediately, no retry
        except Exception as ex:
            log.debug(
                "curl_cffi_error",
                url=url[:60],
                profile=profile,
                error=str(ex)[:80],
            )
    return None, cookies_header, ""


# Stealth JS to inject into Playwright (hides automation signals)
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {
    get: () => [{name: 'Chrome PDF Plugin'}, {name: 'Chrome PDF Viewer'},
                {name: 'Native Client'}]
});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = window.chrome || {runtime: {}};
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : origQuery(p);
Object.defineProperty(navigator, 'platform', {get: () => 'Linux x86_64'});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
// WebGL: headless defaults (SwiftShader/llvmpipe) are a free bot tell.
// Spoof the debug-renderer pair with a real Linux integrated-GPU identity
// consistent with navigator.platform above.
(() => {
    const spoof = (proto) => {
        const orig = proto.prototype.getParameter;
        proto.prototype.getParameter = function (p) {
            if (p === 0x9245) return 'Intel';                                  // UNMASKED_VENDOR_WEBGL
            if (p === 0x9246) return 'Mesa Intel(R) UHD Graphics 620 (CFL GT2)'; // UNMASKED_RENDERER_WEBGL
            return orig.apply(this, arguments);
        };
    };
    if (typeof WebGLRenderingContext !== 'undefined') spoof(WebGLRenderingContext);
    if (typeof WebGL2RenderingContext !== 'undefined') spoof(WebGL2RenderingContext);
})();
// Canvas noise: sparse RANDOM pixel jitter (Brave-style farbling) makes each
// canvas read unique, defeating stable canvas fingerprint hashes while
// leaving rendered content visually identical (~0.03% of samples, +-1).
(() => {
    const origGet = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function (...args) {
        const img = origGet.apply(this, args);
        for (let i = 3; i < img.data.length; i += 9973) {  // alpha channel only
            img.data[i] = (img.data[i] + ((Math.random() * 3) | 0) - 1) & 0xff;
        }
        return img;
    };
    const origToData = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function (...args) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const v = (Math.random() * 7) | 0;
            ctx.fillStyle = 'rgba(' + v + ',' + v + ',' + v + ',0.02)';
            ctx.fillRect(0, 0, 1, 1);
        }
        return origToData.apply(this, args);
    };
})();
"""

# Per-domain cookie store (cf_clearance persistence — avoid re-solving challenge)
_cf_cookies: dict[str, str] = {}


# ── Authenticated-fetch wallet (2026-09-03) ────────────────────────────
# Per-host USER-SUPPLIED session cookies, the pro answer to login-walled
# hosts (ResearchGate et al.): drop a cookie-header line at
#   ~/.config/geokit/websearch/cookies.d/<host>.txt     (e.g. researchgate.net.txt)
# and every fetch for that host merges it into the cookie tier FIRST —
# curl_cffi chrome impersonation + a real session usually renders the
# page that anonymous datacenter requests never see. No paid proxy, no
# scraping service; the user's own account, the user's own access.
_WALLET_DIR = Path.home() / ".config" / "geokit" / "websearch" / "cookies.d"

# Wayback serves this banner when no snapshot exists for the URL.
_WAYBACK_EMPTY_MARKERS = frozenset({"Capture a web page", "Search the history of over"})

# Single source of truth for the Chrome major version we advertise in
# browser-fingerprint surfaces: Sec-Ch-Ua headers (curl_cffi), the
# Playwright UA string, and the impersonation rotation below. WS9 found
# drift (headers 120, TLS 124, UA 120-stale); Wave 6 removes the drift
# ROOT by deriving the major from the ACTUAL installed Playwright Chromium
# binary, so the fingerprint never goes stale again. Precedence:
#   1. WEB_SEARCH_CHROME_UA_MAJOR env pin (explicit override, validated)
#   2. `<chromium-binary> --version` parse, mtime-keyed cache under
#      DEFAULT_CACHE_DIR (spawn fires once per binary update, not per run)
#   3. _CHROME_UA_FALLBACK_MAJOR (last-known-good constant)
_CHROME_UA_FALLBACK_MAJOR = 131

_RE_CHROME_VERSION = re.compile(r"Chrom(?:e|ium)\D+(\d{1,3})", re.IGNORECASE)


def _parse_chrome_major(version_output: str) -> int | None:
    """'Google Chrome for Testing 151.0.7922.34' -> 151; None if unparseable."""
    m = _RE_CHROME_VERSION.search(version_output or "")
    return int(m.group(1)) if m else None


def _find_playwright_chromium() -> Path | None:
    """Newest chrome binary across Playwright browser roots. Handles the
    chrome-linux/ and chrome-linux64/ layouts plus macOS/Windows names via
    PLAYWRIGHT_BROWSERS_PATH and the default ~/.cache/ms-playwright."""
    bases: list[Path] = []
    env_base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_base:
        bases.append(Path(env_base))
    bases.append(Path.home() / ".cache" / "ms-playwright")
    best: tuple[int, Path] | None = None
    for base in bases:
        if not base.is_dir():
            continue
        for name in ("chrome", "chrome.exe"):
            for candidate in base.glob(f"chromium-*/chrome-*/{name}"):
                if not candidate.is_file():
                    continue
                mtime = int(candidate.stat().st_mtime)
                if best is None or mtime > best[0]:
                    best = (mtime, candidate)
    return best[1] if best else None


def _derive_chrome_ua_major() -> int:
    # 1. explicit pin wins (fail fast on garbage per H1)
    pin = os.environ.get("WEB_SEARCH_CHROME_UA_MAJOR", "").strip()
    if pin:
        try:
            value = int(pin)
        except ValueError:
            raise ValueError(
                f"WEB_SEARCH_CHROME_UA_MAJOR must be an integer, got {pin!r}"
            ) from None
        if not 60 <= value <= 300:
            raise ValueError(
                f"WEB_SEARCH_CHROME_UA_MAJOR out of plausible range: {value}"
            )
        return value
    # 2. mtime-keyed derivation from the installed binary
    cache_file = DEFAULT_CACHE_DIR / "chrome_ua_major.json"
    try:
        binary = _find_playwright_chromium()
        if binary is not None:
            mtime = int(binary.stat().st_mtime)
            try:
                cached = json.loads(cache_file.read_text())
            except Exception:
                cached = None
            if (
                isinstance(cached, dict)
                and cached.get("exe") == str(binary)
                and cached.get("mtime") == mtime
                and isinstance(cached.get("major"), int)
            ):
                return cached["major"]
            proc = subprocess.run(
                [str(binary), "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            major = _parse_chrome_major(proc.stdout or "") or _parse_chrome_major(
                proc.stderr or ""
            )
            if major is not None:
                try:
                    DEFAULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(
                        json.dumps({"exe": str(binary), "mtime": mtime, "major": major})
                    )
                except Exception:
                    pass  # cache-write failure must never break fingerprinting
                return major
            log.debug("chrome_version_unparseable", out=(proc.stdout or "")[:60])
    except Exception as ex:
        log.debug("chrome_ua_derive_failed", error=str(ex)[:80])
    # 3. last-known-good
    return _CHROME_UA_FALLBACK_MAJOR


_CHROME_UA_MAJOR = _derive_chrome_ua_major()

# Impersonation rotation: introspect the installed curl_cffi for the chrome
# profiles CLOSEST to (not exceeding) the advertised major, then keep
# browser-family diversity. Live basis: curl_cffi 9.x ships chrome99..146;
# hardcoding chrome120/124 while advertising 151 re-creates the WS9 drift.
_IMPERSONATION_FALLBACK = ["chrome120", "chrome124", "firefox133", "safari17_2_ios"]
_chrome_profiles_cache: list[tuple[int, str]] | None = None


def _available_chrome_profiles() -> list[tuple[int, str]]:
    """[(major, name)] of desktop chrome impersonations in the installed
    curl_cffi — introspected at runtime, survives library upgrades."""
    global _chrome_profiles_cache
    if _chrome_profiles_cache is not None:
        return _chrome_profiles_cache
    found: list[tuple[int, str]] = []
    try:
        import typing as _typing

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        for name in _typing.get_args(BrowserTypeLiteral):
            m = re.fullmatch(r"chrome(\d+)", name)
            if m:
                found.append((int(m.group(1)), name))
    except Exception as ex:
        log.debug("impersonation_introspect_failed", error=str(ex)[:80])
    _chrome_profiles_cache = sorted(set(found))
    return _chrome_profiles_cache


def _impersonation_rotation() -> list[str]:
    """Top-2 closest-supported chrome profiles first (max TLS/UA coherence),
    then firefox/safari for rotation diversity. Falls back to the historic
    hardcoded list when introspection yields nothing."""
    eligible = [
        name
        for major, name in _available_chrome_profiles()
        if major <= _CHROME_UA_MAJOR
    ]
    if not eligible:
        return list(_IMPERSONATION_FALLBACK)
    eligible.sort(key=lambda n: -int(re.search(r"(\d+)", n).group(1)))
    return eligible[:2] + ["firefox133", "safari17_2_ios"]


# JS-render retry (WS9): a clean HTML payload this large with ZERO
# extractable text is the signature of a JS-only SPA shell — the markup is
# divs/scripts, content arrives via scripts. Thresholds bound the retry.
_JS_RENDER_MIN_HTML = 20_000
_JS_RENDER_MIN_TEXT = 200


def wallet_cookie(host: str) -> str:
    """Cookie-header string from the wallet for this host ('' = none).

    Accepts a raw header line ("k=v; k2=v2") or Netscape cookie-file
    lines (tab-separated: domain, flag, path, secure, expiry, name, value
    — exported by browser extensions); Netscape rows are joined into
    name=value pairs. Unparseable files are ignored loudly at debug.
    """
    key = host.removeprefix("www.")
    f = _WALLET_DIR / f"{key}.txt"
    if not f.is_file():
        return ""
    try:
        text = f.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as ex:
        log.debug("wallet_read_failed", host=host, error=str(ex)[:60])
        return ""
    if not text:
        return ""
    if "\t" in text or "\t" in text:
        pairs = [
            f"{parts[5]}={parts[6]}"
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
            for parts in [line.split("\t")]
            if len(parts) >= 7
        ]
        return "; ".join(pairs)
    return text.splitlines()[0].strip()


_cf_cookies_lock = threading.Lock()


def _get_cf_cookies(host: str) -> str:
    with _cf_cookies_lock:
        return _cf_cookies.get(host, "")


def _save_cf_cookies(host: str, cookie_header: str) -> None:
    if not cookie_header:
        return
    with _cf_cookies_lock:
        old = _cf_cookies.get(host, "")
        # Merge: new cookies override old ones with same name
        old_pairs = dict(p.strip().split("=", 1) for p in old.split(";") if "=" in p)
        new_pairs = dict(
            p.strip().split("=", 1) for p in cookie_header.split(";") if "=" in p
        )
        old_pairs.update(new_pairs)
        _cf_cookies[host] = "; ".join(f"{k}={v}" for k, v in old_pairs.items())


def _human_delay() -> None:
    """Random delay simulating human browsing pace (1-3.5s)."""
    time.sleep(random.uniform(1.0, 3.5))


_PROFILE_DIR = Path.home() / ".cache" / "geokit" / "websearch" / "profiles"


def _profile_for(host: str) -> Path | None:
    """Persistent-browser profile dir for this host (one-time login)."""
    d = _PROFILE_DIR / host.removeprefix("www.")
    return d if d.is_dir() else None


def host_login(host: str, timeout_s: int = 300) -> int:
    """Open a HEADED browser for a one-time manual login on *host*.

    The pro pattern for login-walled hosts (ResearchGate): solve the
    wall ONCE as a human; the persistent profile keeps the session and
    clearance cookies, and later headless fetches launch the same
    profile (see _fetch_playwright). Run:
        python -m geokit.websearch host-login researchgate.net
    """
    import webbrowser as _wb

    url = f"https://{host.removeprefix('www.')}/"
    target = _PROFILE_DIR / host.removeprefix("www.")
    target.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed — cannot run host-login")
        return 1
    print(f"host-login: opening {url} (profile: {target})")
    print("Log in / solve the challenge in the window, then close it.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(target),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        print(f"Waiting up to {timeout_s}s for you to finish (close the window)...")
        try:
            page.wait_for_event("close", timeout=timeout_s * 1000)
        except Exception:
            pass
        ctx.close()
    print(f"Profile saved: {target}")
    print(f"Later fetches of {host} will reuse it automatically.")
    return 0


def _challenge_max_attempts() -> int:
    """F4: bounded bot-challenge re-solve attempts (§5 H16 explicit policy).

    WEB_SEARCH_CHALLENGE_ATTEMPTS (default 2, clamped 0-5). 0 disables the
    re-solve loop entirely (single-settle legacy behaviour).
    """
    raw = os.environ.get("WEB_SEARCH_CHALLENGE_ATTEMPTS", "2").strip()
    try:
        return max(0, min(5, int(raw)))
    except ValueError:
        return 2


def _fetch_playwright(url: str, cookies_header: str = "") -> tuple[str | None, str]:
    """Headless browser with stealth patches — solves Cloudflare JS challenges.

    SSRF-safe: validates the URL before launch AND installs a page.route()
    interceptor that aborts any sub-request (asset, XHR, iframe, JS-driven
    redirect) whose resolved IP is forbidden. Defeats DNS rebinding inside
    the browser context.

    Injects stealth JS to hide automation signals (navigator.webdriver, etc).
    Captures cf_clearance cookies for reuse by curl_cffi on subsequent pages.
    Returns (html, content_type). content_type may be empty if unavailable.
    Raises InvalidURL / SSRFBlocked on policy violation.
    """
    from playwright.sync_api import sync_playwright  # hard dep

    # Pre-launch validation — fail before opening a browser if the URL is bad.
    resolver = get_safe_url_resolver()
    resolver.resolve_and_validate(url)

    def _block_ssrf(route) -> None:
        """page.route() handler — intercepts every browser request and aborts
        any whose host resolves to a forbidden IP. Catches JS-driven redirects
        and asset loads to internal/metadata endpoints."""
        try:
            req_url = route.request.url
            # Data/blob/about schemes don't need IP validation.
            parsed = urlparse(req_url)
            if parsed.scheme not in _ALLOWED_SCHEMES:
                route.continue_()
                return
            host = (parsed.hostname or "").lower()
            if not host or host in resolver._trusted:
                route.continue_()
                return
            if host in _METADATA_HOSTS:
                log.warning(
                    "playwright_ssrf_blocked", url=req_url[:80], reason="metadata host"
                )
                route.abort("blockedbyclient")
                return
            ips = resolver._resolve(host)
            if not ips:
                # DNS failure mid-render — abort this sub-request only.
                route.abort("connectionrefused")
                return
            for ip in ips:
                blocked, reason = _check_ip_forbidden(ip)
                if blocked:
                    log.warning(
                        "playwright_ssrf_blocked", url=req_url[:80], reason=reason
                    )
                    route.abort("blockedbyclient")
                    return
            route.continue_()
        except SearchError as policy_ex:
            # Policy evaluation itself failed mid-render — FAIL CLOSED (F-08):
            # a sub-request we cannot validate must not be allowed through.
            log.warning(
                "playwright_route_policy_error",
                url=str(route.request.url)[:80],
                error=str(policy_ex)[:80],
            )
            route.abort("blockedbyclient")
        except Exception:
            # Non-policy handler bug — still fail closed for safety.
            route.abort("blockedbyclient")

    browser = None
    try:
        with sync_playwright() as p:
            # Persistent profile (login-walled hosts): one-time `host-login`
            # created a real session; reuse it so the wall recognizes us.
            _prof = _profile_for(_host_of(url))
            if _prof is not None:
                context = p.chromium.launch_persistent_context(
                    str(_prof),
                    headless=True,
                    viewport={"width": 1920, "height": 1080},
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
                browser = context  # persistent context closes like a browser
                _persistent_ctx = True
            else:
                _persistent_ctx = False
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{_CHROME_UA_MAJOR}.0.0.0 "
                    f"Safari/537.36"
                ),
                locale="en-US",
            )
            # Inject stealth patches before any page script runs
            context.add_init_script(_STEALTH_JS)
            page = context.new_page()
            # SSRF interceptor — every browser request is policy-checked.
            page.route("**/*", _block_ssrf)

            # Load existing cookies if available
            if cookies_header:
                cookie_list = []
                for pair in cookies_header.split(";"):
                    if "=" in pair:
                        name, value = pair.strip().split("=", 1)
                        cookie_list.append(
                            {
                                "name": name,
                                "value": value,
                                "domain": "." + _host_of(url),
                                "path": "/",
                            }
                        )
                if cookie_list:
                    try:
                        context.add_cookies(cookie_list)
                    except Exception:
                        pass

            # domcontentloaded, NOT networkidle (2026-09-03 live: S2's
            # analytics/long-poll traffic held networkidle for the FULL
            # 60 s budget per URL; T&F/RG likewise). DOM-ready + the 3 s
            # challenge settle below is enough for the extractor; thin
            # results still fall through to later cascade tiers.
            resp = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(min(30, DEFAULT_MAX_BROWSER_TIME_S) * 1000),
            )
            # Best-effort settle (2026-09-03): some hosts (S2) serve a JS
            # challenge at DOM-ready and render real content only after
            # the network quiets. Bounded 8s wait_for_load_state — skipped
            # entirely when already idle; never the 60s networkidle goto.
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            # Wait for Cloudflare challenge to resolve
            time.sleep(3)
            html = page.content()
            # F4: post-challenge verify/re-solve loop. A single settle used to
            # be the whole story — if the page still smelled like a challenge
            # the layer silently returned blocked HTML and the cascade moved
            # on. Now bounded re-solve attempts: poll content, and if still
            # blocked force a reload (fresh navigation often picks up the
            # cf_clearance cookie captured mid-flight). Explicit policy per
            # §5 H16: max attempts from env, clamp 0-5, zero hidden retries.
            attempts = 0
            max_attempts = _challenge_max_attempts()
            while _is_bot_blocked(html) and attempts < max_attempts:
                attempts += 1
                time.sleep(4)
                html = page.content()
                if _is_bot_blocked(html):
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=15000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass
                        time.sleep(3)
                    except Exception:
                        pass
                    html = page.content()
            if attempts:
                log.info(
                    "playwright_resolve_attempts",
                    url=url[:60],
                    attempts=attempts,
                    resolved=not _is_bot_blocked(html),
                )
            content_type = ""
            try:
                if resp is not None:
                    content_type = resp.headers.get("content-type", "") or ""
            except Exception:
                pass

            # Capture cookies for reuse
            pw_cookies = context.cookies()
            if pw_cookies:
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in pw_cookies)
                _save_cf_cookies(_host_of(url), cookie_str)

            if html and len(html) > 500:
                # Post-fetch size check — guard against pathological pages.
                if len(html.encode("utf-8", errors="ignore")) > DEFAULT_MAX_HTML_BYTES:
                    raise BudgetExceeded(
                        "MAX_HTML_BYTES", DEFAULT_MAX_HTML_BYTES, len(html), url
                    )
                log.info(
                    "playwright_bypass_ok",
                    url=url[:60],
                    chars=len(html),
                    cookies_captured=len(pw_cookies),
                    content_type=content_type.split(";")[0][:60],
                )
                return html, content_type
    except (InvalidURL, SSRFBlocked, BudgetExceeded):
        # Policy errors are real results — never mask them as transport
        # failures (F-08b). Propagate to the caller/cascade boundary.
        raise
    except Exception as ex:
        log.warning("playwright_failed", url=url[:60], error=str(ex)[:80])
    finally:
        # F-11: always close the browser — a goto timeout previously leaked
        # a live Chromium process per failure.
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    return None, ""


# --------------------------------------------------------------------------- #
# SOTA markdown extraction cascade (hard deps)
#   trafilatura (#1 in ScrapingHub + Webis ROUGE benchmarks, full preservation)
#   > crawl4ai raw_markdown (handles JS-rendered / non-article pages)
# All extractors are hard deps — _check_required_deps() fails fast if missing.
# WEB_SEARCH_EXTRACTOR=auto picks best output per page.
# --------------------------------------------------------------------------- #


def _extract_trafilatura(html: str, url: str) -> str:
    """trafilatura — SOTA article extractor (#1 in ScrapingHub + Webis ROUGE).
    Hard dep. Preserves links, formatting, tables, images."""
    from trafilatura import extract  # hard dep

    try:
        return (
            extract(
                html,
                url=url or None,
                output_format="markdown",
                include_links=True,
                include_formatting=True,
                include_tables=True,
                include_images=True,
                include_comments=False,
                with_metadata=False,
            )
            or ""
        )
    except Exception as ex:
        log.debug("trafilatura_failed", error=str(ex)[:80])
        return ""


def _extract_readability(html: str) -> str:
    """readability-lxml — Mozilla's Readability port. Strips navigation, ads,
    scripts, sidebars; returns just the article HTML. Used as a PRE-PROCESSOR
    before trafilatura on pages where trafilatura alone fails (JS-heavy
    WordPress, Thrive Architect, landing pages). Same algorithm Firefox Reader
    Mode uses. Hard dep."""
    from readability import Document  # hard dep

    try:
        doc = Document(html)
        return doc.summary()  # returns cleaned article HTML
    except Exception as ex:
        log.debug("readability_failed", error=str(ex)[:80])
        return ""


def _html_to_text_fallback(html: str) -> str:
    """Last-resort: html2text converts ANY HTML to readable plain text.
    Used when trafilatura + crawl4ai + readability all fail to produce clean
    markdown. Better than returning raw <!doctype html> to the agent."""
    import html2text  # hard dep

    try:
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.ignore_emphasis = False
        h.body_width = 0  # no line wrapping
        h.unicode_snob = True
        return h.handle(html)
    except Exception as ex:
        log.debug("html2text_failed", error=str(ex)[:80])
        return ""


def _extract_crawl4ai_md(html: str, url: str) -> str:
    """crawl4ai markdown generation. raw_markdown by default (full content).
    Set WEB_SEARCH_CRAWL4AI_FIT=1 for BM25-pruned fit_markdown (noise removal).
    Hard dep."""
    from crawl4ai.markdown_generation_strategy import (
        DefaultMarkdownGenerator,
    )  # hard dep

    try:
        gen = DefaultMarkdownGenerator()
        result = gen.generate_markdown(input_html=html, url=url)
        if os.environ.get("WEB_SEARCH_CRAWL4AI_FIT", "0") == "1":
            return getattr(result, "fit_markdown", "") or ""
        return getattr(result, "raw_markdown", "") or ""
    except Exception as ex:
        log.debug("crawl4ai_md_failed", error=str(ex)[:80])
        return ""


def _html_to_markdown(html: str, url: str = "") -> str:
    """SOTA markdown extraction cascade:
      1. readability-lxml → trafilatura (CLEANEST: strips nav/TOC/bios/ads,
         then extracts article body — same combo Firefox Reader Mode + #1
         academic extractor)
      2. trafilatura on raw HTML (fallback if readability strips too much —
         non-article pages where readability is too aggressive)
      3. crawl4ai raw_markdown (handles JS-rendered / structured pages)
      4. html2text last-resort (converts ANY HTML to readable text)
    All hard deps. WEB_SEARCH_EXTRACTOR=auto|trafilatura|crawl4ai to force."""
    if not html:
        return ""
    choice = os.environ.get("WEB_SEARCH_EXTRACTOR", "auto").lower().strip()

    if choice == "trafilatura":
        return _extract_trafilatura(html, url)
    if choice == "crawl4ai":
        return _extract_crawl4ai_md(html, url)

    # Layer 1: readability → trafilatura (CLEANEST — strips navigation,
    # TOC, author bios, review credits, ads; then extracts article body).
    cleaned = _extract_readability(html)
    if cleaned and len(cleaned) > 200:
        out = _extract_trafilatura(cleaned, url)
        if out and len(out) >= 200:
            return out

    # Layer 2: trafilatura on raw HTML (better for non-article pages where
    # readability's article-detector strips too much).
    out2 = _extract_trafilatura(html, url)
    if out2 and len(out2) >= 50:
        return out2

    # Layer 3: crawl4ai raw_markdown
    out3 = _extract_crawl4ai_md(html, url)
    if out3 and len(out3) >= 50:
        stripped = out3.lstrip().lower()[:20]
        if not (stripped.startswith("<!doctype") or stripped.startswith("<html")):
            return out3

    # Layer 4: html2text last-resort — better than raw HTML
    out4 = _html_to_text_fallback(html)
    if out4 and len(out4) >= 50:
        return out4

    return out2 or out3 or ""


# --------------------------------------------------------------------------- #
# Fetcher / Extractor Protocols — separate transport from parsing.
#   Enables deterministic fakes (FakeFetcher/FakeExtractor) for test injection
#   without touching the network. The extract cascade below is a thin runner
#   that tries fetchers in order and pipes successful HTML through the
#   extractor. This is the P1.2 split from the review (point #11).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FetchResult:
    """Output of a successful transport fetch. Carries raw HTML + metadata
    needed by the extractor and cookie-store layers."""

    html: str
    content_type: str
    cookies: str  # response Set-Cookie header (for cf_clearance reuse)
    source: str  # "curl_cffi" | "playwright" | "ddgs"


class Fetcher(Protocol):
    """Transport-only interface: fetch a URL → raw HTML. No parsing.
    Implementations: CurlCffiFetcher, PlaywrightFetcher, DdgsFetcher."""

    name: str

    def fetch(self, url: str, cookies: str = "") -> FetchResult | None:
        """Returns FetchResult on success, None on transport failure (the
        cascade tries the next fetcher). Raises SearchError subclass for
        policy violations (SSRFBlocked, BudgetExceeded) which propagate."""
        ...


class Extractor(Protocol):
    """Parse-only interface: raw HTML → markdown. No network.
    Implementations: CompositeExtractor (trafilatura+crawl4ai cascade)."""

    name: str

    def extract(self, html: str, url: str) -> str:
        """Returns markdown text (may be empty if extraction fails)."""
        ...


class CurlCffiFetcher:
    """Adapter wrapping _fetch_curl_cffi for the Fetcher Protocol."""

    name = "curl_cffi"

    def fetch(self, url: str, cookies: str = "") -> FetchResult | None:
        html, resp_cookies, ct = _fetch_curl_cffi(url, cookies_header=cookies)
        if html and len(html) > 500:
            return FetchResult(
                html=html,
                content_type=ct,
                cookies=resp_cookies or cookies,
                source=self.name,
            )
        return None


class PlaywrightFetcher:
    """Adapter wrapping _fetch_playwright for the Fetcher Protocol."""

    name = "playwright"

    def fetch(self, url: str, cookies: str = "") -> FetchResult | None:
        html, ct = _fetch_playwright(url, cookies_header=cookies)
        if html and len(html) > 500:
            return FetchResult(
                html=html, content_type=ct, cookies=cookies, source=self.name
            )
        return None


class DdgsFetcher:
    """Adapter wrapping ddgs.extract (primp TLS impersonation) for the Fetcher
    Protocol. Special: ddgs.extract returns processed content, not raw HTML —
    we treat its output as pre-extracted markdown and bypass the Extractor."""

    name = "ddgs"

    def __init__(self, ddgs: Any) -> None:
        self._ddgs = ddgs

    def fetch(self, url: str, cookies: str = "") -> FetchResult | None:
        try:
            extracted = self._ddgs.extract(url, fmt="text")
            html = str(extracted.get("content", ""))
            if html and len(html) > 500:
                # Mark as ddgs-source so the cascade knows this is pre-extracted.
                return FetchResult(
                    html=html, content_type="text/plain", cookies="", source=self.name
                )
        except Exception as ex:
            log.debug("ddgs_fetch_failed", url=url[:60], error=str(ex)[:80])
        return None


class CompositeExtractor:
    """Adapter wrapping _html_to_markdown (trafilatura → crawl4ai cascade)
    for the Extractor Protocol."""

    name = "composite"

    def extract(self, html: str, url: str) -> str:
        return _html_to_markdown(html, url)


@dataclass
class ExtractCascade:
    """Ordered layered extraction: cookie-reuse → ddgs → curl_cffi → playwright.
    Each successful fetch pipes through the extractor (unless the fetcher is
    ddgs, which pre-extracts). Bot-block + binary-content checks at each layer.
    This is the parameterizable heart of _extract_with_fallback_uncached —
    tests inject FakeFetcher lists to exercise failure modes deterministically."""

    fetchers: list[Fetcher]
    extractor: Extractor
    cookie_store: Any  # object with get(host)->str and save(host, str)->None

    def run(self, url: str, host: str) -> str:
        """Execute the cascade. Returns markdown text. Raises BinaryDocument
        on non-HTML content, FetchBlocked when all layers exhaust."""
        cached_cookies = self.cookie_store.get(host)
        # Wallet cookies (user-supplied session for login-walled hosts)
        # take precedence and MERGE with any cached clearance cookies.
        _wallet = wallet_cookie(host)
        if _wallet:
            merged = _wallet
            if cached_cookies:
                _wn = {
                    c.split("=", 1)[0].strip() for c in _wallet.split(";") if "=" in c
                }
                merged = "; ".join(
                    [_wallet]
                    + [
                        c.strip()
                        for c in cached_cookies.split(";")
                        if "=" in c and c.split("=", 1)[0].strip() not in _wn
                    ]
                )
            if merged != cached_cookies:
                log.info(
                    "wallet_cookies_active", host=host, n_cookies=merged.count("=")
                )
                cached_cookies = merged

        # Layer 0: cookie reuse fast path with the first non-ddgs fetcher.
        if cached_cookies:
            for f in self.fetchers:
                if f.name == "ddgs":
                    continue
                result = f.fetch(url, cookies=cached_cookies)
                sniffed = _sniff_binary(result.html) if result else None
                if result and (_is_binary_content_type(result.content_type) or sniffed):
                    raise BinaryDocument(
                        url, sniffed or result.content_type or "binary"
                    )
                if result and not _is_bot_blocked(result.html):
                    text = self._extract_or_passthrough(result, url)
                    if text:
                        log.info(
                            "cookie_reuse_success",
                            url=url[:60],
                            chars=len(text),
                            source=f.name,
                        )
                        return text
            log.info("cached_cookies_expired_or_insufficient", url=url[:60])

        # Layers 1..N: each fetcher in order.
        js_candidate: FetchResult | None = None  # WS9: big clean shell, no text
        best_short: str | None = None  # WS9.1: stub text kept as last resort
        for f in self.fetchers:
            log.info("trying_fetcher", fetcher=f.name, url=url[:60])
            result = f.fetch(url, cookies=cached_cookies)
            if not result:
                continue
            sniffed = _sniff_binary(result.html)
            if _is_binary_content_type(result.content_type) or sniffed:
                # F-02: body magic overrides a lying/fabricated Content-Type.
                raise BinaryDocument(url, sniffed or result.content_type or "binary")
            if _is_bot_blocked(result.html):
                log.warning("bot_block_detected", fetcher=f.name, url=url[:60])
                continue
            text = self._extract_or_passthrough(result, url)
            text_len = len((text or "").strip())
            # Playwright is terminal: its output is already JS-rendered, so
            # even short text from it is the page's genuine content.
            if text_len >= _JS_RENDER_MIN_TEXT or (
                text_len > 0 and f.name == "playwright"
            ):
                if result.cookies and f.name != "ddgs":
                    self.cookie_store.save(host, result.cookies)
                log.info(
                    "fetcher_success",
                    fetcher=f.name,
                    url=url[:60],
                    html_chars=len(result.html),
                    text_chars=len(text),
                )
                return text
            if text_len > 0:
                # WS9.1: sub-threshold stub text (noscript boilerplate, "enable
                # JavaScript" SPA shells) is NOT terminal — a later layer or
                # the JS-render retry may still produce real content. Keep the
                # first stub as a best-effort fallback and keep escalating.
                if best_short is None:
                    best_short = text
                if result.cookies and f.name != "ddgs":
                    self.cookie_store.save(host, result.cookies)
                log.info(
                    "short_text_not_terminal",
                    fetcher=f.name,
                    url=url[:60],
                    text_chars=text_len,
                    floor=_JS_RENDER_MIN_TEXT,
                )
            if (
                f.name not in ("playwright", "ddgs")
                and len(result.html) >= _JS_RENDER_MIN_HTML
            ):
                # Clean, large HTML that nothing (or only stub text) came out
                # of — classic JS-only SPA shell. Remember it for the retry.
                js_candidate = result

        # JS-RENDER RETRY (WS9): one bounded longer-chance Playwright pass
        # before declaring the URL dead. Value over the regular Playwright
        # layer: transient launch failures get a second chance, and a shell
        # that rendered-but-empty on the first pass may succeed on a fresh
        # navigation. Explicit retry policy (§5 H16): exactly one attempt,
        # logged, never silent.
        if js_candidate is not None:
            for f in self.fetchers:
                if f.name != "playwright":
                    continue
                log.info(
                    "js_render_retry",
                    url=url[:60],
                    shell_chars=len(js_candidate.html),
                    reason="large clean html shell, no extractable text",
                )
                try:
                    result = f.fetch(url, cookies=cached_cookies)
                except Exception:
                    break
                if result and not _is_bot_blocked(result.html):
                    text = self._extract_or_passthrough(result, url)
                    if text and len(text) >= _JS_RENDER_MIN_TEXT:
                        if result.cookies:
                            self.cookie_store.save(host, result.cookies)
                        log.info(
                            "js_render_retry_success",
                            url=url[:60],
                            text_chars=len(text),
                        )
                        return text
                break  # one attempt only

        # WS9.1: nothing beat the stub — return the best short text we saw
        # rather than declaring failure (a 57-char real stub still beats a
        # hard error when every escalation came up empty).
        if best_short is not None:
            log.info(
                "returning_short_text_best_effort",
                url=url[:60],
                text_chars=len(best_short),
            )
            return best_short

        # WAYBACK FALLBACK (2026-09-03): before declaring the URL dead,
        # try the Internet Archive's snapshot of it — revives dead links
        # (DNS failures like geosocindia.org), removed pages, and some
        # paywalled/blocked hosts. Not ResearchGate (RG publication pages
        # are excluded from the archive — verified), but it costs one
        # cheap request when everything else already failed.
        wayback_url = f"https://web.archive.org/web/2/{url}"
        for f in self.fetchers:
            try:
                result = f.fetch(wayback_url, cookies="")
            except Exception:
                continue
            if not result:
                continue
            if _is_bot_blocked(result.html) or _WAYBACK_EMPTY_MARKERS & {
                m
                for m in ("Capture a web page", "Search the history of over")
                if m in result.html
            }:
                continue  # no-snapshot banner page — not the target content
            text = self._extract_or_passthrough(result, wayback_url)
            if text and len(text) > 300:
                log.info(
                    "wayback_fallback_success",
                    url=url[:60],
                    chars=len(text),
                    fetcher=f.name,
                )
                return f"[Internet Archive snapshot]\n\n{text}"

        # All layers exhausted (including Wayback).
        raise FetchBlocked(
            f"All fetch layers exhausted for {url}. Tried: "
            + ", ".join(f.name for f in self.fetchers)
            + " + wayback"
        )

    def _extract_or_passthrough(self, result: FetchResult, url: str) -> str:
        """Fetched content pipes through the Extractor, with one refinement
        (F-26): ddgs.extract(fmt='text') often returns genuinely PLAIN TEXT
        (already-extracted article body, zero HTML tags). Running that through
        the HTML cascade wastes two extractor passes and can mangle it. If the
        payload contains no markup at all, pass it through untouched. When it
        DOES contain markup (ddgs fell back to raw HTML), the cascade runs —
        that guarantees no <!doctype html> ever reaches the output."""
        if result.source == "ddgs" and "<" not in result.html[:2000]:
            return result.html
        text = self.extractor.extract(result.html, url)
        # If the extractor returned raw HTML (starts with <!doctype or <html),
        # the page defeated both trafilatura AND crawl4ai — try html2text as a
        # last-resort plain-text conversion so the agent gets SOMETHING readable.
        if text and len(text) > 200:
            stripped = text.lstrip().lower()[:20]
            if stripped.startswith("<!doctype") or stripped.startswith("<html"):
                text = _html_to_text_fallback(result.html) or text
        return text


class _CookieStoreAdapter:
    """Adapter so ExtractCascade can use the module-level _cf_cookies dict
    via a get/save interface (matches the Protocol test fakes implement)."""

    @staticmethod
    def get(host: str) -> str:
        return _get_cf_cookies(host)

    @staticmethod
    def save(host: str, cookies: str) -> None:
        _save_cf_cookies(host, cookies)


def _build_default_cascade(ddgs: Any) -> ExtractCascade:
    """Construct the production fetcher chain: ddgs → curl_cffi → playwright.
    Extractor is the composite trafilatura+crawl4ai cascade."""
    return ExtractCascade(
        fetchers=[DdgsFetcher(ddgs), CurlCffiFetcher(), PlaywrightFetcher()],
        extractor=CompositeExtractor(),
        cookie_store=_CookieStoreAdapter(),
    )


def _extract_with_fallback(ddgs, url: str, *, proxy_for_log: str = "<none>") -> str:
    """Layered extract with cookie persistence + per-URL TTL cache.

    Caches positive results (markdown) and negative results (failures) per URL.
    Reuses _cache_* infrastructure (TTL: WEB_SEARCH_CACHE_TTL / WEB_SEARCH_NEG_CACHE_TTL).
    Set WEB_SEARCH_NO_CACHE=1 to bypass.
    """
    # SSRF guard — MUST fire before cache lookup so a neg-cached bad URL still
    # surfaces SSRFBlocked (not a misleading "cached failure" message) and so
    # forbidden URLs never pollute the cache.
    get_safe_url_resolver().resolve_and_validate(url)

    cache_key = _cache_key("extract", {"url": canonicalize_url(url)})
    if DEFAULT_CACHE_ENABLED:
        cached, status = _cache_read(
            cache_key, DEFAULT_CACHE_TTL, DEFAULT_NEG_CACHE_TTL
        )
        if cached is not None and status == "fresh":
            content = cached.get("content", "")
            sniffed = _sniff_binary(content)
            if sniffed:
                # Live-observed (2026-08-21): pre-F-02-era entries can hold raw
                # binary (arxiv PDF served as 5.4M-char "markdown"). Cache hits
                # must meet the same binary bar as fresh fetches — evict + refetch.
                log.warning(
                    "extract_cache_binary_evicted",
                    url=url[:60],
                    content_type=sniffed,
                    chars=len(content),
                )
                _cache_delete(cache_key)
            else:
                log.info(
                    "extract_cache_hit",
                    url=url[:60],
                    chars=len(content),
                )
                return content
        if cached is not None and status == "neg":
            raise ExtractionFailed(
                f"cached failure for {url} (negative cache, retry in {DEFAULT_NEG_CACHE_TTL}s)"
            )

    try:
        text = _extract_with_fallback_uncached(ddgs, url, proxy_for_log=proxy_for_log)
    except Exception as ex:
        _maybe_neg_cache(cache_key, ex)
        raise

    if DEFAULT_CACHE_ENABLED and text:
        _cache_write(cache_key, {"content": text})
    return text


def _maybe_neg_cache(cache_key: str, ex: Exception) -> None:
    """F-14: only PERSISTENT per-URL failures enter the negative cache.
    FetchBlocked (all layers exhausted) and BinaryDocument (non-HTML content)
    are properties of the URL itself. Timeouts/DNS blips/rate limits are
    transient — caching them poisoned URLs for 30 min on a single network
    hiccup (observed as a zero-extract pro run)."""
    if isinstance(ex, (FetchBlocked, BinaryDocument)) and DEFAULT_CACHE_ENABLED:
        _cache_write(cache_key, {"error": str(ex)[:200]}, negative=True)


def _extract_with_fallback_uncached(
    ddgs, url: str, *, proxy_for_log: str = "<none>"
) -> str:
    """Layered extract via the ExtractCascade (Fetcher/Extractor Protocols).

    Delegates the transport-vs-parse logic to ExtractCascade.run(), which
    tries fetchers in order (ddgs → curl_cffi → playwright) and pipes HTML
    through the composite extractor (trafilatura → crawl4ai). Cookie reuse
    fast-path fires first. SSRF guard + binary pre-check happen here at the
    entry boundary, before the cascade.

    Returns markdown content. Raises FetchBlocked (SearchError) if all layers
    exhaust. Raises BinaryDocument / SSRFBlocked / BudgetExceeded on the
    corresponding policy violations.
    """
    host = _host_of(url)

    # SSRF guard — validate resolved IP before any transport fires. Covers the
    # ddgs.extract (primp) Layer-1 path which bypasses curl_cffi/Playwright.
    # Fail-fast: bad URL → policy error, no fallback.
    get_safe_url_resolver().resolve_and_validate(url)

    # Pre-check: URL path ends with a known binary extension → don't waste the
    # HTML cascade (it would silently return empty/garbage on PDF bytes).
    inferred_ct = _binary_url_inference(url)
    if inferred_ct:
        log.info("binary_url_precheck", url=url[:80], content_type=inferred_ct)
        raise BinaryDocument(url, inferred_ct)

    cascade = _build_default_cascade(ddgs)
    return cascade.run(url, host)


def _extract_top_urls(
    ddgs,
    results,
    top_n,
    max_chars,
    query_terms,
    tier_budget: dict[int, int] | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    limiter = _get_rate_limiter()
    metrics = _get_metrics()
    # P2.5: optional semantic rerank (hybrid BM25 + cross-encoder). When
    # WEB_SEARCH_SEMANTIC_RERANK=1 and query is provided, results get a
    # hybrid_score field; the sort below prefers it. When OFF (default),
    # zero overhead — pass-through.
    if query:
        results = _semantic_rerank_if_enabled(query, results)

    # F2: relevance first, tier as tiebreaker. Old tier-first sort wasted all
    # extract slots on T0 spam (Hebrew Wikipedia, gov.in redirects, eportal
    # errors) while perfect T3 matches (cleartax.in ETF guide) never got
    # extracted. Prefer hybrid_score (semantic+bm25) > bm25_score > relevance.
    # F-49/F-50: bm25 rewards term-stuffed stats pages and SEO discovery pages
    # alike, so flagged results take a 0.3x multiplier on their primary score.
    def _slot_score(r: dict[str, Any]) -> float:
        s = r.get("hybrid_score", r.get("bm25_score", r.get("relevance_score", 0)))
        try:
            s = float(s)
        except (TypeError, ValueError):
            s = 0.0
        if r.get("stats_methods_noise") or r.get("low_quality_source"):
            s *= DEFAULT_NOISE_PENALTY
        return s

    ranked = sorted(
        results,
        key=lambda r: (
            -_slot_score(r),
            r.get("tier", 3),
        ),
    )
    # F5 (#5): per-tier extract budget. Forces source-type diversity when
    # user passes --tier-budget T0:3,T1:5,T3:8. Default (None) = no caps,
    # pick strictly by relevance-first.
    if tier_budget:
        from collections import defaultdict

        by_tier: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for r in ranked:
            by_tier[r.get("tier", 3)].append(r)
        capped: list[dict[str, Any]] = []
        for tier, cap in sorted(tier_budget.items()):
            capped.extend(by_tier.get(tier, [])[:cap])
        # If capped < top_n, backfill from remaining ranked results
        if len(capped) < top_n:
            seen = {id(r) for r in capped}
            for r in ranked:
                if id(r) not in seen:
                    capped.append(r)
                    if len(capped) >= top_n:
                        break
        ranked = capped[:top_n]
    else:
        ranked = ranked[:top_n]
    urls = [
        r.get("canonical_url") or r.get("href")
        for r in ranked[:top_n]
        if r.get("canonical_url") or r.get("href")
    ]
    tier_by_url = {
        (r.get("canonical_url") or r.get("href")): r.get("tier", 3) for r in results
    }

    def _one(url: str) -> dict[str, Any]:
        # Rate limiter is thread-safe — concurrent calls block as needed.
        limiter.acquire(timeout=10.0)
        metrics.incr("requests")
        metrics.incr("extracts")
        t0 = time.monotonic()
        try:
            content = _extract_with_fallback(ddgs, url, proxy_for_log="<none>")
            if not _content_floor_ok({"content": content}):
                # F-48: boilerplate-only extraction (nav footers, cookie
                # walls) is junk, not content — report it as such instead of
                # serving 146-char footers as "articles".
                log.warning(
                    "extract_below_quality_floor",
                    url=url[:60],
                    chars=len(content),
                )
                return {
                    "url": url,
                    "content": None,
                    "error": "below_content_floor",
                    "chars": len(content),
                    "hint": f"page extracted under {DEFAULT_MIN_CONTENT_CHARS} chars of real content",
                    "tier": tier_by_url.get(url, 3),
                }
            kp = find_key_passage(content, query_terms)
            return {
                "url": url,
                "content": content,
                "chars": len(content),
                "key_passage": kp,
                "tier": tier_by_url.get(url, 3),
                "latency_s": round(time.monotonic() - t0, 3),
            }
        except BinaryDocument as ex:
            # Non-HTML (PDF/DOCX) result from a search hit — record as binary
            # so agent routes to pdf-ocr skill instead of synthesizing emptiness.
            log.info("extract_binary_hit", url=url, content_type=ex.content_type)
            return {
                "url": url,
                "content": None,
                "binary": True,
                "content_type": ex.content_type,
                "error": "binary_content",
                "hint": "download + invoke pdf-ocr skill",
                "tier": tier_by_url.get(url, 3),
            }
        except SearchError as ex:
            log.warning("extract_failed", url=url, error=str(ex))
            return {"url": url, "content": None, "error": str(ex)}

    # Parallel extract — IO-bound (HTTP + Playwright), ThreadPoolExecutor gives
    # 2-3x speedup on top-N URLs. Order preserved via .map().
    workers = max(1, min(len(urls), DEFAULT_CONCURRENCY))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        out = list(pool.map(_one, urls))
    return out


# --------------------------------------------------------------------------- #
# Deep crawl: BFS extract of a site's internal pages
# --------------------------------------------------------------------------- #

# Markdown link patterns — match RELATIVE + absolute URLs (ddgs returns both)
_RE_REF_DEF = re.compile(r"^\[(\d+)\]:\s*(\S+)", re.MULTILINE)
_RE_INLINE_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
_RE_BARE_URL = re.compile(r"(?<![=[(])(https?://[^\s\]\])>\[<>\"'`]+)")
_RE_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# Static-asset extensions — crawl budget should go to article pages, not
# CSS/JS/font/image files. Checked via URL path suffix.
_CRAWL_SKIP_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".css",
        ".js",
        ".mjs",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".webp",
        ".avif",
        ".bmp",
        ".tiff",
        ".mp4",
        ".webm",
        ".mp3",
        ".ogg",
        ".wav",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".rar",
        ".xml",
        ".txt",
    }
)

# CMS / non-content path patterns — platform-AGNOSTIC. Catches admin/api/
# checkout/feed endpoints across WordPress, Joomla, Drupal, Shopify,
# Squarespace, Wix, Ghost, Next.js, and generic frameworks. Does NOT skip
# content-bearing paths like /tag/, /category/, /blog/, /article/ — those
# lead to real article lists the crawl should follow.
_CRAWL_SKIP_PATHS: tuple[str, ...] = (
    # Admin / auth / dashboard (no public content anywhere)
    "/wp-admin/",
    "/wp-login",
    "/administrator/",
    "/admin/",
    "/admin.php",
    "/user/login",
    "/account/login",
    "/account/register",
    "/dashboard",
    "/manage/",
    "/console",
    "/ghost/",
    "/ghost/api",
    # API / JSON endpoints (not HTML article content)
    "/wp-json/",
    "/wp-content/",
    "/wp-includes/",
    "/api/",
    "/_api/",
    "/graphql",
    "/.well-known/",
    # CMS framework internals (Joomla/Drupal/module dirs)
    "/components/com_",
    "/modules/mod_",
    "/plugins/",
    "/templates/",
    "/media/system/",
    "/sites/default/files/",
    "/sites/all/",
    "/ngimages/",
    "/config/",
    "/xmlrpc.php",
    # E-commerce checkout (not article content)
    "/cart",
    "/checkout",
    "/products.json",
    "/collections.json",
    "/order",
    "/payment",
    # Feeds / machine endpoints
    "/feed",
    "/comments/feed",
    "/trackback",
    "/rss",
    "/atom.xml",
    "/robots.txt",
    "/sitemap",
    "/webmanifest",
    "/manifest.json",
    "/sw.js",
    "/service-worker",
    "/.htaccess",
    # CMS query-string page IDs
    "/?p=",
    "/?page_id=",
    "/?attachment_id=",
    "/?cat=",
    "/?author=",
    # CDN / edge
    "/cdn-cgi/",
    "/_next/",
    "/__nextjs",
)


def _is_crawl_skip(url: str) -> bool:
    """Return True if URL is a static asset or CMS-internal page that should
    NOT consume crawl budget. Filters CSS/JS/fonts/images + WordPress/Joomla/
    Drupal admin/feed/API internals + query-string-based internal IDs."""
    path = urlparse(url).path.lower()
    # Also check the query string — WP uses ?p=ID, ?page_id=ID, ?attachment_id=ID
    query = urlparse(url).query.lower()
    full = path + ("?" + query if query else "")
    if not path:
        return False
    # Extension check (fast path)
    if "." in path.rsplit("/", 1)[-1]:
        ext = "." + path.rsplit(".", 1)[-1]
        if ext in _CRAWL_SKIP_EXTENSIONS:
            return True
    # CMS-internal pattern check (path + query)
    for skip in _CRAWL_SKIP_PATHS:
        if skip in full:
            return True
    return False


def _score_link_by_query(url: str, query_terms: set[str]) -> int:
    """Score a URL by query-keyword overlap in its slug/path. Higher = more
    relevant. Uses STEM-aware matching: "strategy" matches "strategies",
    "trade" matches "trading", "reversion" matches "reversal". Minimum 4-char
    prefix to avoid false positives.

    Example: query="mean reversion RSI strategy backtest"
      /mean-reversion-rsi-strategy → score 4 (all terms stem-match)
      /trading-strategies           → score 1 (strategy→strategies)
      /testimonials                 → score 0
    """
    if not query_terms:
        return 0
    path = urlparse(url).path.lower()
    raw_tokens = re.split(r"[-/_\s.]+", path)
    url_tokens = {t for t in raw_tokens if len(t) > 2}
    score = 0
    for qt in query_terms:
        # Stem: use first 4+ chars as prefix for fuzzy matching
        stem = qt[: max(4, len(qt) - 2)] if len(qt) > 4 else qt
        for ut in url_tokens:
            # Bidirectional stem match: query term in URL token OR vice versa
            if stem in ut or ut[: max(4, len(ut) - 2)] in qt:
                score += 1
                break
    return score


def _score_anchor_text(anchor_text: str, query_terms: set[str]) -> int:
    """F3: score link ANCHOR TEXT by query-keyword overlap.

    Same stem-aware matching as `_score_link_by_query` but over the visible
    link label — "complete guide to RSI mean reversion" scores for an RSI
    query even when the URL slug is "/blog/2023/p1". Anchor text is often
    the page's own description of itself; ignoring it left the frontier
    blind to /p1-style CMS paths.
    """
    if not query_terms or not anchor_text:
        return 0
    raw_tokens = re.split(r"\s+", anchor_text.strip().strip("*_`#'\""))
    tokens = {t.lower() for t in raw_tokens if len(t) > 2}
    score = 0
    for qt in query_terms:
        stem = qt[: max(4, len(qt) - 2)] if len(qt) > 4 else qt
        for tok in tokens:
            if stem in tok or tok[: max(4, len(tok) - 2)] in qt:
                score += 1
                break
    return score


def _extract_links_from_markdown(
    content: str, base_host: str, base_url: str
) -> list[tuple[str, str]]:
    """Parse ALL internal links from markdown. Resolves relative URLs.

    Handles ddgs extract format: reference [text][1] + [1]: relative-or-abs-url,
    inline [text](url), bare URLs. Returns unique INTERNAL links (same domain)
    as (canonical_url, anchor_text) tuples — anchor text feeds F3 frontier
    scoring, empty for reference/bare links where no label exists.
    """
    if not content or not base_host:
        return []

    links: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _try_add(raw_url: str, text: str = "") -> None:
        # Strip trailing punctuation + quotes (markdown link extraction can
        # capture trailing ", ', ), ] that aren't part of the URL).
        raw_url = raw_url.strip().rstrip(".,;)}]\"'")
        if not raw_url or raw_url.startswith(("#", "mailto:", "javascript:")):
            return
        # Skip markdown reference artifacts like "[2]:" being treated as URLs
        if raw_url.startswith("[") or "]" in raw_url:
            return
        # Resolve relative URL against the page URL
        full_url = urljoin(base_url, raw_url)
        if not full_url.startswith("http"):
            return
        # Skip static assets + CMS internals — they waste crawl budget and
        # never contain article content. Filtered at extraction time so they
        # never enter the frontier.
        if _is_crawl_skip(full_url):
            return
        host = _host_of(full_url)
        if not host or "[" in full_url or "]" in full_url:
            return
        # Internal = same domain or subdomain
        if host == base_host or host.endswith("." + base_host):
            canon = canonicalize_url(full_url)
            if canon and canon not in seen:
                seen.add(canon)
                links.append((canon, text.strip()[:200]))

    # Reference definitions: [1]: url-or-relative-path
    ref_urls: dict[str, str] = {}
    for m in _RE_REF_DEF.finditer(content):
        ref_id = m.group(1)
        ref_urls[ref_id] = m.group(2)
        _try_add(m.group(2))

    # Inline links: [text](url) — group(1)=text, group(2)=url (F3)
    for m in _RE_INLINE_LINK.finditer(content):
        _try_add(m.group(2), m.group(1))

    # Bare URLs
    for m in _RE_BARE_URL.finditer(content):
        _try_add(m.group(0))

    return links


def _extract_page_title(content: str) -> str:
    """Extract page title from markdown (first H1 or first line)."""
    if not content:
        return ""
    m = _RE_H1.search(content)
    if m:
        return m.group(1).strip()[:200]
    # Fallback: first non-empty line
    for line in content.split("\n"):
        line = line.strip().strip("#*[]()")
        if len(line) > 5:
            return line[:200]
    return ""


# --------------------------------------------------------------------------- #
# F1/F2: crawl-start intelligence — robots.txt politeness + sitemap seeding
# --------------------------------------------------------------------------- #

_ROBOTS_MAX_BYTES = 262_144  # robots.txt read cap (256 KB)
_SITEMAP_MAX_BYTES = 2_097_152  # per-sitemap decompressed read cap (2 MB)
_SITEMAP_FETCH_CAP = 4  # max sitemap documents fetched per crawl


def _fetch_text_bounded(url: str, max_bytes: int) -> str | None:
    """GET url via stdlib urllib, bounded to max_bytes (compressed AND
    decompressed), transparent gzip (magic-byte sniff). Returns None on any
    failure or over-cap — robots/sitemap are accelerants, never worth a crash.
    """
    try:
        req = urllib_request.Request(url, headers={"User-Agent": _LLM_UA})
        with urllib_request.urlopen(req, timeout=5) as resp:
            raw = resp.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return None
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
                if len(raw) > max_bytes:
                    return None
            return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


def _fetch_robots_txt(origin: str) -> RobotFileParser | None:
    """F2: fetch + parse {origin}/robots.txt. None = unavailable/disabled.

    WEB_SEARCH_ROBOTS=0 disables (kill switch). Any failure degrades to None
    silently-but-logged — robots is politeness metadata, not a hard dependency.
    The START URL itself is never robots-gated (it was explicitly directed by
    the user/agent); only frontier-discovered links are.
    """
    if os.environ.get("WEB_SEARCH_ROBOTS", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return None
    text = _fetch_text_bounded(f"{origin}/robots.txt", _ROBOTS_MAX_BYTES)
    if not text:
        log.debug("robots_unavailable", origin=origin[:60])
        return None
    rp = RobotFileParser()
    try:
        rp.parse(text.splitlines())
        return rp
    except Exception:
        log.debug("robots_parse_failed", origin=origin[:60])
        return None


def _parse_locs(xml_text: str) -> tuple[list[str], list[str]]:
    """Parse a sitemap document -> (page_locs, child_sitemap_locs).

    Namespace-agnostic via local-name matching (tag.split('}')[-1]) —
    sitemaps in the wild mix plain <urlset> and namespace-prefixed variants.
    'sitemapindex' roots yield child sitemap URLs; 'urlset' roots yield <loc>.
    Text-format sitemaps (sitemap.txt: one URL per line, no XML — e.g.
    doc.rust-lang.org declares one) yield their lines as page locs.
    """
    stripped = xml_text.lstrip()
    if not stripped.startswith("<"):
        urls = [
            ln.strip()
            for ln in xml_text.splitlines()
            if ln.strip().startswith(("http://", "https://"))
        ]
        return urls, []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], []
    root_tag = root.tag.split("}")[-1]
    urls: list[str] = []
    children: list[str] = []
    if root_tag == "sitemapindex":
        for el in root.iter():
            if el.tag.split("}")[-1] == "sitemap":
                for loc in el:
                    if loc.tag.split("}")[-1] == "loc" and (loc.text or "").strip():
                        children.append(loc.text.strip())
    elif root_tag == "urlset":
        for el in root.iter():
            if el.tag.split("}")[-1] == "url":
                for loc in el:
                    if loc.tag.split("}")[-1] == "loc" and (loc.text or "").strip():
                        urls.append(loc.text.strip())
    return urls, children


def _fetch_sitemap_locs(
    sitemap_urls: list[str],
    base_host: str,
    resolver: Any,
    deadline: float,
) -> list[str]:
    """F1: collect internal page URLs from robots-declared + conventional
    sitemaps. Every fetched sitemap URL is SSRF-validated FIRST (R3: a
    robots.txt can declare sitemaps pointing anywhere, incl. cloud-metadata
    IPs — same guard as every other fetch in this skill).

    Bounded: max _SITEMAP_FETCH_CAP documents (child sitemapindex recursion
    capped at 3 per level, ONE level deep), monotonic deadline, max
    WEB_SEARCH_SITEMAP_MAX (default 500) locs, internal-host filter applied.
    WEB_SEARCH_SITEMAP=0 disables.
    """
    if os.environ.get("WEB_SEARCH_SITEMAP", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return []
    try:
        max_locs = max(
            1, min(5000, int(os.environ.get("WEB_SEARCH_SITEMAP_MAX", "500")))
        )
    except ValueError:
        max_locs = 500

    queue: list[str] = []
    for u in sitemap_urls:
        if u not in queue:
            queue.append(u)
    for conventional in (
        f"https://{base_host}/sitemap.xml",
        f"http://{base_host}/sitemap.xml",
    ):
        if conventional not in queue:
            queue.append(conventional)

    locs: list[str] = []
    seen: set[str] = set()
    fetches = 0
    index_children: list[str] = []

    def _absorb(urls: list[str]) -> None:
        """Filter sitemap <loc> entries down to internal, non-asset, canonical
        URLs and append (bounded by max_locs). Shared by root + child passes."""
        for u in urls:
            if len(locs) >= max_locs:
                break
            if not u.startswith(("http://", "https://")):
                continue
            if _is_crawl_skip(u):
                continue
            host = _host_of(u)
            if not host or not (host == base_host or host.endswith("." + base_host)):
                continue
            canon = canonicalize_url(u)
            if canon and canon not in seen:
                seen.add(canon)
                locs.append(canon)

    def _fetch_sm(sm_url: str) -> tuple[list[str], list[str]] | None:
        """SSRF-validate + fetch + parse one sitemap document. None =
        skipped or failed. R3: robots-declared sitemap URLs can point
        anywhere (incl. metadata IPs) — validated BEFORE any fetch."""
        if not sm_url.startswith(("http://", "https://")):
            return None
        try:
            resolver.resolve_and_validate(sm_url)
        except Exception:
            return None
        xml_text = _fetch_text_bounded(sm_url, _SITEMAP_MAX_BYTES)
        if not xml_text:
            return None
        return _parse_locs(xml_text)

    while queue and fetches < _SITEMAP_FETCH_CAP and len(locs) < max_locs:
        if time.monotonic() > deadline:
            break
        parsed = _fetch_sm(queue.pop(0))
        fetches += 1
        if parsed is None:
            continue
        urls, children = parsed
        for child in children[:3]:
            if child not in index_children and len(index_children) < 3:
                index_children.append(child)
        _absorb(urls)
        if locs:
            # First productive sitemap wins — the conventional /sitemap.xml
            # fallbacks exist for when declared sources yield nothing, not
            # to burn fetch budget re-reading the same pages over http.
            break
    # Child sitemapindex recursion — ONE level, same fetch/deadline budget
    for child in index_children:
        if fetches >= _SITEMAP_FETCH_CAP or len(locs) >= max_locs:
            break
        if time.monotonic() > deadline:
            break
        parsed = _fetch_sm(child)
        fetches += 1
        if parsed is None:
            continue
        _absorb(parsed[0])
    if locs:
        log.info("sitemap_seeded", urls=len(locs), fetches=fetches)
    return locs


def _crawl_site(
    ddgs,
    start_url: str,
    *,
    max_pages: int,
    max_chars: int,
    call_deadline: float,
    proxy_for_log: str,
    max_depth: int = 0,
    query: str | None = None,
) -> dict[str, Any]:
    """Priority-queue crawl: extract start page → find internal links → score
    by query relevance → extract highest-scoring sub-pages first.

    When `query` is provided, links are scored by keyword overlap in their URL
    slug — strategy articles ("/rsi-mean-reversion-strategy") are visited
    BEFORE low-value pages ("/testimonials", "/cart", "/blog"). When `query`
    is None, falls back to BFS (depth-order).

    Bounded by max_pages, max_depth, call_deadline, and DEFAULT_MAX_CRAWL_BYTES.
    Rate-limited via token bucket. SSRF-validated at start + per-link enqueue.
    Non-HTML sub-pages (PDF/DOCX) are recorded as binary_url entries.
    """
    base_host = _host_of(start_url)
    if not base_host:
        return {"error": f"Invalid start URL: {start_url}"}

    # SSRF guard — validate the start URL's resolved IP before any crawl fetch.
    resolver = get_safe_url_resolver()
    try:
        resolver.resolve_and_validate(start_url)
    except (InvalidURL, SSRFBlocked) as ex:
        return {"error": f"Start URL failed SSRF policy: {ex}"}

    # Query terms for link scoring (enables priority frontier).
    query_terms: set[str] = set()
    if query:
        query_terms = {t.lower() for t in re.split(r"\s+", query) if len(t) > 2}

    # F2: robots.txt intelligence — politeness (disallow gate on frontier
    # links, Crawl-Delay top-up) + declared sitemaps for F1 seeding. The
    # start URL itself is never robots-gated (explicitly directed fetch).
    _origin = f"{urlparse(start_url).scheme or 'https'}://{base_host}"
    robots = _fetch_robots_txt(_origin)
    crawl_delay = robots.crawl_delay("*") if robots is not None else None
    sitemap_seeded = 0

    limiter = _get_rate_limiter()
    metrics = _get_metrics()
    visited: set[str] = set()
    pages: list[dict[str, Any]] = []
    binary_urls: list[dict[str, str]] = []
    all_links: set[str] = set()
    crawl_bytes = 0
    # F-27: only pages that yielded CONTENT consume the max_pages budget.
    # Binary hits and extraction errors are recorded but don't shrink the
    # crawl — a site with many PDFs previously starved real article pages.
    n_content_pages = 0
    start_canon = canonicalize_url(start_url)

    # Priority frontier: heap of (-score, depth, insertion_order, url).
    # Negative score because heapq is a min-heap → highest score pops first.
    # insertion_order is a tiebreaker for stable ordering.
    start_score = _score_link_by_query(start_canon, query_terms)
    frontier: list[tuple[int, int, int, str]] = [(-start_score, 0, 0, start_canon)]
    heapq.heapify(frontier)
    _insert_counter = 1

    # F1: sitemap pre-seed — orphan pages (nothing links to them) become
    # reachable, and query-scored locs surface deep content on iteration 1.
    # Gated on remaining budget; strict 15s sub-deadline so seeding never
    # eats the crawl itself.
    if call_deadline - time.monotonic() > 5:
        sm_locs = _fetch_sitemap_locs(
            robots.site_maps() if robots is not None else [],
            base_host,
            resolver,
            deadline=time.monotonic() + 15.0,
        )
        for loc in sm_locs:
            if len(frontier) >= DEFAULT_MAX_TOTAL_LINKS:
                break
            if len(pages) + len(frontier) >= max_pages:
                break
            if loc in visited or loc == start_canon:
                continue
            try:
                resolver.resolve_and_validate(loc)
            except (InvalidURL, SSRFBlocked, SearchError):
                continue
            loc_score = _score_link_by_query(loc, query_terms)
            heapq.heappush(frontier, (-loc_score, 1, _insert_counter, loc))
            _insert_counter += 1
            sitemap_seeded += 1

    while frontier and n_content_pages < max_pages:
        if time.monotonic() > call_deadline:
            log.warning("crawl_budget_exhausted", pages_extracted=len(pages))
            break
        if crawl_bytes >= DEFAULT_MAX_CRAWL_BYTES:
            log.warning(
                "crawl_byte_budget_exhausted", bytes=crawl_bytes, pages=len(pages)
            )
            break

        neg_score, depth, _, url = heapq.heappop(frontier)
        link_score = -neg_score
        if url in visited:
            continue
        visited.add(url)

        # Rate-limited fetch
        acquired = limiter.acquire(timeout=min(call_deadline - time.monotonic(), 10.0))
        if not acquired:
            break
        metrics.incr("requests")
        metrics.incr("extracts")
        t0 = time.monotonic()

        try:
            content = _extract_with_fallback(ddgs, url, proxy_for_log=proxy_for_log)
            elapsed = round(time.monotonic() - t0, 3)
        except BinaryDocument as ex:
            log.info("crawl_binary_skipped", url=url, content_type=ex.content_type)
            binary_urls.append(
                {"url": url, "content_type": ex.content_type, "depth": str(depth)}
            )
            pages.append(
                {
                    "url": url,
                    "depth": depth,
                    "binary": True,
                    "content_type": ex.content_type,
                    "error": "binary_content",
                    "content": None,
                }
            )
            continue
        except SearchError as ex:
            log.warning("crawl_extract_failed", url=url, error=str(ex))
            pages.append(
                {
                    "url": url,
                    "depth": depth,
                    "error": str(ex),
                    "content": None,
                }
            )
            continue

        title = _extract_page_title(content)
        # Crawl byte-budget accounting (guards against pathological multi-GB crawls)
        crawl_bytes += len(content.encode("utf-8", errors="ignore"))
        if crawl_bytes > DEFAULT_MAX_CRAWL_BYTES:
            log.warning(
                "crawl_byte_budget_hit_mid",
                bytes=crawl_bytes,
                limit=DEFAULT_MAX_CRAWL_BYTES,
                pages=len(pages),
            )
            pages.append(
                {
                    "url": url,
                    "title": title,
                    "content": content,
                    "chars": len(content),
                    "depth": depth,
                    "n_links_found": 0,
                    "latency_s": elapsed,
                    "truncated": "crawl_byte_budget",
                }
            )
            break
        # Parse links from FULL content (link defs at bottom, no truncation).
        # Cap per-page links to bound frontier growth on link-farm pages.
        # F3: links now carry (url, anchor_text) tuples — anchor text feeds
        # frontier scoring; R1: unpack tuples for all_links (plain URL set).
        found_links = _extract_links_from_markdown(content, base_host, url)[
            :DEFAULT_MAX_LINKS_PER_PAGE
        ]
        all_links.update(u for u, _ in found_links)

        # NO truncation — full page content stored as-is
        pages.append(
            {
                "url": url,
                "title": title,
                "content": content,
                "chars": len(content),
                "depth": depth,
                "link_score": link_score,
                "n_links_found": len(found_links),
                "latency_s": elapsed,
            }
        )
        n_content_pages += 1
        log.info(
            "crawl_page_ok",
            url=url,
            depth=depth,
            link_score=link_score,
            chars=len(content),
            links_found=len(found_links),
            latency_s=elapsed,
        )

        # Enqueue unvisited internal links into the priority frontier.
        # Each link is scored by query-keyword overlap in its URL slug PLUS
        # its anchor text (F3) so strategy articles are visited before
        # testimonials/blog/cart. max_depth == 0 means unlimited. SSRF
        # per-link validation at enqueue. F2: robots-disallowed links are
        # dropped here (politeness = fewer blocks).
        if max_depth == 0 or depth < max_depth:
            for link, anchor_text in found_links:
                if len(pages) + len(frontier) >= max_pages:
                    break
                if len(frontier) >= DEFAULT_MAX_TOTAL_LINKS:
                    break
                if link in visited:
                    continue
                if robots is not None and not robots.can_fetch("*", link):
                    log.debug("crawl_robots_disallow", url=link[:60])
                    continue
                try:
                    resolver.resolve_and_validate(link)
                except (InvalidURL, SSRFBlocked) as ex:
                    log.debug(
                        "crawl_link_ssrf_skip", url=link[:60], reason=str(ex)[:60]
                    )
                    continue
                except SearchError:
                    continue
                score = _score_link_by_query(link, query_terms) + min(
                    6, 2 * _score_anchor_text(anchor_text, query_terms)
                )
                heapq.heappush(frontier, (-score, depth + 1, _insert_counter, link))
                _insert_counter += 1

        # Human-paced delay between page fetches (anti behavioral analysis).
        # F2: robots Crawl-Delay top-up — _human_delay() sleeps 1-3.5s; if
        # the site declares a longer minimum, sleep the remainder (never
        # negative, never re-sleeping what already elapsed).
        if frontier and n_content_pages < max_pages:
            _human_delay()
            if crawl_delay:
                time.sleep(max(0.0, crawl_delay - 3.5))

    followed = {p["url"] for p in pages if "content" in p and p["content"]}
    not_followed = sorted(all_links - followed)

    return {
        "schema": OUTPUT_SCHEMA_VERSION,
        "start_url": start_canon,
        "host": base_host,
        "tier": domain_tier(base_host),
        "robots": {
            "fetched": robots is not None,
            "crawl_delay": crawl_delay,
            "sitemap_seeded": sitemap_seeded,
        },
        "max_depth": max_depth,
        "n_pages_extracted": sum(1 for p in pages if p.get("content")),
        "n_errors": sum(1 for p in pages if p.get("error")),
        "n_binary_skipped": len(binary_urls),
        "binary_urls": binary_urls,
        "n_total_links_found": len(all_links),
        "n_links_not_followed": len(not_followed),
        "total_content_chars": sum(p.get("chars", 0) for p in pages),
        "pages": pages,
        "all_links_found": sorted(all_links)[:100],
        "links_not_followed": not_followed[:50],
        "metrics": metrics.snapshot(),
    }


# --------------------------------------------------------------------------- #
# Adaptive crawl with KB cache — Crawl4AI's export/import_knowledge_base
# persists the semantic index per (start_url, query). Repeat queries = instant.
# --------------------------------------------------------------------------- #


def _adaptive_cache_dir() -> Path:
    """Per-skill cache dir for adaptive KBs (from Settings — no env bypass)."""
    p = get_settings().cache.dir / "adaptive_kb"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _adaptive_cache_paths(start_url: str, query: str) -> tuple[Path, Path]:
    """Return (kb_path, meta_path) for a (start_url, query) pair."""
    key = hashlib.sha256(f"{start_url}|{query}".encode()).hexdigest()[:16]
    base = _adaptive_cache_dir() / key
    return base.with_suffix(".jsonl"), base.with_suffix(".meta.json")


def _atomic_write_text(path: Path, text: str) -> None:
    """Torn-write-safe file replace: write tmp + os.replace (same guarantees
    as the search cache writer — a crash mid-write never corrupts the meta)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as ex:
        log.debug("atomic_write_failed", path=str(path), error=str(ex)[:80])
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _crawl_adaptive_cached(
    start_url: str,
    query: str,
    *,
    max_pages: int,
    call_deadline: float,
    no_cache: bool = False,
) -> dict[str, Any]:
    """Adaptive crawl with KB persistence. Returns cached result if fresh,
    else runs live and saves KB. Cache knobs from Settings (single source);
    `no_cache` carries the CLI --no-cache flag through (was silently dropped)."""
    _cfg = get_settings()
    no_cache = no_cache or not _cfg.cache.enabled
    ttl = _cfg.cache.ttl
    kb_path, meta_path = _adaptive_cache_paths(start_url, query)

    if not no_cache and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            age = time.time() - meta.get("ts", 0)
            if age <= ttl:
                meta["cache_status"] = "fresh"
                meta["served_from"] = "cache"
                return meta
        except Exception as ex:
            log.debug("adaptive_cache_read_failed", error=str(ex)[:80])

    # Cache miss or stale — run live, export KB alongside
    result = _crawl_adaptive(
        start_url,
        query,
        max_pages=max_pages,
        call_deadline=call_deadline,
        kb_export_path=str(kb_path) if not no_cache else None,
    )
    if "error" in result:
        return result

    result["ts"] = time.time()
    result["cache_status"] = "miss"
    result["served_from"] = "live"
    if not no_cache:
        _atomic_write_text(meta_path, json.dumps(result))

    return result


def _crawl_adaptive(
    start_url: str,
    query: str,
    *,
    max_pages: int,
    call_deadline: float,
    kb_export_path: str | None = None,
) -> dict[str, Any]:
    """Crawl4AI AdaptiveCrawler — confidence-scored semantic crawl.

    Digests a site guided by a query: scrapes pages, ranks by semantic
    relevance, stops when confidence threshold reached. Returns same schema
    as _crawl_site plus `confidence`, `query`, `crawler` fields.
    Optionally exports KB to `kb_export_path` for cache reuse.

    F-04: the start URL is SSRF-validated BEFORE the browser is launched —
    previously this path (crawl --query / agentic --adaptive) bypassed the
    egress policy entirely and would happily fetch internal/metadata hosts.
    """
    try:
        get_safe_url_resolver().resolve_and_validate(start_url)
    except (InvalidURL, SSRFBlocked) as ex:
        log.warning("adaptive_ssrf_blocked", url=start_url[:80], reason=str(ex)[:80])
        return {"error": f"Start URL failed SSRF policy: {ex}"}
    except SearchError as ex:
        return {"error": f"Start URL resolution failed: {ex}"}

    import asyncio  # hard dep

    from crawl4ai import AdaptiveCrawler, AsyncLogger, AsyncWebCrawler  # hard dep
    from crawl4ai import BrowserConfig  # hard dep

    async def _run() -> tuple[float, list[dict[str, Any]]]:
        # accept_downloads (2026-09-03): journal PDF-galley URLs trigger
        # Playwright 'Download is starting' -> hard crawl error when
        # downloads are refused. Accept, discard the binaries.
        import tempfile as _tf_dl

        _dl_dir = _tf_dl.mkdtemp(prefix="ws_crawl_dl_")

        _bc = BrowserConfig(
            accept_downloads=True, downloads_path=_dl_dir, headless=True
        )
        async with AsyncWebCrawler(
            config=_bc, logger=AsyncLogger(verbose=False)
        ) as crawler:
            adaptive = AdaptiveCrawler(crawler)
            await adaptive.digest(start_url=start_url, query=query)
            confidence = float(adaptive.confidence)
            relevant = adaptive.get_relevant_content(top_k=max_pages)
            if kb_export_path:
                # Suppress crawl4ai's "Exported N documents..." stdout (would pollute JSON)
                import contextlib
                import io

                with contextlib.redirect_stdout(io.StringIO()):
                    try:
                        adaptive.export_knowledge_base(kb_export_path)
                    except Exception as ex:
                        log.debug("kb_export_failed", error=str(ex)[:80])
            return confidence, list(relevant)

    try:
        confidence, relevant = asyncio.run(_run())
    except Exception as ex:
        return {"error": f"adaptive crawl failed: {ex}"}

    base_host = _host_of(start_url)
    start_canon = canonicalize_url(start_url)
    pages: list[dict[str, Any]] = []
    total_chars = 0
    total_links = 0
    for i, item in enumerate(relevant):
        _u = str(item.get("url", ""))
        if "citationStyleLanguage" in _u:
            # OJS citation-format AJAX snippets are metadata, never content
            continue
        content = str(item.get("content", "") or "")
        chars = len(content)
        total_chars += chars
        n_links = content.count("](")
        total_links += n_links
        pages.append(
            {
                "url": str(item.get("url", "")),
                "title": _extract_page_title(content),
                "content": content,
                "chars": chars,
                "depth": 0,
                "n_links_found": n_links,
                "latency_s": 0.0,
                "score": float(item.get("score", 0) or 0),
                "index": int(item.get("index", i) or i),
            }
        )
    return {
        "schema": OUTPUT_SCHEMA_VERSION,
        "start_url": start_canon,
        "host": base_host,
        "tier": domain_tier(base_host),
        "crawler": "crawl4ai-adaptive",
        "query": query,
        "confidence": round(confidence, 4),
        "n_pages_extracted": len(pages),
        "n_errors": 0,
        "n_total_links_found": total_links,
        "n_links_not_followed": 0,
        "total_content_chars": total_chars,
        "pages": pages,
        "all_links_found": [],
        "links_not_followed": [],
        "metrics": _get_metrics().snapshot(),
    }


# --------------------------------------------------------------------------- #
# Agentic loop (pure infrastructure: per-iter backend rotation + query
# breadth modifiers + page increment + convergence stop). NO LLM cognition —
# the AI agent decomposes complex research questions itself via
# `batch -q ... -q ...`. This loop maximises source diversity within one query.
# --------------------------------------------------------------------------- #


# F4: forced backend rotation per iteration. Each backend has a distinct
# index (Brave = independent, Yandex = RU/Asia-heavy, DDG = Bing proxy, Google,
# Yahoo). Rotating forces genuinely different result corpora vs `--backend auto`
# which picked the single fastest backend every iter. Filtered against
# _get_text_backends() at runtime in case ddgs disables any.
_ROTATION_PREFERENCE = ["brave", "duckduckgo", "yandex", "google", "yahoo"]


def _derive_agentic_rotation() -> list[str]:
    """F-60: derive the agentic rotation pool from the live ddgs engine
    registry (context-driven - new backends join without code edits). The
    curated order survives as fallback when the registry is unavailable and
    as ordering seed; capped so per-iteration rotation stays meaningful."""
    try:
        from ddgs.engines import ENGINES

        available = set(ENGINES.get("text", {}).keys())
    except ImportError:
        available = set()
    if not available:
        return list(_ROTATION_PREFERENCE)
    preferred = [b for b in _ROTATION_PREFERENCE if b in available]
    rest = sorted(available - set(preferred))
    return (preferred + rest)[:6]


_AGENTIC_BACKEND_ROTATION = _derive_agentic_rotation()

# F6: query breadth modifiers (kept — they DO add result diversity in engines
# that respect OR; removed root cause was stop condition + ranking, not these).
# Combined with backend rotation, gives multi-axis diversity: each iter changes
# BOTH the backend AND the query flavour.
# Query-breadth phrasings: English search-engine conventions (domain
# knowledge constant, same boundary rationale as the stats vocabulary).
_AGENTIC_MODIFIERS = [
    "",  # iter 0: original
    "guide OR explained OR overview",  # iter 1: breadth
    "2024 OR 2025 OR latest",  # iter 2: recency
    "analysis OR comparison OR review",  # iter 3: depth
    "example OR case study OR findings",  # iter 4: evidence
]


def _refine_query_llm(
    query: str, seen_titles: list[str], *, model: str | None = None
) -> str:
    """One alternative phrasing of `query` aimed at results the previous
    iterations missed (WS2b). The static _AGENTIC_MODIFIERS are generic
    breadth hints; an LLM rewrite targets the actual gap in the found set.
    Raises OllamaUnavailable — the caller falls back to the static modifier
    and logs it (mid-run resilience, never silent)."""
    seen = "\n".join(f"- {t[:80]}" for t in seen_titles[:8]) or "- (none yet)"
    system = (
        "You rewrite search queries for a research engine. Given the original "
        "query and the titles already found, produce ONE alternative search "
        "query that surfaces COMPLEMENTARY results the existing set misses "
        "(different terminology, angle, or aspect). Keep the query's language. "
        "Return ONLY the query text — no quotes, no explanation."
    )
    prompt = (
        f"Original query: {query}\n\nTitles already found:\n{seen}\n\n"
        "Alternative query:"
    )
    raw = _llm_call(prompt, model=model, system=system, think=False, light=True)
    line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    # LLMs love wrapping the query in quotes — sometimes with trailing
    # commentary after the close quote. Take the quoted span when present.
    quoted = re.match(r'^[\u201c"](.*?)[\u201d"]', line)
    if quoted and quoted.group(1).strip():
        line = quoted.group(1).strip()
    return line[:300] or query


def _rerank_results_llm(
    query: str,
    results: list[dict[str, Any]],
    *,
    model: str | None = None,
    max_results: int = 30,
) -> tuple[list[dict[str, Any]], int]:
    """LLM relevance scoring of the top results in ONE call (WS2b).

    BM25 sees tokens; an LLM sees meaning — a lexically close but
    semantically dead result ("Unity GC tutorial" for a cargo-GC query)
    sorts down here. Scores 0-10 per result, results gain `llm_relevance`,
    stable re-sort (scored rise, unscored keep relative order). Returns
    (results, n_scored). Never raises — rerank is best-effort polish; a
    failure logs and returns the input order unchanged."""
    pool = results[:max_results]
    if not pool:
        return results, 0
    listing = "\n".join(
        f"{i}. {str(r.get('title', ''))[:90]} — {str(r.get('body', ''))[:110]}"
        for i, r in enumerate(pool, 1)
    )
    system = (
        "You are a search relevance judge. Score each result 0-10 for how "
        "well it answers the query (10 = directly answers it, 0 = unrelated). "
        "Judge by meaning, not keyword overlap. Return ONLY lines of the "
        "form 'N SCORE' — no other text."
    )
    prompt = f"Query: {query}\n\nResults:\n{listing}\n\nScores:"
    try:
        raw = _llm_call(prompt, model=model, system=system, think=False, light=True)
    except OllamaUnavailable as ex:
        log.warning("llm_rerank_failed", error=str(ex)[:80])
        return results, 0
    scores: dict[int, int] = {}
    for m in re.finditer(r"^\s*(\d{1,3})\s+(\d{1,2})\s*$", raw, re.MULTILINE):
        idx, score = int(m.group(1)), min(10, max(0, int(m.group(2))))
        if 1 <= idx <= len(pool):
            scores[idx - 1] = score
    if not scores:
        log.warning("llm_rerank_unparsed", raw_head=raw[:80])
        return results, 0
    for i, r in enumerate(results):
        r["llm_relevance"] = scores.get(i, -1)
    order = sorted(
        range(len(results)),
        key=lambda i: (-results[i].get("llm_relevance", -1), i),
    )
    return [results[i] for i in order], len(scores)


def _agentic_loop(
    ddgs,
    initial_query,
    *,
    max_iter,
    max_results_per_iter,
    top_n_extract,
    relevance_domains,
    max_per_domain,
    region,
    safesearch,
    timelimit,
    backend,
    cache_ttl,
    neg_cache_ttl,
    extract_chars,
    call_budget,
    proxy_for_log,
    convergence_new_min=DEFAULT_AGENTIC_CONVERGENCE_NEW_MIN,
    convergence_window=DEFAULT_AGENTIC_CONVERGENCE_WINDOW,
    exclude_urls: list[str] | None = None,
    tier_budget: dict[int, int] | None = None,
    llm_mode: str = "auto",
    llm_model: str | None = None,
    summarize: bool = False,
) -> dict[str, Any]:
    """Iterative search with backend rotation + convergence stop.

    Source diversity from (a) per-iter backend rotation across 5 distinct
    corpora (brave→ddg→yandex→google→yahoo→wrap+page-increment) and (b) page
    increment when rotation wraps. Stop = convergence (N consecutive
    low-yield iters) or max_iter or call-budget exhaustion.

    WS2b LLM assist (tri-state llm_mode, default auto): when a local runtime
    answers, iterations ≥1 get an LLM query rewrite targeting the actual gap
    in the found set (static breadth modifier as logged fallback), and the
    final corpus gets a one-call LLM relevance rerank. Payload marks
    llm_assisted + llm_provider + llm_reranked_n — never silent.

    WS7 --summarize: after the extract phase, the corpus (top-20 sources ×
    matched extracted content) goes through _synthesize_llm — the same
    cited, quote-grounded synthesis `pro` uses. think=False (44x faster);
    the loop already spent the research budget, synthesis runs even at the
    call deadline because the answer IS the point of the flag. Fails fast
    when no runtime answers or when combined with --no-llm.
    """
    call_deadline = time.monotonic() + call_budget
    # WS7: --summarize needs the LLM by definition — conflict with --no-llm
    # and missing runtime both fail fast BEFORE any search budget is spent.
    if summarize and llm_mode == "off":
        raise ValueError(
            "--summarize requires LLM synthesis but --no-llm disables it. "
            "Drop one of the two flags."
        )
    llm_provider = _llm_probe() if llm_mode != "off" else None
    if llm_mode == "on" and llm_provider is None:
        raise OllamaUnavailable(
            "--use-llm set but no local LLM runtime answers (probed "
            "/api/tags and /v1/models). Start one or drop --use-llm."
        )
    if summarize and llm_provider is None:
        raise OllamaUnavailable(
            "--summarize set but no local LLM runtime answers (probed "
            "/api/tags and /v1/models). Start one or drop --summarize."
        )
    llm_active = llm_provider is not None and llm_mode != "off"
    query_terms = {t for t in re.split(r"\s+", initial_query) if len(t) > 2}
    # F3: query-language-aware diagnostic filter. The relevance-domain count
    # in iteration metadata now excludes results whose detected language
    # differs from the query's language — kills Yandex i18n spam poisoning.
    query_lang = _detect_language(initial_query) or "en"
    all_results, seen_canon = [], set()
    # F7 (feedback loop): pre-populate seen_canon with agent-flagged useless
    # URLs. Lets agent refine: "extract #3 was off-topic, exclude URL X from
    # next agentic call" → fresh URLs surface in subsequent iters. Pure
    # dedup-based feedback — skill doesn't judge usefulness, agent does.
    if exclude_urls:
        for u in exclude_urls:
            seen_canon.add(canonicalize_url(u))
        log.info("feedback_exclude", n_excluded=len(exclude_urls))
    iterations, extracted = [], []
    status = "ok"
    terminal_error = None
    cur_query = initial_query
    consecutive_low_yield = 0  # F1: convergence counter

    # F4: only rotate when user passed `auto`. Explicit --backend is honored
    # verbatim (user knows they want one engine). Filter rotation against
    # actually-available text backends so a disabled engine doesn't waste iters.
    available = set(_get_text_backends())
    rotate_backends = (backend or "auto").lower() in ("", "auto")
    if rotate_backends:
        rotation = [b for b in _AGENTIC_BACKEND_ROTATION if b in available]
        if not rotation:  # extremely unlikely (ddgs broken); fall back to auto
            rotation = list(available or ["duckduckgo"])
    else:
        rotation = [backend]
    n_rotation = len(rotation)

    for i in range(max_iter):
        if time.monotonic() > call_deadline:
            status = "budget_exhausted"
            break

        # F4 + F6: combined backend rotation + query breadth modifier + page
        # increment. Pass rotation as reordered comma-delimited list so
        # _pick_backend prefers the iter's "first" backend but falls back to
        # others on no-results/errors (single-backend forcing defeated retries).
        iter_preferred_backend = rotation[i % n_rotation]
        rotation_for_iter = rotation[i % n_rotation :] + rotation[: i % n_rotation]
        backend_for_iter = ",".join(rotation_for_iter)
        page_for_iter = (i // n_rotation) + 1
        # Query breadth modifier (kept from old code — adds diversity on engines
        # that respect OR; harmless on those that don't).
        mod = _AGENTIC_MODIFIERS[i] if i < len(_AGENTIC_MODIFIERS) else ""
        # WS2b: LLM rewrite targets the actual gap in the found set; the
        # static modifier stays as a logged fallback so a mid-run LLM hiccup
        # can never degrade below the pre-LLM behavior.
        query_source = "original" if i == 0 else "static"
        cur_query = initial_query
        if i > 0 and llm_active and time.monotonic() < call_deadline - 30:
            try:
                seen_titles = [str(r.get("title", "")) for r in all_results[:8]]
                refined = _refine_query_llm(initial_query, seen_titles, model=llm_model)
                if refined and refined.lower() != initial_query.lower():
                    cur_query = refined
                    query_source = "llm"
            except OllamaUnavailable as ex:
                log.warning("llm_refine_fallback", iteration=i, error=str(ex)[:80])
        if query_source != "llm" and mod:
            cur_query = f"{initial_query} {mod}".strip()

        try:
            payload = _search_with_defense(
                ddgs,
                "text",
                cur_query,
                region=region,
                safesearch=safesearch,
                timelimit=timelimit,
                max_results=max_results_per_iter,
                page=page_for_iter,
                backend=backend_for_iter,
                cache_ttl=cache_ttl,
                neg_cache_ttl=neg_cache_ttl,
                call_deadline=call_deadline,
                proxy_for_log=proxy_for_log,
            )
        except (RateLimited, FetchTimeout) as ex:
            status, terminal_error = "rate_limited_partial", str(ex)
            break
        except BackendUnavailable as ex:
            status, terminal_error = "ddgs_error_partial", str(ex)
            break

        new_count = 0
        for r in payload.get("results", []):
            canon = r.get("canonical_url") or r.get("href") or ""
            if canon and canon in seen_canon:
                continue
            if canon:
                seen_canon.add(canon)
            r2 = dict(r)
            r2["iteration"] = i
            all_results.append(r2)
            new_count += 1

        # F3: query-language-aware relevance count (diagnostic; stop logic below)
        all_rel = {
            r["host"]
            for r in all_results
            if r.get("relevance_score", 0) > 0
            and r.get("tier", 3) <= 2
            and (r.get("language") or "en") == query_lang
        }
        iterations.append(
            {
                "index": i,
                "query": cur_query,
                "query_source": query_source,
                "backend": iter_preferred_backend,
                "backends_pool": backend_for_iter,
                "page": page_for_iter,
                "n_results": len(payload.get("results", [])),
                "n_new": new_count,
                "relevant_domains_total": len(all_rel),
                "consecutive_low_yield": consecutive_low_yield,
                "cache_status": payload["meta"]["cache_status"],
            }
        )

        # F1: convergence-based stop. Old T0-host-count stop was gameable by
        # Yandex i18n spam. Convergence = N consecutive iters with n_new
        # below threshold = corpus exhaustion signal (we've seen everything
        # this query + backend set has to offer).
        if new_count < convergence_new_min:
            consecutive_low_yield += 1
            if consecutive_low_yield >= convergence_window:
                status = "converged"
                break
        else:
            consecutive_low_yield = 0

    # F1+BM25+SimHash post-iter pipeline. Order matters:
    #   1. BM25 rerank — adds bm25_score field per result
    #   2. SimHash content-dedup — drops syndicated near-dupes (AP wire × N sites)
    #   3. Tier-weighted sort — display order
    #   4. Per-domain diversify
    #   5. Tier-budget aware extract selection (in _extract_top_urls)
    n_dropped_dupes = 0
    if all_results:
        all_results = _bm25_rerank(all_results, initial_query)
        all_results, dropped_dupes = _dedup_by_content(all_results, threshold=5)
        n_dropped_dupes = len(dropped_dupes)
        if dropped_dupes:
            log.info(
                "content_dedup",
                kept=len(all_results),
                dropped=len(dropped_dupes),
            )

    all_results.sort(
        key=lambda r: (
            -(4 - r.get("tier", 3)) * 100
            - r.get("bm25_score", r.get("relevance_score", 0)) * 10,
            r.get("host", ""),
        )
    )
    all_results = _diversify(all_results, max_per_domain)

    # WS2b: one-call LLM relevance rerank over the diversified top corpus.
    # Budget guard: needs ≥60s left — a rerank that eats the extract budget
    # is a net loss.
    llm_reranked_n = 0
    if llm_active and all_results and time.monotonic() < call_deadline - 60:
        all_results, llm_reranked_n = _rerank_results_llm(
            initial_query, all_results, model=llm_model
        )
        if llm_reranked_n:
            log.info("llm_rerank", n_scored=llm_reranked_n, corpus=len(all_results))

    if top_n_extract > 0 and all_results:
        extracted = _extract_top_urls(
            ddgs,
            all_results,
            top_n_extract,
            extract_chars,
            query_terms,
            tier_budget=tier_budget,
            query=initial_query,
        )

    # WS7: cited, quote-grounded synthesis over the final corpus. Runs even
    # past the call deadline — the answer is the point of --summarize, and
    # the research itself already obeyed every budget guard above.
    synthesis: str | None = None
    citation_grounding: list[dict[str, Any]] = []
    ungrounded_citations = 0
    if summarize and extracted:
        log.info(
            "agentic_synthesis_start",
            sources=len(all_results),
            extracted=len(extracted),
        )
        synthesis, _verified_cites, citation_grounding = _synthesize_llm(
            initial_query, extracted, all_results, model=llm_model
        )
        ungrounded_citations = sum(
            1 for g in citation_grounding if not g.get("grounded")
        )

    return {
        "schema": OUTPUT_SCHEMA_VERSION,
        "initial_query": initial_query,
        "final_query": cur_query,
        "query_language": query_lang,
        "llm_assisted": llm_active,
        "llm_provider": llm_provider if llm_active else None,
        "llm_reranked_n": llm_reranked_n,
        "iterations": iterations,
        "n_iterations": len(iterations),
        "n_total_results": len(all_results),
        "n_extracted": len(extracted),
        "n_unique_domains": len({r.get("host", "") for r in all_results}),
        "n_excluded_urls": len(
            seen_canon & {canonicalize_url(u) for u in (exclude_urls or [])}
        ),
        "n_dropped_dupes": n_dropped_dupes,
        "status": status,
        "terminal_error": terminal_error,
        "results": all_results,
        "citations": (
            [_build_citation(r) for r in all_results] if DEFAULT_EMIT_CITATIONS else []
        ),
        "extracted": extracted,
        "summarized": bool(synthesis),
        "synthesis": synthesis,
        "citation_grounding": citation_grounding or None,
        "ungrounded_citations": ungrounded_citations,
        "query_terms": sorted(query_terms),
        "metrics": _get_metrics().snapshot(),
    }


def _diversify(
    results: list[dict[str, Any]],
    max_per_domain: int,
) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    out = []
    for r in results:
        h = r.get("host", "")
        if counts[h] < max_per_domain:
            out.append(r)
            counts[h] += 1
    return out


# --------------------------------------------------------------------------- #
# RESEARCH ORCHESTRATOR — thin composite over existing primitives.
#   Calls _agentic_loop (search+extract) + _crawl_site (deep) + adds:
#   - key_findings: best query-relevant passage per page (progressive disclosure)
#   - confidence: convergence-based 0-1 score
#   - next_steps_hint: specific suggestion for going deeper
#   Stateless — each call independent. Agent controls depth via --depth flag.
# --------------------------------------------------------------------------- #


def _extract_key_passage(
    content: str, query_terms: set[str], max_chars: int = 300
) -> tuple[str, float]:
    """Find the most query-relevant paragraph in extracted content.
    Returns (passage, relevance_score 0-1). Used for progressive disclosure —
    the agent reads key_findings FIRST (~2K chars) before reading full pages."""
    if not content:
        return "", 0.0
    if not query_terms:
        return content[:max_chars], 0.0
    # Split into paragraphs (double-newline first, single-newline fallback)
    paragraphs = [p.strip() for p in content.split("\n\n") if len(p.strip()) > 50]
    if not paragraphs:
        paragraphs = [p.strip() for p in content.split("\n") if len(p.strip()) > 50]
    if not paragraphs:
        return content[:max_chars], 0.0

    best_score = -1.0
    best_passage = ""
    best_relevance = 0.0
    for para in paragraphs:
        if _is_stats_methods_noise(para, query_terms):
            continue  # F-49: methods-math passage, not subject evidence
        tokens = set(
            t.strip(".,;:!?()[]\"'`/—–-").lower()
            for t in para.split()
            if len(t.strip(".,;:!?()[]\"'`/—–-")) > 1
        )
        overlap = len(query_terms & tokens)
        if overlap == 0:
            continue
        density = overlap / max(len(tokens), 1)
        score = overlap * 2.0 + density * 10.0
        relevance = min(overlap / max(len(query_terms), 1), 1.0)
        if score > best_score:
            best_score = score
            best_passage = para
            best_relevance = relevance

    if not best_passage:
        # No paragraph had query-term overlap — return the first substantive one
        best_passage = paragraphs[0]
        best_relevance = 0.0

    if len(best_passage) > max_chars:
        best_passage = best_passage[:max_chars].rsplit(" ", 1)[0] + "…"
    return _strip_md_syntax(best_passage), round(best_relevance, 2)


def _evidence_window(content: str, query_terms: set[str], window: int = 4000) -> str:
    """Best query-relevant window of content for LLM evidence (P0-2).

    Replaces blind head-truncation in _synthesize_llm: the model used to see
    only content[:4000], so a key passage deep in a long page could never be
    quoted by the model or verified by the grounding gate. Anchors on the
    best-scoring paragraph (same overlap*2 + density*10 formula as
    _extract_key_passage — kept duplicated, not shared: that helper is
    pinned by its own tests and must not gain callers-coupled churn), then
    grows the window contiguously toward the higher-scoring neighbor until
    the char budget fills. Fallbacks: no query overlap anywhere -> head
    window (never empty evidence); WEB_SEARCH_EVIDENCE_MODE=head restores
    legacy head-truncation for A/B comparison.
    """
    if not content:
        return ""
    if os.getenv("WEB_SEARCH_EVIDENCE_MODE", "passage").strip().lower() == "head":
        return content[:window]
    paragraphs = [p.strip() for p in content.split("\n\n") if len(p.strip()) > 50]
    if not paragraphs:
        paragraphs = [p.strip() for p in content.split("\n") if len(p.strip()) > 50]
    if not paragraphs:
        return content[:window]

    def _score(para: str) -> float:
        if _is_stats_methods_noise(para, query_terms):
            return -1.0
        tokens = set(
            t.strip(".,;:!?()[]\"'`/—–-").lower()
            for t in para.split()
            if len(t.strip(".,;:!?()[]\"'`/—–-")) > 1
        )
        overlap = len(query_terms & tokens)
        if overlap == 0:
            return -1.0
        return overlap * 2.0 + (overlap / max(len(tokens), 1)) * 10.0

    scores = [_score(p) for p in paragraphs]
    anchor = max(range(len(paragraphs)), key=lambda i: scores[i])
    if scores[anchor] < 0:
        return content[:window]
    lo = hi = anchor
    used = len(paragraphs[anchor])
    while used < window:
        grow_left = lo > 0 and scores[lo - 1] >= 0
        grow_right = hi < len(paragraphs) - 1 and scores[hi + 1] >= 0
        if not grow_left and not grow_right:
            break  # both borders are off-topic noise — stop, don't pad budget
        if grow_left and (not grow_right or scores[lo - 1] >= scores[hi + 1]):
            lo -= 1
            used += len(paragraphs[lo]) + 2
        else:
            hi += 1
            used += len(paragraphs[hi]) + 2
    return "\n\n".join(paragraphs[lo : hi + 1])[:window]


def _compute_confidence(
    extracted: list[dict[str, Any]],
    total_results: int,
    unique_domains: int,
    query_terms: set[str] | None = None,
) -> tuple[float, str]:
    """Research confidence (0-1) based on extraction success, domain diversity,
    result volume, and query-term coverage. Returns (score, reason).

    Weights (sum=1.0):
    - success_rate 0.40  — fraction of extracts with >200 chars of content
    - domain_factor 0.20 — unique source diversity (saturates at 5)
    - result_factor 0.15  — result volume (saturates at 20)
    - coverage_factor 0.25 — query-term coverage across extracts (NEW).
      Without this, a query with 5 extracts from 5 domains hit confidence=1.0
      even if NONE of the extracts mentioned the query's key terms. Coverage
      penalises topical drift so auto-escalation fires when evidence is
      off-target, not just when it's scarce.
    """
    if not extracted:
        if total_results > 0:
            return 0.2, "results_but_no_extractions"
        return 0.0, "no_results"
    n_successful = sum(
        1
        for e in extracted
        if e.get("content") and len(str(e.get("content", ""))) > 200
    )
    success_rate = n_successful / max(len(extracted), 1)
    domain_factor = min(unique_domains / 5.0, 1.0)
    result_factor = min(total_results / 20.0, 1.0)
    # Coverage: fraction of query terms appearing in ANY successful extract.
    # Empty query_terms (e.g. very short query) → neutral 1.0 (no penalty).
    if query_terms and extracted:
        joined = " ".join(
            str(e.get("content") or "").lower() for e in extracted
        ).lower()
        hits = sum(1 for t in query_terms if t.lower() in joined)
        coverage_factor = hits / max(len(query_terms), 1)
    else:
        coverage_factor = 1.0
    confidence = (
        success_rate * 0.40
        + domain_factor * 0.20
        + result_factor * 0.15
        + coverage_factor * 0.25
    )
    if confidence >= 0.7:
        reason = "sufficient_evidence"
    elif confidence >= 0.4:
        reason = "partial_evidence"
    else:
        reason = "insufficient_evidence"
    return round(confidence, 2), reason


def _research_composite(
    ddgs,
    query: str,
    *,
    depth: str = "auto",
    region: str,
    safesearch: str,
    timelimit: str | None,
    backend: str,
    cache_ttl: int,
    neg_cache_ttl: int,
    extract_chars: int,
    call_budget: float,
    proxy_for_log: str,
    exclude_urls: list[str] | None = None,
    top_n_override: int | None = None,
    max_crawl_pages: int | None = None,
    max_iter_override: int | None = None,
    tier_budget: dict[int, int] | None = None,
    llm_mode: str = "auto",
    llm_model: str | None = None,
    summarize: bool = False,
) -> dict[str, Any]:
    """Thin orchestrator: agentic search → extract → key_findings → confidence.

    Reuses _agentic_loop (search+convergence) + _crawl_site (deep). Adds:
    - key_findings: best passage per page (progressive disclosure summary)
    - confidence: 0-1 convergence score
    - next_steps_hint: specific follow-up suggestion
    - crawled: site-wide pages (depth=deep or auto-escalation)

    Depth knobs (None → sane defaults; previously hardcoded to 5/5):
    - top_n_override: pages extracted by the agentic phase (default 12)
    - max_crawl_pages: pages crawled per top-domain (default 10); 0 skips crawl
    - max_iter_override: agentic loop iterations (default DEFAULT_AGENTIC_MAX_ITER)
    """
    query_terms = {t.lower() for t in re.split(r"\s+", query) if len(t) > 2}
    do_extract = depth in ("medium", "deep", "auto")
    do_crawl = depth == "deep"

    # LEVEL 1-3: agentic search + extract (reuse existing engine)
    # None-check (not truthy) so an explicit 0 disables extraction.
    if do_extract:
        top_n = (
            top_n_override
            if top_n_override is not None
            else DEFAULT_AGENTIC_TOP_N_EXTRACT
        )
    else:
        top_n = 0
    max_iter = (
        max_iter_override if max_iter_override is not None else DEFAULT_AGENTIC_MAX_ITER
    )
    payload = _agentic_loop(
        ddgs,
        query,
        max_iter=max_iter,
        llm_mode=llm_mode,
        llm_model=llm_model,
        summarize=summarize,
        max_results_per_iter=DEFAULT_AGENTIC_MAX_RESULTS_PER_ITER,
        top_n_extract=top_n,
        relevance_domains=DEFAULT_AGENTIC_RELEVANCE_DOMAINS,
        max_per_domain=DEFAULT_AGENTIC_MAX_PER_DOMAIN,
        region=region,
        safesearch=safesearch,
        timelimit=timelimit,
        backend=backend,
        cache_ttl=cache_ttl,
        neg_cache_ttl=neg_cache_ttl,
        extract_chars=extract_chars,
        call_budget=call_budget * 0.7,
        proxy_for_log=proxy_for_log,
        exclude_urls=exclude_urls,
        tier_budget=tier_budget,
    )

    results = payload.get("results", [])
    extracted = payload.get("extracted", [])

    # KEY FINDINGS: extract best passage per page (progressive disclosure)
    key_findings = []
    for e in extracted:
        content = str(e.get("content") or "")
        if content and len(content) > 200:
            passage, relevance = _extract_key_passage(content, query_terms)
            if passage:
                key_findings.append(
                    {
                        "url": e.get("url", ""),
                        "title": e.get("title", ""),
                        "passage": passage,
                        "relevance": relevance,
                        "host": e.get("host", ""),
                        "tier": e.get("tier", 3),
                    }
                )
    key_findings.sort(key=lambda x: -x.get("relevance", 0))

    # CONFIDENCE
    unique_domains = len({r.get("host", "") for r in results if r.get("host")})
    confidence, conv_reason = _compute_confidence(
        extracted, len(results), unique_domains, query_terms=query_terms
    )

    # AUTO ESCALATION: if confidence low and depth=auto, try deep
    if depth == "auto" and confidence < 0.6:
        do_crawl = True
        log.info(
            "research_auto_escalating",
            confidence=confidence,
            query=query[:50],
            reason=conv_reason,
        )

    # LEVEL 4 (deep): crawl top 1-2 domains by result count
    # max_crawl_pages: None → default 10 (was hardcoded 5 — research-depth bug B);
    # explicit 0 skips crawl entirely (avoids _crawl_site treating 0 as unlimited).
    crawl_pages = max_crawl_pages if max_crawl_pages is not None else 10
    crawled = []
    if do_crawl and crawl_pages > 0 and results:
        domain_counts: dict[str, int] = defaultdict(int)
        for r in results:
            h = r.get("host", "")
            if h:
                domain_counts[h] += 1
        top_domains = sorted(domain_counts.items(), key=lambda x: -x[1])[:2]
        for domain, _count in top_domains:
            domain_url = next(
                (
                    r.get("href") or r.get("canonical_url")
                    for r in results
                    if r.get("host") == domain
                ),
                None,
            )
            if not domain_url:
                continue
            try:
                crawl_result = _crawl_site(
                    ddgs,
                    domain_url,
                    max_pages=crawl_pages,
                    max_chars=extract_chars,
                    call_deadline=time.monotonic() + (call_budget * 0.15),
                    proxy_for_log=proxy_for_log,
                    query=query,
                )
                if crawl_result.get("pages"):
                    for p in crawl_result["pages"]:
                        if p.get("content") and len(str(p["content"])) > 200:
                            passage, rel = _extract_key_passage(
                                str(p["content"]), query_terms
                            )
                            p["key_passage"] = passage
                            p["passage_relevance"] = rel
                    crawled.extend(crawl_result.get("pages", []))
            except Exception as ex:
                log.warning("research_crawl_failed", domain=domain, error=str(ex)[:80])

    # NEXT STEPS HINT
    next_hint = ""
    if confidence < 0.5:
        next_hint = (
            f"Low confidence ({confidence}). Try more specific terms or: "
            f"research '{query}' --depth deep"
        )
    elif confidence < 0.7:
        top_dom = (
            max(
                (r.get("host", "") for r in results if r.get("host")),
                key=lambda h: sum(1 for r in results if r.get("host") == h),
                default="",
            )
            if results
            else ""
        )
        if top_dom:
            next_hint = (
                f"Partial evidence. For site-wide depth: "
                f"crawl https://{top_dom} --query '{query}' --bfs --max-pages 8"
            )

    # DETERMINE depth_reached
    if crawled:
        depth_reached = "deep"
    elif extracted:
        depth_reached = "medium"
    else:
        depth_reached = "shallow"

    return {
        "schema": OUTPUT_SCHEMA_VERSION,
        "query": query,
        "depth_requested": depth,
        "depth_reached": depth_reached,
        "confidence": confidence,
        "results": results,
        "extracted": extracted,
        "key_findings": key_findings,
        "crawled": crawled,
        # WS7 --summarize: propagate the loop's synthesis verbatim.
        "summarized": payload.get("summarized", False),
        "synthesis": payload.get("synthesis"),
        "citation_grounding": payload.get("citation_grounding"),
        "ungrounded_citations": payload.get("ungrounded_citations", 0),
        "meta": {
            **payload.get("meta", {}),
            "pages_fetched": len(results),
            "pages_extracted": len([e for e in extracted if e.get("content")]),
            "pages_crawled": len(crawled),
            "unique_domains": unique_domains,
            "convergence_reason": conv_reason,
            "next_steps_hint": next_hint,
        },
    }


# --------------------------------------------------------------------------- #
# PRO MODE — Perplexity-style agentic research.
#   decompose → multi-round (search + extract + coverage reflection) → synthesize.
#   Two paths:
#     - heuristic (default, zero-config): noun-phrase decomp + structured summary
#     - --use-llm (opt-in): local Ollama for decomp / reflection / prose synthesis
#   NO hardcoded model names — env (WEB_SEARCH_LLM_MODEL) + CLI (--model).
#   Local Ollama only (§7g). Fail-fast if --use-llm + Ollama down (§5 H1).
# --------------------------------------------------------------------------- #


class OllamaUnavailable(RuntimeError):
    """--use-llm set but Ollama unreachable or model not found. Fail-fast (§5 H1)."""


# --------------------------------------------------------------------------- #
# WS1 provider layer — runtime-agnostic local LLM (2026-09-15).
#   _llm_call / _llm_embed dispatch to the native Ollama API OR any
#   OpenAI-compatible server (/v1/chat/completions): Pallama, vLLM,
#   llama.cpp server, LM Studio. Auto-detection probes /api/tags then
#   /v1/models (2s timeout, process-cached). WEB_SEARCH_LLM_PROVIDER
#   pins the provider explicitly. Egress guard is provider-agnostic.
#   OllamaUnavailable keeps its name across providers: it means "local
#   LLM unavailable", not "Ollama the product".
# --------------------------------------------------------------------------- #

_LLM_UA = "web-search-skill/2.0 (local research CLI)"
_LLM_PROVIDER_CACHE: dict[str, str | None] = {}


def _probe_url(url: str, timeout: float) -> bool:
    """True when a GET answers (any status). Connection errors → False."""
    req = urllib_request.Request(url, method="GET", headers={"User-Agent": _LLM_UA})
    try:
        with urllib_request.urlopen(req, timeout=timeout):
            return True
    except (urllib_error.URLError, OSError, ValueError):
        return False


def _llm_probe(host: str | None = None, timeout: float = 2.0) -> str | None:
    """Detect which local LLM runtime answers at `host`.

    Returns 'ollama' | 'openai' | None. Explicit WEB_SEARCH_LLM_PROVIDER
    (ollama/openai) short-circuits the probe. Result cached per host for the
    process lifetime — the skill is single-shot per CLI call, so one probe
    per run regardless of how many LLM calls the pipeline makes.
    A host already ending in /v1 is an OpenAI-compatible base by construction.
    """
    cfg = _settings.llm
    host = (host or cfg.ollama_host).rstrip("/")
    if cfg.provider in ("ollama", "openai"):
        return cfg.provider
    if host in _LLM_PROVIDER_CACHE:
        return _LLM_PROVIDER_CACHE[host]
    detected: str | None = None
    if host.endswith("/v1"):
        detected = "openai" if _probe_url(f"{host}/models", timeout) else None
    elif _probe_url(f"{host}/api/tags", timeout):
        detected = "ollama"
    elif _probe_url(f"{host}/v1/models", timeout):
        # Ollama also exposes /v1 — native API preferred when both answer.
        detected = "openai"
    _LLM_PROVIDER_CACHE[host] = detected
    return detected


def _llm_egress_guard(host: str) -> None:
    """Provider-agnostic §7g guard: warn when the LLM host is off-machine."""
    parsed = urlparse(host)
    if parsed.hostname not in ("localhost", "127.0.0.1", "::1", None, ""):
        log.warning(
            "llm_egress_nonlocal",
            host=parsed.hostname,
            note="LLM host off-machine — data egress risk (§7g)",
        )


def _openai_call(
    prompt: str,
    *,
    model: str | None = None,
    system: str | None = None,
    host: str | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """POST {host}/v1/chat/completions — the universal local-runtime dialect.

    Non-streaming only (adversarial-review simplicity cap): SSE adds protocol
    surface without changing the returned text. `think`/`num_ctx` are
    Ollama-native options with no OpenAI equivalent — callers pass them to
    _llm_call and they are dropped here on purpose.
    """
    cfg = _settings.llm
    model = model or cfg.model
    host = (host or cfg.ollama_host).rstrip("/")
    timeout = timeout or cfg.timeout
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens if max_tokens is not None else cfg.max_tokens,
            # Research synthesis wants low variance, not creativity.
            "temperature": 0.2,
            "stream": False,
        }
    ).encode("utf-8")
    base = host if host.endswith("/v1") else f"{host}/v1"
    req = urllib_request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib_error.URLError as ex:
        raise OllamaUnavailable(
            f"OpenAI-compatible LLM unreachable at {host}: {ex}. "
            "Is the server running (pallama serve / llama-server / vllm serve)?"
        ) from ex
    except TimeoutError as ex:
        raise OllamaUnavailable(
            f"OpenAI-compatible LLM timed out after {timeout}s at {host} "
            f"(model '{model}')"
        ) from ex
    except json.JSONDecodeError as ex:
        raise OllamaUnavailable(f"LLM returned non-JSON: {ex}") from ex
    if isinstance(payload.get("error"), dict):
        payload["error"] = payload["error"].get("message", "unknown error")
    if "error" in payload:
        raise OllamaUnavailable(f"LLM error for model '{model}': {payload['error']}")
    try:
        return _strip_inline_think(payload["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as ex:
        raise OllamaUnavailable(f"Malformed chat.completions response: {ex}") from ex


_LIGHT_MODEL_CACHE: dict[str, str] = {}


def _pick_light_model(host: str) -> str:
    """Smallest suitable chat model on the Ollama host for routing stages
    (query refine/decompose/gap/rerank). Excludes embedding/vision/OCR/clip
    models by name; falls back to the main model (ALWAYS logged — §6i).
    Cached per host for the session."""
    cached = _LIGHT_MODEL_CACHE.get(host)
    if cached:
        return cached
    cfg = _settings.llm
    chosen, why = cfg.model, "fallback-main"
    try:
        with urllib_request.urlopen(
            urllib_request.Request(f"{host.rstrip('/')}/api/tags"), timeout=5
        ) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        banned = ("embed", "vision", "ocr", "clip")
        chat_models = [
            m
            for m in tags.get("models", [])
            if not any(b in (m.get("name") or "").lower() for b in banned)
        ]
        if chat_models:
            smallest = min(chat_models, key=lambda m: m.get("size") or 0)
            chosen, why = smallest.get("name"), f"smallest-of-{len(chat_models)}"
    except (
        urllib_error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        ValueError,
    ) as ex:
        log.warning("llm_light_model_probe_failed", host=host, error=str(ex))
    _LIGHT_MODEL_CACHE[host] = chosen
    log.info("llm_light_model_routed", model=chosen, reason=why, host=host)
    return chosen


def _llm_call(
    prompt: str,
    *,
    model: str | None = None,
    system: str | None = None,
    host: str | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
    think: bool = False,
    stream: bool = False,
    num_ctx: int | None = None,
    light: bool = False,
) -> str:
    """Provider-agnostic generation entry (all research call sites use this).

    Dispatch: probe once → native Ollama API or OpenAI-compatible server.
    Raises OllamaUnavailable when no runtime answers — callers already map
    that to the fail-fast contract (§5 H1).

    light=True routes routing-class work (refine/decompose/gap/rerank) to
    the small model: explicit `model` still wins, else WEB_SEARCH_LLM_LIGHT_MODEL,
    else auto-pick (smallest non-embed/vision model on the host). Synthesis
    and other heavy stages stay on the main model (P0-3)."""
    cfg = _settings.llm
    host = host or cfg.ollama_host
    if light and model is None:
        model = cfg.light_model or _pick_light_model(host)
    _llm_egress_guard(host)
    provider = _llm_probe(host=host)
    if provider == "openai":
        if stream:
            log.info(
                "openai_stream_unsupported",
                note="v1 path returns complete text in one body",
            )
        return _openai_call(
            prompt,
            model=model,
            system=system,
            host=host,
            timeout=timeout,
            max_tokens=max_tokens,
        )
    if provider is None:
        raise OllamaUnavailable(
            f"No local LLM runtime at {host} (probed /api/tags and /v1/models). "
            "Start one (ollama serve / pallama serve / llama-server) or set "
            "WEB_SEARCH_LLM_PROVIDER explicitly."
        )
    return _ollama_call(
        prompt,
        model=model,
        system=system,
        host=host,
        timeout=timeout,
        max_tokens=max_tokens,
        think=think,
        stream=stream,
        num_ctx=num_ctx,
    )


def _openai_embed(
    text: str,
    *,
    model: str | None = None,
    host: str | None = None,
    timeout: float | None = None,
) -> list[float]:
    """POST {host}/v1/embeddings (OpenAI-compatible runtimes)."""
    cfg = _settings.llm
    model = model or cfg.embed_model
    host = (host or cfg.ollama_host).rstrip("/")
    timeout = timeout or cfg.timeout
    body = json.dumps({"model": model, "input": text}).encode("utf-8")
    base = host if host.endswith("/v1") else f"{host}/v1"
    req = urllib_request.Request(
        f"{base}/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib_error.URLError as ex:
        raise OllamaUnavailable(
            f"OpenAI-compatible embeddings unreachable at {host}: {ex}"
        ) from ex
    if "error" in payload:
        raise OllamaUnavailable(f"LLM embed error for '{model}': {payload['error']}")
    try:
        return payload["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as ex:
        raise OllamaUnavailable(f"Malformed embeddings response: {ex}") from ex


def _llm_embed(
    text: str,
    *,
    model: str | None = None,
    host: str | None = None,
    timeout: float | None = None,
) -> list[float]:
    """Provider-agnostic embedding entry — mirrors _llm_call dispatch."""
    host = host or _settings.llm.ollama_host
    _llm_egress_guard(host)
    if _llm_probe(host=host) == "openai":
        return _openai_embed(text, model=model, host=host, timeout=timeout)
    return _ollama_embed(text, model=model, host=host, timeout=timeout)


def _ollama_call(
    prompt: str,
    *,
    model: str | None = None,
    system: str | None = None,
    host: str | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
    think: bool = False,
    stream: bool = False,
    num_ctx: int | None = None,
) -> str:
    """POST to Ollama /api/generate. Returns the `response` field (ignores
    `thinking`). Stdlib urllib only — no new pip dep (§7c). Raises
    OllamaUnavailable on any failure.

    Latency/quality controls:
    - think=False (default): disables chain-of-thought → up to 44x faster.
    - stream=True: reads NDJSON line-by-line, logs progress to stderr.
    - num_ctx: override Ollama context window (default 4096). Set higher for
      synthesis with large evidence (e.g. 32768 for 20×4000-char sources).
    """
    cfg = _settings.llm
    model = model or cfg.model
    host = host or cfg.ollama_host
    timeout = timeout or cfg.timeout
    parsed = urlparse(host)
    if parsed.hostname not in ("localhost", "127.0.0.1", "::1", None, ""):
        log.warning(
            "llm_egress_nonlocal",
            host=parsed.hostname,
            note="WEB_SEARCH_OLLAMA_HOST off-machine — data egress risk (§7g)",
        )
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": stream,
        "think": think,
        # keep the model resident between pro rounds — avoids a cold reload
        # (~10-40s on CPU) before every light routing call (P0-3; mirrors
        # the scientific-research _llm_extract precedent).
        "keep_alive": "30m",
    }
    if system:
        body["system"] = system
    opts: dict[str, Any] = {
        "num_predict": max_tokens if max_tokens is not None else cfg.max_tokens
    }
    if num_ctx is not None:
        opts["num_ctx"] = num_ctx
    body["options"] = opts
    data = json.dumps(body).encode("utf-8")
    req = urllib_request.Request(
        f"{host.rstrip('/')}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            if not stream:
                payload = json.loads(resp.read().decode("utf-8"))
                if "error" in payload:
                    raise OllamaUnavailable(
                        f"Ollama error for model '{model}': {payload['error']}. "
                        f"Pull with `ollama pull {model}`."
                    )
                return _strip_inline_think(payload.get("response", ""))
            # Streaming: read NDJSON line-by-line. Collect `response` tokens,
            # skip `thinking` tokens. Log progress every ~200 chars.
            parts: list[str] = []
            total = 0
            next_log = 200
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "error" in chunk:
                    raise OllamaUnavailable(
                        f"Ollama error for model '{model}': {chunk['error']}. "
                        f"Pull with `ollama pull {model}`."
                    )
                tok = chunk.get("response", "")
                if tok:
                    parts.append(tok)
                    total += len(tok)
                    if total >= next_log:
                        log.info("llm_stream_progress", chars=total)
                        next_log = total + 200
                if chunk.get("done"):
                    break
            return _strip_inline_think("".join(parts))
    except urllib_error.URLError as ex:
        raise OllamaUnavailable(
            f"Ollama unreachable at {host}: {ex}. "
            f"Run `ollama serve` or drop --use-llm for the heuristic path."
        ) from ex
    except TimeoutError as ex:
        # Mid-read socket timeout (urlopen raises URLError only on connect;
        # a stalled generation surfaces as raw TimeoutError). Wrap it so the
        # caller gets the structured OllamaUnavailable contract instead of a
        # traceback with empty stdout (live-observed on slow think runs).
        raise OllamaUnavailable(
            f"Ollama generation timed out after {timeout}s at {host} "
            f"(model '{model}' — raise WEB_SEARCH_LLM_TIMEOUT or use --no-think)"
        ) from ex
    except json.JSONDecodeError as ex:
        raise OllamaUnavailable(f"Ollama returned non-JSON: {ex}") from ex


def _ollama_embed(
    text: str,
    *,
    model: str | None = None,
    host: str | None = None,
    timeout: float | None = None,
) -> list[float]:
    """POST to Ollama /api/embeddings. Returns the embedding vector.
    Uses nomic-embed-text by default (768-dim). Raises OllamaUnavailable."""
    cfg = _settings.llm
    model = model or cfg.embed_model
    host = host or cfg.ollama_host
    timeout = timeout or cfg.timeout
    body = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib_request.Request(
        f"{host.rstrip('/')}/api/embeddings",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib_error.URLError as ex:
        raise OllamaUnavailable(
            f"Ollama embeddings unreachable at {host}: {ex}. Pull with `ollama pull {model}`."
        ) from ex
    if "error" in payload:
        raise OllamaUnavailable(
            f"Ollama embed error for model '{model}': {payload['error']}."
        )
    return payload.get("embedding", [])


# Domains that are useless to crawl (search aggregators, social media feeds,
# link farms). Auto-crawl skips these to avoid wasting budget on pages that
# don't contain research-relevant content.
_CRAWL_BLOCKLIST = frozenset(
    {
        "google.com",
        "www.google.com",
        "bing.com",
        "www.bing.com",
        "duckduckgo.com",
        "search.yahoo.com",
        "baidu.com",
        "twitter.com",
        "x.com",
        "facebook.com",
        "instagram.com",
        "reddit.com",
        "pinterest.com",
        "linkedin.com",
        "youtube.com",
        "tiktok.com",
    }
)


def _strip_inline_think(text: str) -> str:
    """Strip model reasoning leakage from a response.

    Three shapes handled:
    1. Full inline block: "<think>...</think>" pairs removed (lfm2.5 style).
    2. Lone closer (F-39): qwen3.5-family via Ollama streams its reasoning to
       a separate `thinking` field but may still emit a stray `</think>` —
       everything BEFORE the last closer is deliberation, keep only the tail.
       (Live-observed: 10,396 chars of CoT shipped ahead of the real answer.)
    3. Unclosed "<think>": the model never finished reasoning → return empty
       (caller surfaces the truncation instead of hallucinating a bridge)."""
    if "</think>" in text:
        # Case 1: matched pairs (may be several).
        cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
        # Case 2: leftover closers with no opener → drop the prefix entirely.
        if "</think>" in cleaned:
            cleaned = cleaned.rsplit("</think>", 1)[1].lstrip()
        if "<think>" in cleaned:
            log.warning(
                "llm_think_unclosed",
                hint="reasoning incomplete — try higher max_tokens",
            )
            return ""
        return cleaned.strip()
    if "<think>" in text:
        log.warning(
            "llm_think_unclosed", hint="reasoning incomplete — try higher max_tokens"
        )
        return ""
    return text


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Returns 0-1 (clamped)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def _compute_semantic_coverage(
    query: str,
    extracted: list[dict[str, Any]],
    *,
    embed_model: str | None = None,
    threshold: float = 0.45,
) -> tuple[float, list[dict[str, Any]]]:
    """Semantic coverage via embedding cosine similarity.

    Embeds the query + each extract's key passage, computes max cosine.
    Returns (best_cosine 0-1, per_passage_scores).
    Catches paraphrased evidence keyword coverage misses (e.g. 'share creation'
    covered when query asks 'ETF creation').

    F-29: OllamaUnavailable PROPAGATES — the caller opted into
    --semantic-coverage, so a silent 0.0 would masquerade as "measured,
    no coverage". The pro CLI handler converts it to the structured
    ollama_unavailable error (same fail-fast contract as --use-llm)."""
    if not extracted:
        return 0.0, []
    q_emb = _llm_embed(query, model=embed_model)
    if not q_emb:
        return 0.0, []
    scores: list[dict[str, Any]] = []
    best = 0.0
    for e in extracted:
        content = str(e.get("content") or "")
        if len(content) < 200:
            continue
        # Embed first 2000 chars (representative sample, bounds latency).
        p_emb = _llm_embed(content[:2000], model=embed_model)
        cos = _cosine_similarity(q_emb, p_emb)
        scores.append(
            {
                "url": e.get("url") or e.get("href") or "",
                "cosine": round(cos, 3),
                "relevant": cos >= threshold,
            }
        )
        if cos > best:
            best = cos
    return round(best, 3), scores


# --- query decomposition ---

_DECOMP_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "versus",
        "vs",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "how",
        "does",
        "do",
        "is",
        "are",
        "was",
        "were",
        "what",
        "why",
        "when",
        "which",
        "who",
        "can",
        "could",
        "should",
        "would",
        "will",
        "between",
        "into",
        "from",
        "by",
        "at",
        "as",
        "about",
        "like",
        "than",
        "then",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "be",
    }
)


def _decompose_query_heuristic(query: str) -> list[str]:
    """Split a research query into sub-query facets without an LLM.

    Strategy: split on conjunctions, keep noun-ish chunks >3 chars, always
    include the original query as facet[0]. Returns 1-6 sub-queries."""
    q = query.strip()
    if not q:
        return [q]
    parts = re.split(
        r"\s+(?:and|or|versus|vs\.?|as well as|compared to?)\s+", q, flags=re.I
    )
    facets: list[str] = []
    seen: set[str] = set()
    for p in parts:
        p = p.strip(" .,;:!?")
        if not p or len(p) < 4:
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        facets.append(p)
    if not facets:
        facets = [q]
    if facets[0].lower() != q.lower():
        facets.insert(0, q)
    return facets[:6]


# F-40: models leak meta-commentary ("The user wants me to...", "Let me
# identify...") into line-oriented outputs despite "no preamble" instructions
# (live-observed: 2 of 6 sub-queries were garbage and were fired at backends).
# Filter those lines at every LLM list boundary.
_LLM_META_MARKERS = (
    "the user",
    "let me",
    "i need to",
    "i should",
    "i will",
    "i'll",
    "i'm going",
    "research question:",
    "sub-questions",
    "based on",
    "as an ai",
)


def _filter_llm_lines(raw: str, min_len: int, max_items: int) -> list[str]:
    """Clean an LLM line-list: strip bullets/numbering, drop meta-commentary,
    enforce min length, cap item count. Meta markers are matched against the
    line PREFIX only — mid-sentence phrases like "strategies based on evidence"
    are legitimate content, "Based on the question, I will..." is commentary."""
    out: list[str] = []
    for ln in raw.splitlines():
        ln = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", ln.strip())
        if len(ln) < min_len:
            continue
        head = ln[:60].lower()
        if any(head.startswith(m) for m in _LLM_META_MARKERS):
            continue
        out.append(ln)
    return out[:max_items]


def _decompose_query_llm(
    query: str, *, model: str | None = None, think: bool = False
) -> list[str]:
    """Decompose a research query into 3-6 sub-questions via local Ollama."""
    system = (
        "You are a research analyst. Break the user's research question into "
        "3-6 distinct sub-questions that together cover all important aspects. "
        "Output ONLY the sub-questions, one per line. No numbering, no preamble."
    )
    prompt = f"Research question: {query}\n\nSub-questions:"
    raw = _llm_call(prompt, model=model, system=system, think=think, light=True)
    subs = _filter_llm_lines(raw, min_len=10, max_items=6)
    if not subs:
        log.warning("llm_decompose_empty", query=query[:60])
        return [query]
    return subs


# --- coverage ---


def _compute_pro_coverage(
    query_terms: set[str],
    extracted: list[dict[str, Any]],
    *,
    min_sources: int = 2,
) -> tuple[float, dict[str, list[str]]]:
    """Fraction of query terms found in ≥min_sources distinct extracts.

    Returns (frac, per_term_urls). Tightened from ≥1 source to ≥2 because
    auto-crawl reads 20+ pages — a term in only 1 page is weakly covered,
    not fully answered. This prevents coverage from saturating to 1.0 on
    every query (which wasted rounds re-searching already-covered topics)."""
    if not query_terms or not extracted:
        return 0.0, {}
    # Can't require more sources than we have extracts.
    effective_min = min(min_sources, len(extracted))
    per_term: dict[str, list[str]] = {}
    for term in query_terms:
        term_lower = term.lower()
        hits: list[str] = []
        for e in extracted:
            if term_lower in str(e.get("content") or "").lower():
                url = e.get("url") or e.get("href") or ""
                if url and url not in hits:
                    hits.append(url)
        per_term[term] = hits
    covered = sum(1 for hits in per_term.values() if len(hits) >= effective_min)
    return covered / max(len(query_terms), 1), per_term


def _generate_gap_queries_heuristic(
    query_terms: set[str],
    coverage: dict[str, list[str]],
    original_query: str,
) -> list[str]:
    """Heuristic gap queries: pair uncovered terms with the original query."""
    uncovered = [t for t, hits in coverage.items() if not hits]
    if not uncovered:
        return []
    gaps: list[str] = []
    for i in range(0, len(uncovered), 2):
        pair = uncovered[i : i + 2]
        gaps.append(f"{original_query} {' '.join(pair)}")
    return gaps[:4]


def _generate_gap_queries_llm(
    original_query: str,
    coverage: dict[str, list[str]],
    *,
    model: str | None = None,
    think: bool = False,
) -> list[str]:
    """LLM-driven gap analysis: what's still unanswered?"""
    uncovered = [t for t, hits in coverage.items() if not hits]
    covered = [t for t, hits in coverage.items() if hits]
    system = (
        "You are a research analyst. Given a question and coverage so far, "
        "output 2-4 NEW search queries that would fill the evidence gaps. "
        "Output ONLY queries, one per line. No preamble."
    )
    prompt = (
        f"Question: {original_query}\n"
        f"Already covered: {', '.join(covered[:10]) or '(none)'}\n"
        f"Not yet covered: {', '.join(uncovered[:10]) or '(none)'}\n\n"
        f"Gap-filling search queries:"
    )
    raw = _llm_call(prompt, model=model, system=system, think=think, light=True)
    return _filter_llm_lines(raw, min_len=5, max_items=4)


# --- synthesis ---


def _find_source_index(url: str, sources: list[dict[str, Any]]) -> int:
    for i, s in enumerate(sources):
        if s.get("url") == url or s.get("href") == url:
            return i
    return -1


def _synthesize_heuristic(
    query: str,
    key_findings: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    coverage: dict[str, list[str]],
) -> str:
    """Structured markdown synthesis — no prose generation (no LLM).
    Ranks findings by relevance, cites as [[n]] mapping to sources index."""
    if not key_findings:
        return (
            f"# {query}\n\nNo substantive evidence extracted. "
            f"Try broader terms, --depth deep, or --max-rounds 4."
        )
    lines = [f"# {query}\n", "## Key findings\n"]
    findings_sorted = sorted(key_findings, key=lambda x: -x.get("relevance", 0))
    for i, kf in enumerate(findings_sorted[:12]):
        src_idx = _find_source_index(kf.get("url", ""), sources)
        cite = f" [[{src_idx + 1}]]" if src_idx >= 0 else ""
        passage = kf.get("passage", "").strip()
        lines.append(f"{i + 1}. {passage}{cite}")
    covered = sum(1 for hits in coverage.values() if hits)
    total = max(len(coverage), 1)
    lines.append(f"\n## Coverage\n\n{covered}/{total} query terms found in evidence.")
    uncovered = [t for t, hits in coverage.items() if not hits]
    if uncovered:
        lines.append(f" Uncovered: {', '.join(sorted(uncovered))}")
    return "\n".join(lines)


def _normalize_quote_text(s: str) -> str:
    """Collapse whitespace + lowercase — quote/source comparison form."""
    return re.sub(r"\s+", " ", s).strip().lower()


def _quote_in_content(quote_n: str, content_n: str, ratio_floor: float) -> float:
    """Best fuzzy-containment ratio of quote inside content (1.0 = exact).

    Sliding-window SequenceMatcher: window = qlen+20, step = qlen//4,
    early-break at >= 0.95 (near-exact containment found). difflib is pure
    Python, so window scan is bounded: 4000-char content, ~200-char quote
    = ~76 windows — milliseconds per quote, only for cited sources.
    """
    if not quote_n or not content_n:
        return 0.0
    qlen = len(quote_n)
    # Length guard FIRST: a <8-char fragment substring-matching anywhere is
    # meaningless evidence — reject before the exact-substring fast path.
    if qlen < 8:
        return 0.0
    if quote_n in content_n:
        return 1.0
    matcher = difflib.SequenceMatcher(None, quote_n, "", autojunk=False)
    window = qlen + 20
    step = max(1, qlen // 4)
    best = 0.0
    for start in range(0, max(1, len(content_n) - window + 1), step):
        matcher.set_seq2(content_n[start : start + window])
        r = matcher.ratio()
        if r > best:
            best = r
            if best >= 0.95:
                break
    return best


def _verify_quote_grounding(
    synthesis_raw: str,
    sources: list[dict[str, Any]],
    extracted: list[dict[str, Any]],
    ratio_floor: float = 0.65,
    query: str = "",
) -> tuple[list[dict[str, Any]], str]:
    """Quote-grounding gate (anti-hallucination §5 H7): every quote the LLM
    claims came from source n must fuzzy-exist in that source's extracted
    content. Returns (records, synthesis_without_sources_section).

    Records flag grounded/ungrounded — flagged quotes are NEVER removed
    (agent decides what to trust); this verifies, it does not censor.
    Quotes are capped at 40 words in the prompt; comparison is
    whitespace/lowercase-normalized (markdown rendering drift tolerated).

    Scan contract (P0-2): exact substring is checked against the FULL
    content; the fuzzy window scan uses the SAME evidence window the model
    saw (_evidence_window with the query's terms, head window when query
    is unknown), so verification cost stays bounded and passage-mode quotes
    are never falsely flagged.
    """
    m = re.search(r"SOURCES:\s*\n(.*)\Z", synthesis_raw, re.DOTALL)
    if not m:
        return [], synthesis_raw
    query_terms = {t.lower() for t in query.split() if len(t) > 2}
    records: list[dict[str, Any]] = []
    for line in m.group(1).splitlines():
        lm = re.match(r"^\s*(\d{1,2})\.\s*[\"'\u201c](.+?)[\"'\u201d]\s*$", line)
        if not lm:
            continue
        idx = int(lm.group(1))
        quote = lm.group(2)
        content_full = ""
        scan = ""
        if 1 <= idx <= len(sources):
            s_url = sources[idx - 1].get("url") or sources[idx - 1].get("href")
            for e in extracted:
                if (e.get("url") or e.get("href")) == s_url:
                    content_full = str(e.get("content") or "")
                    if query_terms:
                        scan = _evidence_window(content_full, query_terms)
                    else:
                        scan = content_full[:4000]
                    break
        quote_n = _normalize_quote_text(quote)
        if quote_n and quote_n in _normalize_quote_text(content_full):
            ratio = 1.0
        else:
            ratio = (
                _quote_in_content(
                    quote_n,
                    _normalize_quote_text(scan),
                    ratio_floor,
                )
                if content_full
                else 0.0
            )
        records.append(
            {
                "index": idx,
                "quote": quote[:200],
                "grounded": bool(ratio >= ratio_floor),
                "ratio": round(ratio, 3),
            }
        )
    stripped = synthesis_raw[: m.start()].rstrip()
    if records:
        ungrounded = sum(1 for r in records if not r["grounded"])
        log.info(
            "llm_quote_grounding",
            quotes=len(records),
            ungrounded=ungrounded,
            ratio_floor=ratio_floor,
        )
    return records, stripped


def _synthesize_llm(
    query: str,
    extracted: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    *,
    model: str | None = None,
    think: bool = False,
    stream: bool = False,
) -> tuple[str, list[int], list[dict[str, Any]]]:
    """LLM prose synthesis with [n] citations + quote-grounding gate.
    Post-checks every [n] maps to a real source (anti-hallucination §5 H7),
    then verifies each SOURCES: quote fuzzy-exists in the cited source's
    content. think=False for speed (44x faster); stream=True for progressive
    stderr output. Evidence cap: 20 sources × 4000 chars = 80K chars
    (~25K tokens). num_ctx=32768 ensures Ollama doesn't truncate (default
    num_ctx=4096 would lose most evidence)."""
    src_for_llm = sources[:20]
    query_terms = {t.lower() for t in query.split() if len(t) > 2}
    evidence_lines = []
    for i, s in enumerate(src_for_llm):
        content = ""
        for e in extracted:
            if (e.get("url") or e.get("href")) == (s.get("url") or s.get("href")):
                content = _evidence_window(str(e.get("content") or ""), query_terms)
                break
        if content:
            evidence_lines.append(
                f"[{i + 1}] {s.get('title', '')} ({s.get('host', '')}):\n{content}"
            )
    evidence = "\n\n".join(evidence_lines)
    if not evidence:
        return f"# {query}\n\nInsufficient evidence to synthesize.", [], []
    system = (
        "You are a research analyst. Answer using ONLY the evidence provided. "
        "Cite every claim with [n] matching the source number. If evidence does "
        "not cover an aspect, say so explicitly. Do not fabricate citations. "
        "After your answer, output a final line 'SOURCES:' followed by one line "
        'per cited source: n. "verbatim quote from that source, max 40 words" '
        "— copy each quote EXACTLY from the evidence."
    )
    prompt = (
        f"Question: {query}\n\nEvidence:\n{evidence}\n\n"
        f"Answer (cite as [1], [2], etc. matching source numbers above). "
        f"End your reply with a line 'SOURCES:' then one line per cited "
        f'source, exactly: n. "verbatim quote from that source, max 40 words"'
    )
    if stream:
        log.info("llm_synthesis_streaming_start", sources=len(src_for_llm))
    # think=True burns ~2-4K tokens on chain-of-thought before the answer.
    # Default num_predict (4096) gets consumed by thinking → empty response.
    # Raise to 8192 when think is on so both thinking + answer fit.
    syn_max_tokens = 8192 if think else _settings.llm.max_tokens
    syn_timeout = 300.0 if think else _settings.llm.timeout
    raw = _llm_call(
        prompt,
        model=model,
        system=system,
        think=think,
        stream=stream,
        num_ctx=32768,
        max_tokens=syn_max_tokens,
        timeout=syn_timeout,
    )
    if stream:
        log.info("llm_synthesis_streaming_done", chars=len(raw))
    cited = sorted({int(m) - 1 for m in re.findall(r"\[(\d+)\]", raw)})
    verified = [c for c in cited if 0 <= c < len(src_for_llm)]
    unverified = [c for c in cited if c < 0 or c >= len(src_for_llm)]
    if unverified:
        log.warning(
            "llm_citation_overflow",
            unverified_indices=[c + 1 for c in unverified],
            note="LLM cited source numbers beyond the provided set",
        )
    grounding, clean_raw = _verify_quote_grounding(
        raw, src_for_llm, extracted, query=query
    )
    return f"# {query}\n\n{clean_raw}", verified, grounding


def _generate_follow_ups_llm(
    query: str,
    synthesis: str,
    *,
    model: str | None = None,
    think: bool = False,
) -> list[str]:
    """LLM suggests 3-5 related follow-up questions (Perplexity-style)."""
    system = (
        "You are a research assistant. Based on the question and synthesis, "
        "suggest 3-5 related follow-up questions a researcher might ask next. "
        "Output ONLY questions, one per line. No preamble."
    )
    prompt = (
        f"Question: {query}\n\nSynthesis (first 1500 chars):\n{synthesis[:1500]}\n\n"
        f"Follow-up questions:"
    )
    raw = _llm_call(prompt, model=model, system=system, think=think)
    follow_ups = _filter_llm_lines(raw, min_len=8, max_items=5)
    return [
        q.rstrip("?").strip() + "?" if q and not q.endswith("?") else q
        for q in follow_ups
    ]


# --- batch search helper (parallel, dedup-aware) ---


def _pro_batch_search(
    ddgs,
    queries: list[str],
    *,
    top_n: int,
    region: str,
    safesearch: str,
    timelimit: str | None,
    backend: str,
    cache_ttl: int,
    neg_cache_ttl: int,
    call_deadline: float,
    proxy_for_log: str,
    seen_canon: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parallel search + extract for a list of sub-queries. Dedup vs seen_canon."""
    results: list[dict[str, Any]] = []
    combined_query = " ".join(queries)

    def _one(q: str):
        try:
            p = _search_with_defense(
                ddgs,
                "text",
                q,
                region=region,
                safesearch=safesearch,
                timelimit=timelimit,
                max_results=_settings.search.max_results,
                page=1,
                backend=backend,
                cache_ttl=cache_ttl,
                neg_cache_ttl=neg_cache_ttl,
                call_deadline=call_deadline,
                proxy_for_log=proxy_for_log,
            )
            return q, p, None
        except SearchError as ex:
            return q, None, str(ex)

    workers = max(1, min(len(queries), _settings.concurrency))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pro") as pool:
        futs = {pool.submit(_one, q): q for q in queries}
        for fut in as_completed(futs):
            q, p, err = fut.result()
            if p is None:
                log.warning("pro_search_error", query=q[:50], error=str(err)[:80])
                continue
            for r in p.get("results", []):
                canon = r.get("canonical_url") or r.get("href") or ""
                if canon and canon in seen_canon:
                    continue
                if canon:
                    seen_canon.add(canon)
                r2 = dict(r)
                r2["sub_query"] = q
                results.append(r2)

    extracted: list[dict[str, Any]] = []
    if top_n > 0 and results:
        results = _bm25_rerank(results, combined_query)
        results = _diversify(results, _settings.agentic.max_per_domain)
        query_terms = {t for t in re.split(r"\s+", combined_query) if len(t) > 2}
        extracted = _extract_top_urls(
            ddgs,
            results,
            top_n,
            _settings.agentic.extract_chars,
            query_terms,
            query=combined_query,
        )
    return results, extracted


# --- orchestrator ---


def _pro_search(
    ddgs,
    query: str,
    *,
    llm_mode: str = "auto",
    model: str | None,
    max_rounds: int,
    coverage_threshold: float,
    top_n_per_facet: int,
    region: str,
    safesearch: str,
    timelimit: str | None,
    backend: str,
    cache_ttl: int,
    neg_cache_ttl: int,
    extract_chars: int,
    call_budget: float,
    proxy_for_log: str,
    think: bool = False,
    stream: bool = False,
    semantic_coverage: bool = False,
    embed_model: str | None = None,
    auto_crawl: bool = True,
    crawl_pages_per_domain: int = 10,
) -> dict[str, Any]:
    """Perplexity-style agentic research.

    decompose → multi-round (search facets + extract + coverage) → synthesize.
    Heuristic path (default) or LLM path (--use-llm). Budget-bounded.

    Latency/quality knobs:
    - think: chain-of-thought on (slower, better reasoning). Default False (44x faster).
    - stream: progressive synthesis output to stderr. Default False.
    - semantic_coverage: embed-based paraphrase-aware coverage. Default False (adds latency).
    """
    call_deadline = time.monotonic() + call_budget
    query_terms = {t.lower() for t in re.split(r"\s+", query) if len(t) > 2}

    # WS2: resolve the LLM path from the tri-state mode. 'on' = explicit
    # opt-in (downstream _llm_call fails fast when nothing answers, §5 H1);
    # 'off' = forced heuristic; 'auto' (default) = probe once, take the LLM
    # when a local runtime answers, heuristic otherwise. The payload always
    # states which path produced the answer — auto is never a silent
    # fallback (the mode + llm_available fields make the selection visible).
    provider = _llm_probe()
    llm_available = provider is not None
    if llm_mode == "off":
        use_llm = False
    elif llm_mode == "on":
        use_llm = True
    else:
        use_llm = llm_available
        log.info(
            "pro_llm_auto",
            selected="llm" if use_llm else "heuristic",
            provider=provider,
        )

    # PHASE 1: decompose
    if use_llm:
        sub_queries = _decompose_query_llm(query, model=model, think=think)
    else:
        sub_queries = _decompose_query_heuristic(query)
    log.info(
        "pro_decomposed",
        query=query[:50],
        n_subqueries=len(sub_queries),
        path="llm" if use_llm else "heuristic",
        sub_queries=sub_queries,
    )

    # PHASE 2: multi-round search + extract + coverage reflection
    all_results: list[dict[str, Any]] = []
    all_extracted: list[dict[str, Any]] = []
    seen_canon: set[str] = set()
    rounds_meta: list[dict[str, Any]] = []

    for round_idx in range(max_rounds):
        if time.monotonic() > call_deadline:
            rounds_meta.append({"round": round_idx, "status": "budget_exhausted"})
            break

        if round_idx == 0:
            active_queries = sub_queries
        else:
            cov_frac, cov_map = _compute_pro_coverage(query_terms, all_extracted)
            if cov_frac >= coverage_threshold:
                rounds_meta.append(
                    {
                        "round": round_idx,
                        "status": "coverage_met",
                        "coverage": round(cov_frac, 2),
                    }
                )
                break
            if use_llm:
                active_queries = _generate_gap_queries_llm(
                    query, cov_map, model=model, think=think
                )
            else:
                active_queries = _generate_gap_queries_heuristic(
                    query_terms, cov_map, query
                )
            if not active_queries:
                rounds_meta.append(
                    {
                        "round": round_idx,
                        "status": "no_gaps",
                        "coverage": round(cov_frac, 2),
                    }
                )
                break
            log.info(
                "pro_gap_round",
                round=round_idx,
                coverage=round(cov_frac, 2),
                n_gap_queries=len(active_queries),
            )

        round_deadline = min(call_deadline, time.monotonic() + call_budget * 0.3)
        round_results, round_extracted = _pro_batch_search(
            ddgs,
            active_queries,
            top_n=top_n_per_facet,
            region=region,
            safesearch=safesearch,
            timelimit=timelimit,
            backend=backend,
            cache_ttl=cache_ttl,
            neg_cache_ttl=neg_cache_ttl,
            call_deadline=round_deadline,
            proxy_for_log=proxy_for_log,
            seen_canon=seen_canon,
        )
        all_results.extend(round_results)
        all_extracted.extend(round_extracted)
        rounds_meta.append(
            {
                "round": round_idx,
                "status": "ok",
                "n_queries": len(active_queries),
                "n_new_results": len(round_results),
                "n_new_extracts": len(round_extracted),
                "queries": active_queries,
            }
        )
        if round_idx > 0 and len(round_extracted) < 2:
            rounds_meta[-1]["status"] = "converged"
            break

    # PHASE 2b: auto-crawl top domains for site-wide depth.
    # Ranks domains by result count, crawls top-3 (skipping search engines/social
    # media via _CRAWL_BLOCKLIST). Adds ~20-30 full pages of evidence that search
    # snippets can't surface. Budget: 25% of call_budget.
    crawl_meta: list[dict[str, Any]] = []
    if auto_crawl and all_results:
        domain_counts: dict[str, int] = defaultdict(int)
        for r in all_results:
            h = r.get("host", "")
            if h and h not in _CRAWL_BLOCKLIST:
                domain_counts[h] += 1
        # Top 3 domains by result count, blocklist-filtered
        top_domains = sorted(domain_counts.items(), key=lambda x: -x[1])[:3]
        # Protected crawl budget: guarantee at least 20% of call_budget for crawl,
        # even if LLM decomposition/gap calls consumed most of the time.
        # This fixes the benchmark finding where pro-LLM read fewer pages than
        # pro-heuristic (LLM calls ate the crawl budget).
        crawl_budget_remaining = max(0, call_deadline - time.monotonic())
        crawl_min = call_budget * 0.20
        crawl_deadline = time.monotonic() + max(crawl_budget_remaining, crawl_min)
        for domain, count in top_domains:
            if time.monotonic() > crawl_deadline:
                break
            domain_url = next(
                (
                    r.get("href") or r.get("canonical_url")
                    for r in all_results
                    if r.get("host") == domain
                ),
                None,
            )
            if not domain_url:
                continue
            try:
                crawl_result = _crawl_site(
                    ddgs,
                    domain_url,
                    max_pages=crawl_pages_per_domain,
                    max_chars=_settings.agentic.extract_chars,
                    call_deadline=crawl_deadline,
                    proxy_for_log=proxy_for_log,
                    query=query,
                )
                n_crawled = len(crawl_result.get("pages", []))
                for p in crawl_result.get("pages", []):
                    content = str(p.get("content") or "")
                    if content:
                        all_extracted.append(
                            {
                                "url": p.get("url", ""),
                                "href": p.get("url", ""),
                                "title": p.get("title", ""),
                                "content": content,
                                "host": domain,
                                "tier": next(
                                    (
                                        r.get("tier", 3)
                                        for r in all_results
                                        if r.get("host") == domain
                                    ),
                                    3,
                                ),
                            }
                        )
                crawl_meta.append(
                    {
                        "domain": domain,
                        "n_results": count,
                        "n_crawled": n_crawled,
                        "status": "ok",
                    }
                )
                log.info("pro_auto_crawl", domain=domain, n_crawled=n_crawled)
            except Exception as ex:
                crawl_meta.append(
                    {
                        "domain": domain,
                        "status": "failed",
                        "error": str(ex)[:80],
                    }
                )
                log.warning("pro_crawl_failed", domain=domain, error=str(ex)[:80])

    # Dedup + rerank accumulated results
    if all_results:
        all_results = _bm25_rerank(all_results, query)
        all_results = _diversify(all_results, _settings.agentic.max_per_domain)

    # Build key_findings + sources
    key_findings: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen_src: set[str] = set()
    for e in all_extracted:
        url = e.get("url") or e.get("href") or ""
        content = str(e.get("content") or "")
        if not content or len(content) < 200:
            continue
        passage, rel = _extract_key_passage(content, query_terms)
        if passage:
            key_findings.append(
                {
                    "url": url,
                    "title": e.get("title", ""),
                    "passage": passage,
                    "relevance": rel,
                    "host": e.get("host", ""),
                    "tier": e.get("tier", 3),
                }
            )
        if url and url not in seen_src:
            seen_src.add(url)
            sources.append(
                {
                    "url": url,
                    "title": e.get("title", ""),
                    "host": e.get("host", ""),
                    "tier": e.get("tier", 3),
                }
            )

    # Final coverage (keyword + optional semantic) + confidence
    cov_frac, cov_map = _compute_pro_coverage(query_terms, all_extracted)
    sem_cov_frac: float | None = None
    sem_detail: list[dict[str, Any]] = []
    if semantic_coverage and all_extracted:
        sem_cov_frac, sem_detail = _compute_semantic_coverage(
            query, all_extracted, embed_model=embed_model
        )
        # Blend: semantic can only boost coverage (catches paraphrases keywords miss).
        cov_frac = max(cov_frac, sem_cov_frac)
        log.info(
            "pro_semantic_coverage",
            keyword=round(_compute_pro_coverage(query_terms, all_extracted)[0], 2),
            semantic=sem_cov_frac,
            blended=round(cov_frac, 2),
        )
    unique_domains = len({r.get("host", "") for r in all_results if r.get("host")})
    confidence, conf_reason = _compute_confidence(
        all_extracted, len(all_results), unique_domains, query_terms=query_terms
    )

    # PHASE 3: synthesize
    verified_cites: list[int] = []
    follow_ups: list[str] = []
    citation_grounding: list[dict[str, Any]] = []
    if use_llm:
        synthesis, verified_cites, citation_grounding = _synthesize_llm(
            query, all_extracted, sources, model=model, think=think, stream=stream
        )
        follow_ups = _generate_follow_ups_llm(
            query, synthesis, model=model, think=think
        )
    else:
        synthesis = _synthesize_heuristic(query, key_findings, sources, cov_map)

    return {
        "schema": OUTPUT_SCHEMA_VERSION,
        "query": query,
        "mode": "llm" if use_llm else "heuristic",
        "llm_mode": llm_mode,
        "llm_available": llm_available,
        "llm_provider": provider,
        "model": (model or _settings.llm.model) if use_llm else None,
        "think": think if use_llm else None,
        "stream": stream if use_llm else None,
        "sub_queries": sub_queries,
        "rounds": rounds_meta,
        "n_rounds": len(rounds_meta),
        "n_results": len(all_results),
        "n_extracted": len(all_extracted),
        "n_sources": len(sources),
        "coverage": round(cov_frac, 2),
        "coverage_detail": cov_map,
        "semantic_coverage": sem_cov_frac,
        "semantic_detail": sem_detail,
        "confidence": confidence,
        "confidence_reason": conf_reason,
        "synthesis": synthesis,
        "verified_citations": verified_cites,
        "citation_grounding": citation_grounding or None,
        "ungrounded_citations": sum(1 for g in citation_grounding if not g["grounded"]),
        "follow_ups": follow_ups,
        "key_findings": key_findings,
        "sources": sources,
        "results": all_results,
        "meta": {
            "pages_fetched": len(all_results),
            "pages_extracted": len([e for e in all_extracted if e.get("content")]),
            "pages_crawled": sum(c.get("n_crawled", 0) for c in crawl_meta),
            "unique_domains": unique_domains,
            "call_budget_s": call_budget,
            "crawl_domains": crawl_meta,
            "embed_model": embed_model or _settings.llm.embed_model
            if semantic_coverage
            else None,
        },
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _emit(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _common_search_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("query")
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument(
        "--safesearch", default=DEFAULT_SAFESEARCH, choices=["on", "moderate", "off"]
    )
    p.add_argument(
        "--timelimit",
        default=None,
        choices=[None, "d", "w", "m", "y"],
        help="d=day w=week m=month y=year",
    )
    p.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    p.add_argument("--page", type=int, default=1)
    p.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        help="'auto' or comma-delimited (see `backends` cmd)",
    )
    p.add_argument(
        "--sort",
        default="latest",
        choices=["latest", "authority", "relevance"],
        help="latest=newest first, authority=tier first, relevance=keyword first",
    )
    p.add_argument(
        "--extract",
        type=int,
        nargs="?",
        const=3,
        default=3,
        metavar="N",
        help="fetch full page content for top-N (default 3, 0=disable for fast lookup)",
    )
    p.add_argument(
        "--extract-chars",
        type=int,
        default=DEFAULT_EXTRACT_CHARS,
        help=f"max chars per extracted page (default {DEFAULT_EXTRACT_CHARS})",
    )


def _common_ddgs_args(
    p: argparse.ArgumentParser,
    *,
    call_budget: bool = False,
    retries: bool = False,
) -> None:
    p.add_argument("--proxy", default=DEFAULT_PROXY)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--no-cache", action="store_true", default=not DEFAULT_CACHE_ENABLED)
    # F-55 surface audit: only dispatches that READ args.call_budget opt in
    # (text/news/agentic/deep-search/pro/batch); extract/crawl don't.
    if call_budget:
        p.add_argument("--call-budget", type=float, default=DEFAULT_CALL_BUDGET)
    if retries:
        p.add_argument(
            "--retries",
            type=int,
            default=DEFAULT_MAX_RETRIES,
            help="total attempts across the backend rotation (a pinned "
            "single backend receives all of them; env "
            "WEB_SEARCH_MAX_RETRIES sets the default)",
        )


def _parse_tier_budget(spec: str | None) -> dict[int, int] | None:
    """Parse 'T0:3,T1:5,T3:8' → {0:3, 1:5, 3:8}. None if spec empty/invalid.
    Invalid parts are logged (F-24) — a typo'd spec must not be silently
    treated as valid."""
    if not spec:
        return None
    out: dict[int, int] = {}
    dropped: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        if not part or ":" not in part:
            if part:
                dropped.append(part)
            continue
        tier_str, cap_str = part.split(":", 1)
        try:
            tier = int(tier_str.upper().lstrip("T"))
            cap = int(cap_str)
            if 0 <= tier <= 3 and cap >= 0:
                out[tier] = cap
            else:
                dropped.append(part)
        except ValueError:
            dropped.append(part)
    if dropped:
        log.warning(
            "tier_budget_dropped_parts",
            dropped=dropped,
            parsed=out,
            hint="expected T0:3,T1:5,T3:8 form (tiers 0-3, caps >= 0)",
        )
    return out or None


def _parse_exclude_urls(spec: str | None) -> list[str] | None:
    """Parse 'url1,url2' or '@path/to/file' → list of URLs. None if empty."""
    if not spec:
        return None
    if spec.startswith("@"):
        path = spec[1:].strip()
        try:
            text = Path(path).read_text(encoding="utf-8")
            urls = [
                u.strip()
                for u in text.splitlines()
                if u.strip() and not u.startswith("#")
            ]
            return urls or None
        except OSError as ex:
            log.warning("exclude_urls_file_unreadable", path=path, error=str(ex)[:80])
            return None
    urls = [u.strip() for u in spec.split(",") if u.strip()]
    return urls or None


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="web_search.py",
        description="SOTA web search infrastructure via ddgs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--self-check", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=False)

    p = sub.add_parser("text", help="text search across 8+ backends")
    _common_search_args(p)
    _common_ddgs_args(p, call_budget=True, retries=True)

    p = sub.add_parser("news", help="news search")
    _common_search_args(p)
    _common_ddgs_args(p, call_budget=True, retries=True)

    p = sub.add_parser(
        "host-login",
        help="one-time headed login for a login-walled host (persistent profile)",
    )
    p.add_argument("host", help="e.g. researchgate.net")
    p.add_argument("--timeout", type=int, default=300)
    p = sub.add_parser("extract", help="fetch URL + extract markdown")
    p.add_argument("url")
    p.add_argument("--max-chars", type=int, default=DEFAULT_EXTRACT_CHARS)
    _common_ddgs_args(p)

    p = sub.add_parser(
        "agentic",
        help="iterative search: per-iter backend rotation + convergence stop",
    )
    p.add_argument("query")
    p.add_argument(
        "--use-llm",
        action="store_true",
        help="explicitly REQUIRE LLM assist (fail-fast, exit 1, if no local "
        "runtime answers): iterations get LLM query rewrites targeting the "
        "gap in the found set + one-call LLM relevance rerank of the final "
        "corpus. Default AUTO — assist engages when a local runtime "
        "(Ollama, Pallama, llama.cpp, vLLM, LM Studio) answers; payload "
        "marks llm_assisted.",
    )
    p.add_argument(
        "--no-llm",
        action="store_true",
        help="disable LLM assist entirely (static breadth modifiers only, "
        "no rerank — the pre-LLM behavior, fastest).",
    )
    p.add_argument(
        "--model",
        default=None,
        help=f"LLM model for assist (default: env WEB_SEARCH_LLM_MODEL or "
        f"{_settings.llm.model}).",
    )
    p.add_argument(
        "--summarize",
        action="store_true",
        help="after the search+extract loop, produce a cited, quote-grounded "
        "LLM synthesis of the corpus (same engine as `pro`). Requires a "
        "local LLM runtime; fails fast (exit 1) if none answers or if "
        "combined with --no-llm.",
    )
    p.add_argument("--max-iter", type=int, default=DEFAULT_AGENTIC_MAX_ITER)
    p.add_argument(
        "--max-results-per-iter", type=int, default=DEFAULT_AGENTIC_MAX_RESULTS_PER_ITER
    )
    p.add_argument("--top-n-extract", type=int, default=DEFAULT_AGENTIC_TOP_N_EXTRACT)
    p.add_argument(
        "--convergence-new-min",
        type=int,
        default=DEFAULT_AGENTIC_CONVERGENCE_NEW_MIN,
        help=f"stop after N consecutive iters with n_new below this "
        f"(default {DEFAULT_AGENTIC_CONVERGENCE_NEW_MIN}, F1 convergence stop)",
    )
    p.add_argument(
        "--convergence-window",
        type=int,
        default=DEFAULT_AGENTIC_CONVERGENCE_WINDOW,
        help=f"how many consecutive low-yield iters trigger convergence stop "
        f"(default {DEFAULT_AGENTIC_CONVERGENCE_WINDOW})",
    )
    p.add_argument(
        "--tier-budget",
        default=None,
        help="per-tier extract caps, e.g. 'T0:3,T1:5,T3:8' — forces source-"
        "type diversity. Default: no caps (pick by relevance-first only).",
    )
    p.add_argument(
        "--exclude-urls",
        default=None,
        help="comma-delimited URLs to skip (feedback loop): agent identifies "
        "useless URLs in a prior payload, passes them here, fresh URLs surface "
        "in subsequent iterations. Also accepts '@path' to read URLs from a "
        "file (one per line).",
    )
    p.add_argument(
        "--adaptive",
        action="store_true",
        help="deep-search mode: top search hit → adaptive crawl with the user's "
        "query as semantic filter. Returns site-relevant content vs flat extract.",
    )
    p.add_argument("--extract-chars", type=int, default=DEFAULT_AGENTIC_EXTRACT_CHARS)
    p.add_argument(
        "--relevance-domains", type=int, default=DEFAULT_AGENTIC_RELEVANCE_DOMAINS
    )
    p.add_argument("--max-per-domain", type=int, default=DEFAULT_AGENTIC_MAX_PER_DOMAIN)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument(
        "--safesearch", default=DEFAULT_SAFESEARCH, choices=["on", "moderate", "off"]
    )
    p.add_argument("--timelimit", default=None, choices=[None, "d", "w", "m", "y"])
    p.add_argument("--backend", default=DEFAULT_BACKEND)
    p.add_argument(
        "--sort",
        default="latest",
        choices=["latest", "authority", "relevance"],
    )
    _common_ddgs_args(p, call_budget=True)

    # --- research: thin orchestrator (progressive disclosure) ---
    p = sub.add_parser(
        "deep-search",
        help="orchestrated deep research: search → extract → key_findings → "
        "confidence + optional crawl. Progressive disclosure via --depth.",
    )
    p.add_argument("query")
    p.add_argument(
        "--use-llm",
        action="store_true",
        help="explicitly REQUIRE LLM assist in the underlying agentic loop "
        "(fail-fast when no local runtime answers). Default AUTO — engages "
        "when a local runtime answers.",
    )
    p.add_argument(
        "--no-llm",
        action="store_true",
        help="disable LLM assist in the underlying loop.",
    )
    p.add_argument(
        "--model",
        default=None,
        help=f"LLM model for assist (default: env WEB_SEARCH_LLM_MODEL or "
        f"{_settings.llm.model}).",
    )
    p.add_argument(
        "--summarize",
        action="store_true",
        help="append a cited, quote-grounded LLM synthesis of the findings "
        "(same engine as `pro`; runs after the orchestrated phases). "
        "Requires a local LLM runtime; fails fast if none answers or if "
        "combined with --no-llm.",
    )
    p.add_argument(
        "--depth",
        choices=["shallow", "medium", "deep", "auto"],
        default="auto",
        help="shallow=search only; medium=+extract top-N (default 12, see "
        "--top-n); deep=+crawl top domains (default 10 pages each, see "
        "--max-pages); auto=start medium, escalate to deep if confidence<0.6 "
        "(default: auto)",
    )
    p.add_argument(
        "--tier-budget",
        default=None,
        help="per-tier extract caps, e.g. 'T0:4,T1:2,T3:2' — forces academic/"
        "news slots over SEO pages (F-53). Default: no caps.",
    )
    p.add_argument(
        "--exclude-urls",
        default=None,
        help="comma-delimited URLs to skip (feedback loop from prior research)",
    )
    p.add_argument("--extract-chars", type=int, default=DEFAULT_AGENTIC_EXTRACT_CHARS)
    p.add_argument(
        "--top-n",
        type=int,
        default=None,
        help="pages to extract in agentic phase (default 12; was hardcoded 5 — "
        "the shallow-results bug). Set 0 to disable extraction.",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="pages to crawl per top-domain in --depth deep (default 10; was "
        "hardcoded 5). Set 0 to skip crawl.",
    )
    p.add_argument(
        "--max-iter",
        type=int,
        default=None,
        help=f"agentic loop iterations (default {DEFAULT_AGENTIC_MAX_ITER}).",
    )
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument(
        "--safesearch", default=DEFAULT_SAFESEARCH, choices=["on", "moderate", "off"]
    )
    p.add_argument("--timelimit", default=None, choices=[None, "d", "w", "m", "y"])
    p.add_argument("--backend", default=DEFAULT_BACKEND)
    _common_ddgs_args(p, call_budget=True)

    # --- pro: Perplexity-style agentic research (decompose → rounds → synthesize) ---
    p = sub.add_parser(
        "pro",
        help="Perplexity-style agentic research: query decomposition → "
        "multi-round search+extract → coverage reflection → synthesis with "
        "inline citations. Heuristic path by default; --use-llm opts into "
        "local Ollama for decomposition/reflection/prose.",
    )
    p.add_argument("query")
    p.add_argument(
        "--use-llm",
        action="store_true",
        help="explicitly REQUIRE the LLM path (fail-fast, exit 1, if no local "
        "runtime answers). Default is AUTO: pro probes for any local runtime "
        "(Ollama, Pallama, llama.cpp, vLLM, LM Studio — native or "
        "OpenAI-compatible) and uses it when present, heuristic otherwise; "
        "the payload marks which path ran (mode, llm_available).",
    )
    p.add_argument(
        "--no-llm",
        action="store_true",
        help="force the heuristic path (no probe, no LLM calls — fastest).",
    )
    p.add_argument(
        "--model",
        default=None,
        help=f"Ollama model for --use-llm path (default: env WEB_SEARCH_LLM_MODEL "
        f"or {_settings.llm.model}). No hardcoded model — override freely.",
    )
    p.add_argument(
        "--max-rounds",
        type=int,
        default=5,
        help="max reflection rounds (default 5). Round 0 = decomposed facets; "
        "round 1+ = gap-filling queries until coverage threshold met.",
    )
    p.add_argument(
        "--coverage-threshold",
        type=float,
        default=0.7,
        help="stop when this fraction of query terms have evidence (default 0.7).",
    )
    p.add_argument(
        "--top-n-per-facet",
        type=int,
        default=15,
        help="pages to extract per round across all facets (default 15). "
        "Raise for deeper evidence; lower for speed.",
    )
    p.add_argument(
        "--think",
        action=argparse.BooleanOptionalAction,
        default=_settings.llm.think_default,
        help="enable LLM chain-of-thought (default: ON for deeper reasoning; "
        "use --no-think for 44x faster but shallower synthesis). "
        "Env WEB_SEARCH_LLM_THINK=0 to disable by default.",
    )
    p.add_argument(
        "--stream",
        action="store_true",
        help="stream synthesis tokens to stderr (progressive output). Agent sees "
        "the answer forming in real-time instead of waiting in silence.",
    )
    p.add_argument(
        "--semantic-coverage",
        action="store_true",
        help="enable embedding-based semantic coverage (catches paraphrased "
        "evidence keyword coverage misses). Adds ~50ms per passage via "
        "nomic-embed-text. Off by default.",
    )
    p.add_argument(
        "--embed-model",
        default=None,
        help=f"Ollama embedding model for --semantic-coverage (default: env "
        f"WEB_SEARCH_EMBED_MODEL or {_settings.llm.embed_model}).",
    )
    p.add_argument(
        "--no-auto-crawl",
        action="store_true",
        help="disable post-search domain crawling (default: ON — crawls top 3 "
        "domains for 10 pages each to add site-wide depth). Use for speed when "
        "you only want search-result evidence.",
    )
    p.add_argument(
        "--crawl-pages",
        type=int,
        default=10,
        help="pages to crawl per top-domain in auto-crawl phase (default 10).",
    )
    p.add_argument("--extract-chars", type=int, default=DEFAULT_AGENTIC_EXTRACT_CHARS)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument(
        "--safesearch", default=DEFAULT_SAFESEARCH, choices=["on", "moderate", "off"]
    )
    p.add_argument("--timelimit", default=None, choices=[None, "d", "w", "m", "y"])
    p.add_argument("--backend", default=DEFAULT_BACKEND)
    _common_ddgs_args(p, call_budget=True)

    p = sub.add_parser("batch", help="parallel multi-query search")
    p.add_argument("-q", "--query", action="append", required=True, dest="queries")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument(
        "--safesearch", default=DEFAULT_SAFESEARCH, choices=["on", "moderate", "off"]
    )
    p.add_argument("--timelimit", default=None, choices=[None, "d", "w", "m", "y"])
    p.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    p.add_argument("--backend", default=DEFAULT_BACKEND)
    p.add_argument(
        "--extract",
        type=int,
        nargs="?",
        const=3,
        default=0,
        metavar="N",
        help="fetch full page content for top-N results per query "
        "(default 0=snippets only, like text's --extract 3)",
    )
    p.add_argument(
        "--extract-chars",
        type=int,
        default=DEFAULT_EXTRACT_CHARS,
        help=f"max chars per extracted page (default {DEFAULT_EXTRACT_CHARS})",
    )
    p.add_argument(
        "--sort",
        default="latest",
        choices=["latest", "authority", "relevance"],
    )
    _common_ddgs_args(p, call_budget=True, retries=True)

    # crawl — deep BFS extraction of a site's pages
    p = sub.add_parser(
        "crawl",
        help="deep extract: BFS a site's internal pages (sub-pages + content)",
    )
    p.add_argument("url", help="starting URL to crawl")
    p.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="max pages to extract (default 10)",
    )
    p.add_argument(
        "--max-depth",
        type=int,
        default=0,
        help="max BFS depth from start URL (0=unlimited, default 0). "
        "Depth 0 = start page only is depth-0, its direct links are depth-1, "
        "etc. Use --max-depth 1 for start + direct children, 2 for one more "
        "level. Caps crawl horizon independently of --max-pages.",
    )
    p.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_EXTRACT_CHARS,
        help=f"max chars per page (default {DEFAULT_EXTRACT_CHARS})",
    )
    p.add_argument(
        "--query",
        help="semantic query — triggers Crawl4AI AdaptiveCrawler automatically "
        "(semantic relevance + confidence stop + KB cache). "
        "Use with --bfs to run BFS crawl with query-prioritized link selection "
        "instead (strategy articles visited before testimonials/blog).",
    )
    p.add_argument(
        "--bfs",
        action="store_true",
        default=False,
        help="force BFS crawl even when --query is given. Links are scored by "
        "query-keyword overlap so highest-relevance pages are crawled first. "
        "Use when you want deep multi-page extraction (not the single-page "
        "adaptive stop).",
    )
    _common_ddgs_args(p, call_budget=True)

    sub.add_parser("backends", help="list available engines per category")
    p_metrics = sub.add_parser(
        "metrics",
        help="cache + circuit + latency introspection (json or prometheus format)",
    )
    p_metrics.add_argument(
        "--format",
        choices=["json", "prometheus"],
        default=os.environ.get("WEB_SEARCH_METRICS_FORMAT", "json"),
        help="output format (default: json, or WEB_SEARCH_METRICS_FORMAT env)",
    )

    p = sub.add_parser("cleanup", help="delete old cache files")
    p.add_argument("--max-age-days", type=int, default=7)

    return ap


# --------------------------------------------------------------------------- #
# Prometheus metrics formatter (module-level helpers)
# --------------------------------------------------------------------------- #


def _prometheus_flatten(obj: Any, prefix: str, out: list[str]) -> None:
    """Walk a nested dict recursively, emit Prometheus gauge lines for numbers."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            name = f"{prefix}_{k}" if prefix else str(k)
            # If value is a dict of numbers, emit one labeled line per entry
            if isinstance(v, dict) and all(
                isinstance(vv, (int, float)) and not isinstance(vv, bool)
                for vv in v.values()
            ):
                for label_val, num in v.items():
                    metric = f"web_search_{name}"
                    safe_label = str(label_val).replace('"', '\\"')[:64]
                    out.append(f'{metric}{{item="{safe_label}"}} {num}')
            else:
                _prometheus_flatten(v, name, out)
    elif isinstance(obj, bool):
        out.append(f"web_search_{prefix} {1 if obj else 0}")
    elif isinstance(obj, (int, float)):
        out.append(f"web_search_{prefix} {obj}")
    # strings and lists skipped (would need schema-specific labeling)


def _format_prometheus(payload: dict[str, Any]) -> str:
    """Render metrics payload as Prometheus exposition text."""
    flat: list[str] = []
    _prometheus_flatten(payload, "", flat)
    lines: list[str] = []
    seen: set[str] = set()
    for line in flat:
        metric_name = line.split("{", 1)[0].split(" ", 1)[0]
        if metric_name not in seen:
            seen.add(metric_name)
            short = metric_name.replace("web_search_", "")
            lines.append(f"# HELP {metric_name} web_search.{short}")
            lines.append(f"# TYPE {metric_name} gauge")
        lines.append(line)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Self-check
# --------------------------------------------------------------------------- #


def _self_check() -> int:
    failures = []

    # ddgs importable
    try:
        DDGS, *_ = _import_ddgs()
        assert DDGS is not None
    except SystemExit as ex:
        failures.append(f"ddgs import failed (exit {ex.code})")

    # cache key deterministic + comprehensive
    k1 = _cache_key("text", {"query": "x", "region": "us-en", "max_results": 5})
    k2 = _cache_key("text", {"query": "x", "region": "us-en", "max_results": 5})
    k3 = _cache_key("text", {"query": "x", "region": "us-en", "max_results": 6})
    if k1 != k2:
        failures.append("cache key non-deterministic")
    if k1 == k3:
        failures.append("cache key ignores max_results")

    # backoff bounded
    for i in range(8):
        captured = []
        orig = time.sleep

        def _cap(s, _b=captured):
            _b.append(s)

        time.sleep = _cap
        try:
            _backoff_sleep(i)
        finally:
            time.sleep = orig
        d = captured[0] if captured else 0.0
        if d > DEFAULT_BACKOFF_CAP + 1.0 or d <= 0:
            failures.append(f"backoff attempt {i}: {d}")
            break

    # proxy redaction
    if _redact_proxy("http://user:secret@host:3128") == "http://user:secret@host:3128":
        failures.append("proxy creds not redacted")
    if _redact_proxy(None) != "<none>":
        failures.append("proxy redaction breaks on None")

    # URL canon
    c = canonicalize_url("https://www.example.com/p/?utm_source=x&k=1#f")
    if c != "https://example.com/p?k=1":
        failures.append(f"URL canon wrong: {c}")

    # host extraction (CRITICAL: lstrip bug check)
    for url, expected in [
        ("https://www.msn.com/x", "msn.com"),
        ("https://moneycontrol.com/x", "moneycontrol.com"),
        ("https://www.marketwatch.com/x", "marketwatch.com"),
        ("https://m.example.com/x", "example.com"),
        ("https://reuters.com/x", "reuters.com"),
    ]:
        h = _host_of(url)
        if h != expected:
            failures.append(f"_host_of({url!r}) = {h!r} expected {expected!r}")

    # ocid tracking param stripping
    c2 = canonicalize_url("https://msn.com/article?ocid=BingNews")
    if "ocid" in c2:
        failures.append(f"ocid not stripped: {c2}")

    # domain tier — STRUCTURAL only (.gov/.edu TLDs), no hardcoded domain bias
    for dom, exp in [
        ("sec.gov", 0),
        ("mit.edu", 0),
        ("reuters.com", 3),
        ("random.example.com", 3),
    ]:
        if domain_tier(dom) != exp:
            failures.append(f"{dom} tier={domain_tier(dom)} expected {exp}")

    # rank assignment via _apply_sort (not _normalize_results)
    norm = _normalize_results(
        [
            {"title": "A", "href": "https://sec.gov/x"},
            {"title": "B", "href": "https://random.com/b"},
        ],
        {"sec"},
        3,
    )
    sorted_norm = _apply_sort(norm, "latest")
    if not sorted_norm or sorted_norm[0].get("rank") != 1:
        failures.append(f"rank not assigned via _apply_sort: {sorted_norm}")

    # adaptive bucket AIMD
    atb = AdaptiveTokenBucket(2.0, 5, 10.0, 0.2, 0.2, 0.5)
    r0 = atb.rate
    atb.on_success()
    if atb.rate <= r0:
        failures.append("AIMD no increase")
    atb.on_rate_limit()
    if atb.rate >= r0 + 0.2:
        failures.append("AIMD no decrease")

    # single-flight
    sf = SingleFlight()
    l1 = sf.acquire("k")
    l2 = sf.acquire("k")
    if l1 is not l2:
        failures.append("single-flight different locks")
    l1.acquire()
    if l2.acquire(blocking=False):
        failures.append("single-flight not exclusive")
    l1.release()

    # latency tracker
    lt = LatencyTracker(10)
    for v in [0.1, 0.2, 0.3, 0.4, 0.5]:
        lt.record("t", v)
    p50 = lt.percentile("t", 50)
    if p50 is None or not (0.2 <= p50 <= 0.4):
        failures.append(f"latency p50 wrong: {p50}")

    # circuit breaker
    cbr = CircuitBreaker(1, 60.0)
    cbr.record_failure("t:b")
    if not cbr.is_open("t:b"):
        failures.append("circuit did not open")

    # relevance + normalize
    norm = _normalize_results(
        [
            {"title": "A", "href": "https://sec.gov/x?a=1&utm_source=z"},
            {"title": "A2", "href": "https://sec.gov/x?a=1"},
            {"title": "B", "href": "https://random.com/b"},
        ],
        {"sec"},
        3,
    )
    if len(norm) != 2:
        failures.append(f"normalize dedup wrong: {len(norm)}")
    if norm[0]["tier"] != 0:
        failures.append(f"normalize ranking wrong: tier={norm[0]['tier']}")

    # key passage
    kp = find_key_passage(
        "Some general introductory text about various topics here.\n\n"
        "Python async patterns guide for concurrent programming.\n\n"
        "Conclusion and final remarks end the document here.",
        {"python", "async"},
    )
    if "python" not in kp.lower():
        failures.append(f"key passage wrong: {kp}")

    # backend picker skips open circuit
    cb2 = CircuitBreaker(1, 60.0)
    cb2.record_failure("text:google")
    picked2, _ = _pick_backend(["google", "brave"], cb2, "text", LatencyTracker())
    if picked2 == "google":
        failures.append("backend picker did not skip circuit-open")

    # date extraction
    for text, expected_prefix in [
        ("Published: March 15, 2025 by Reuters", "2025-03-15"),
        ("Updated 2025-01-30T10:00:00Z", "2025-01-30"),
        ("Posted 3 days ago by admin", ""),  # relative — just check non-empty
        ("No date here at all", ""),
    ]:
        d = _extract_date(text)
        if expected_prefix and not d.startswith(expected_prefix):
            failures.append(
                f"date extraction: '{text}' → '{d}' expected '{expected_prefix}'"
            )
        if not expected_prefix and text == "No date here at all" and d:
            failures.append(f"date false positive: '{text}' → '{d}'")

    # relative date
    d_rel = _extract_date("2 days ago")
    if not d_rel:
        failures.append("relative date extraction failed")

    # language detection
    if _detect_language("This is a normal English sentence about stocks.") != "en":
        failures.append(f"language en wrong: {_detect_language('This is English.')}")
    if _detect_language("这是关于股票的中文文章") != "zh":
        failures.append("language zh wrong")
    if _detect_language("") != "unknown":
        failures.append("language empty wrong")

    # age computation
    age = _compute_age_days("2025-01-01")
    if age is None or age < 0:
        failures.append(f"age computation wrong: {age}")

    # sort: latest puts dated before undated
    test_results = [
        {"host": "a.com", "tier": 3, "relevance_score": 0, "date": ""},
        {"host": "b.com", "tier": 0, "relevance_score": 5, "date": "2025-08-01"},
        {"host": "c.com", "tier": 3, "relevance_score": 1, "date": "2025-08-09"},
    ]
    sorted_latest = _apply_sort(list(test_results), "latest")
    if sorted_latest[0]["host"] != "c.com":  # newest dated first
        failures.append(f"sort latest wrong: {[r['host'] for r in sorted_latest]}")
    sorted_auth = _apply_sort(list(test_results), "authority")
    if sorted_auth[0]["host"] != "b.com":  # T0 first
        failures.append(f"sort authority wrong: {[r['host'] for r in sorted_auth]}")

    # SOTA markdown cascade — verify both extractors fire + non-empty result
    fixture_html = (
        "<html><body><article><h1>Test</h1>"
        "<p>Body text with <a href='https://x.com'>link</a> and "
        "<strong>formatting</strong>.</p></article></body></html>"
    )
    md_trafil = _extract_trafilatura(fixture_html, "https://x.com/test")
    md_c4ai = _extract_crawl4ai_md(fixture_html, "https://x.com/test")
    md_cascade = _html_to_markdown(fixture_html, "https://x.com/test")
    if not md_trafil:
        failures.append("trafilatura (hard dep) returned empty on simple fixture")
    if not md_c4ai:
        failures.append("crawl4ai (hard dep) returned empty on simple fixture")
    if not md_cascade or len(md_cascade) < 10:
        failures.append(f"cascade returned empty/short: {md_cascade!r}")
    if "Test" not in md_cascade:
        failures.append("cascade lost title text")

    # No-truncation: 1MB+ HTML survives end-to-end (key_passage depends on it)
    big_article = (
        "<html><body><article>"
        + ("<p>filler paragraph xyz. </p>" * 30000)
        + "</article></body></html>"
    )
    big_md = _html_to_markdown(big_article, "https://x.com/big")
    if len(big_md) < 100_000:
        failures.append(
            f"no-truncation violated: input={len(big_article)} output={len(big_md)}"
        )

    # Binary URL inference + Content-Type classifier (PDF/DOCX routing contract)
    if _binary_url_inference("https://rbi.org.in/circular.pdf") != "application/pdf":
        failures.append(
            f"_binary_url_inference missed .pdf: "
            f"{_binary_url_inference('https://rbi.org.in/circular.pdf')!r}"
        )
    if _binary_url_inference("https://example.com/report.DOCX") is None:
        failures.append("_binary_url_inference case-insensitive ext missed")
    if _binary_url_inference("https://example.com/page.html") is not None:
        failures.append("_binary_url_inference false-positive on .html")
    if _binary_url_inference("https://example.com/article") is not None:
        failures.append("_binary_url_inference false-positive on no-ext")
    if not _is_binary_content_type("application/pdf"):
        failures.append("_is_binary_content_type missed application/pdf")
    if not _is_binary_content_type(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document; charset=binary"
    ):
        failures.append("_is_binary_content_type missed docx with params")
    if _is_binary_content_type("text/html; charset=utf-8"):
        failures.append("_is_binary_content_type false-positive on text/html")
    if _is_binary_content_type("application/json"):
        failures.append("_is_binary_content_type false-positive on application/json")
    if _is_binary_content_type(""):
        failures.append("_is_binary_content_type false-positive on empty")
    try:
        raise BinaryDocument("https://x/y.pdf", "application/pdf")
    except BinaryDocument as ex:
        if ex.url != "https://x/y.pdf" or ex.content_type != "application/pdf":
            failures.append(
                f"BinaryDocument attrs wrong: url={ex.url!r} ct={ex.content_type!r}"
            )

    # Agentic depth fixes (F1-F6): rotation, convergence, ranking
    if not _AGENTIC_BACKEND_ROTATION:
        failures.append("_AGENTIC_BACKEND_ROTATION empty")
    # F-59: language gate — ru spam must vanish for en queries (floor met)
    _en_probe = [
        {"title": f"English cargo answer {i}", "href": f"https://en{i}.test/x"}
        for i in range(6)
    ]
    _ru_probe = [
        {
            "title": "Установка Rust на Linux инструкция",
            "href": "https://selectel.ru/x",
        },
    ]
    _gated = _normalize_results(
        _en_probe + _ru_probe,
        {"cargo"},
        3,
        query="cargo garbage collection",
    )
    if any(r.get("host") == "selectel.ru" for r in _gated):
        failures.append("F-59 lang gate did not drop ru result for en query")
    # F-59 fail-open: floor unmet -> nothing dropped
    _failopen = _normalize_results(
        [_en_probe[0], _ru_probe[0]],
        {"cargo"},
        3,
        query="cargo garbage collection",
    )
    if len(_failopen) != 2:
        failures.append(f"F-59 fail-open broke: kept {len(_failopen)} of 2")
    # F-60: coverage-weighted relevance — partial < full
    _terms = {"rust", "cargo", "garbage", "collection", "target"}
    _partial = {
        "title": "Incremental Garbage Collection in Unity",
        "href": "https://unity.test/gc",
        "body": "garbage collection",
    }
    _full = {
        "title": "rust cargo garbage collection target directory issue",
        "href": "https://github.test/x",
        "body": "",
    }
    if not (_score_relevance(_partial, _terms) < _score_relevance(_full, _terms)):
        failures.append(
            "F-60 coverage weighting not applied: partial >= full "
            f"({_score_relevance(_partial, _terms)} >= "
            f"{_score_relevance(_full, _terms)})"
        )
    # WS8: profile presets — all three resolve, invalid names fail fast
    for _pname in ("polite", "standard", "aggressive"):
        if _pname not in _PROFILE_PRESETS:
            failures.append(f"WS8 profile missing from _PROFILE_PRESETS: {_pname}")
    _std = _PROFILE_PRESETS.get("standard", {})
    if _std.get("rate_cap") != 10.0 or _std.get("max_retries") != 4:
        failures.append(f"WS8 standard preset drifted from historic literals: {_std}")
    # WS9: UA consistency — single major version shared by both layers
    if not (120 < _CHROME_UA_MAJOR < 200):
        failures.append(
            f"WS9 _CHROME_UA_MAJOR out of plausible range: {_CHROME_UA_MAJOR}"
        )
    if _JS_RENDER_MIN_HTML <= _JS_RENDER_MIN_TEXT:
        failures.append("WS9 JS-render thresholds inverted (html floor <= text floor)")
    _curl_src = inspect.getsource(_fetch_curl_cffi)
    _pw_src = inspect.getsource(_fetch_playwright)
    if 'v="{_CHROME_UA_MAJOR}"' not in _curl_src:
        failures.append("WS9 curl_cffi Sec-Ch-Ua not derived from _CHROME_UA_MAJOR")
    if "Chrome/{_CHROME_UA_MAJOR}.0.0.0" not in _pw_src:
        failures.append("WS9 playwright UA not derived from _CHROME_UA_MAJOR")
    # Wave 6: UA auto-derive + closest-match rotation + stealth extension
    if _parse_chrome_major("Google Chrome for Testing 151.0.7922.34") != 151:
        failures.append("Wave6 _parse_chrome_major broke")
    if _parse_chrome_major("garbage") is not None:
        failures.append("Wave6 _parse_chrome_major accepts garbage")
    _rot = _impersonation_rotation()
    if "firefox133" not in _rot or "safari17_2_ios" not in _rot:
        failures.append(f"Wave6 rotation lost browser-family diversity: {_rot}")
    _rot_majors = [
        int(m.group(1))
        for n in _rot
        if n.startswith("chrome") and (m := re.search(r"(\d+)", n))
    ]
    if _rot_majors and any(m > _CHROME_UA_MAJOR for m in _rot_majors):
        failures.append("Wave6 rotation advertises chrome NEWER than the UA major")
    if "_impersonation_rotation()" not in _curl_src:
        failures.append("Wave6 curl_cffi not using dynamic rotation")
    if "0x9245" not in _STEALTH_JS or "Math.random" not in _STEALTH_JS:
        failures.append("Wave6 stealth lost WebGL spoof / canvas noise")
    # F2 (Wave 5): robots parsing semantics the crawl gate relies on
    _rp = RobotFileParser()
    _rp.parse(
        [
            "User-agent: *",
            "Disallow: /private",
            "Crawl-delay: 2",
            "Sitemap: https://x.test/sitemap.xml",
            "",
        ]
    )
    if not _rp.can_fetch("*", "https://x.test/public") or _rp.can_fetch(
        "*", "https://x.test/private"
    ):
        failures.append("F2 robots parser: can_fetch semantics broken")
    if _rp.crawl_delay("*") != 2 or "https://x.test/sitemap.xml" not in _rp.site_maps():
        failures.append("F2 robots parser: crawl_delay/site_maps unread")
    # F1 (Wave 5): sitemap loc extraction — urlset + malformed XML
    _locs, _children = _parse_locs(
        "<urlset><url><loc>https://x.test/a</loc></url>"
        "<url><loc>https://x.test/b</loc></url></urlset>"
    )
    if _locs != ["https://x.test/a", "https://x.test/b"] or _children:
        failures.append(f"F1 sitemap parse: urlset locs wrong: {_locs}")
    if _parse_locs("<not-xml") != ([], []):
        failures.append("F1 sitemap parse: malformed XML must yield ([], [])")
    # F3 (Wave 5): anchor-text scoring — stems match both directions
    _anchor_score = _score_anchor_text(
        "backtesting RSI mean reversion", {"rsi", "mean", "reversion", "strategy"}
    )
    if _anchor_score != 3 or _score_anchor_text("", {"rsi"}) != 0:
        failures.append(f"F3 anchor scoring broken: {_anchor_score}")
    # F4 (Wave 5): challenge re-solve attempts default + loop wiring
    os.environ.pop("WEB_SEARCH_CHALLENGE_ATTEMPTS", None)
    if _challenge_max_attempts() != 2:
        failures.append("F4 challenge attempts default drifted from 2")
    if "playwright_resolve_attempts" not in inspect.getsource(_fetch_playwright):
        failures.append("F4 challenge re-solve loop not wired in _fetch_playwright")
    # WS4: recency-intent detection — strong words → 'd', evergreen → None
    if _detect_recency_intent("latest rust release notes") != "d":
        failures.append(
            f"recency intent strong word missed: "
            f"{_detect_recency_intent('latest rust release notes')!r}"
        )
    if _detect_recency_intent("how does rust ownership work") is not None:
        failures.append(
            "recency intent false positive on evergreen query: "
            f"{_detect_recency_intent('how does rust ownership work')!r}"
        )
    # WS5: quote grounding — exact substring grounds, absent quote does not
    _content = "the cargo target directory grows because every rustc change layers new artifacts in deps forever"
    if (
        _quote_in_content(
            _normalize_quote_text("every rustc change layers new artifacts"),
            _normalize_quote_text(_content),
            0.65,
        )
        != 1.0
    ):
        failures.append("quote grounding: exact substring not scored 1.0")
    _absent_ratio = _quote_in_content(
        _normalize_quote_text("python virtualenv wrapper configuration"),
        _normalize_quote_text(_content),
        0.65,
    )
    if _absent_ratio >= 0.65:
        failures.append(f"quote grounding: absent quote scored {_absent_ratio}")
    _g_records, _g_stripped = _verify_quote_grounding(
        'answer text\n\nSOURCES:\n1. "every rustc change layers new artifacts"\n'
        '2. "completely fabricated python quote"',
        [{"url": "https://x.test/a"}],
        [{"url": "https://x.test/a", "content": _content}],
    )
    _g_flags = {r["index"]: r["grounded"] for r in _g_records}
    if _g_flags != {1: True, 2: False} or "SOURCES:" in _g_stripped:
        failures.append(f"quote grounding verifier wrong: {_g_flags} / stripped?!")
    available = set(_get_text_backends())
    bad = [b for b in _AGENTIC_BACKEND_ROTATION if b not in available]
    if bad:
        failures.append(
            f"_AGENTIC_BACKEND_ROTATION has unavailable backends: {bad} "
            f"(available: {sorted(available)})"
        )
    # Convergence math: n_new < min for `window` consecutive iters → stop
    cnm, cw = DEFAULT_AGENTIC_CONVERGENCE_NEW_MIN, DEFAULT_AGENTIC_CONVERGENCE_WINDOW
    if cnm < 1 or cw < 1:
        failures.append(f"convergence knobs insane: min={cnm} window={cw}")
    # F2: extract-top-urls sort = relevance-first, tier-tiebreak
    mock_results = [
        {"url": "a", "tier": 0, "relevance_score": 1, "host": "x"},  # T0 spam
        {"url": "b", "tier": 3, "relevance_score": 4, "host": "y"},  # T3 perfect
        {"url": "c", "tier": 1, "relevance_score": 2, "host": "z"},  # T1 medium
    ]
    ranked = sorted(
        mock_results,
        key=lambda r: (-r.get("relevance_score", 0), r.get("tier", 3)),
    )
    if ranked[0]["url"] != "b":
        failures.append(
            f"F2 extract sort wrong: high-relevance T3 must rank first, got {ranked[0]}"
        )
    # Rotation index math: iter i prefers rotation[i % n]; page = i//n + 1
    n_rot = len(_AGENTIC_BACKEND_ROTATION)
    for i, exp_preferred, exp_page in [
        (0, _AGENTIC_BACKEND_ROTATION[0], 1),
        (n_rot - 1, _AGENTIC_BACKEND_ROTATION[-1], 1),
        (n_rot, _AGENTIC_BACKEND_ROTATION[0], 2),
    ]:
        got_preferred = _AGENTIC_BACKEND_ROTATION[i % n_rot]
        got_page = (i // n_rot) + 1
        # Verify comma-delimited pool passed to _search_with_defense starts
        # with the preferred backend (so _pick_backend prefers it first).
        pool_str = ",".join(
            _AGENTIC_BACKEND_ROTATION[i % n_rot :]
            + _AGENTIC_BACKEND_ROTATION[: i % n_rot]
        )
        pool_first = pool_str.split(",")[0]
        if (got_preferred, got_page, pool_first) != (
            exp_preferred,
            exp_page,
            exp_preferred,
        ):
            failures.append(
                f"rotation math iter={i}: got (preferred={got_preferred},"
                f"page={got_page},pool_first={pool_first}) "
                f"expected (preferred={exp_preferred},page={exp_page},"
                f"pool_first={exp_preferred})"
            )

    # Hard-dep verification (already done at startup, but sanity-check here)
    for mod, _pip in REQUIRED_DEPS:
        try:
            __import__(mod)
        except ImportError:
            failures.append(f"hard dep missing: {mod}")

    # BM25 reranker (#1) — verifies rank_bm25 produces sane scores
    bm25_test_results = [
        {"title": "ETF tax guide India", "body": "LTCG STCG rates", "href": "a"},
        {"title": "Bollywood box office", "body": "movie collection 2025", "href": "b"},
        {"title": "ETF taxation rules", "body": "long term capital gains", "href": "c"},
    ]
    bm25_out = _bm25_rerank(bm25_test_results, "ETF tax India")
    if not any("bm25_score" in r for r in bm25_out):
        failures.append("BM25 reranker did not set bm25_score field")
    else:
        # The off-topic Bollywood page must have lower BM25 than ETF pages
        scores = {r["href"]: r.get("bm25_score", 0) for r in bm25_out}
        if scores.get("b", 0) > scores.get("a", 0):
            failures.append(
                f"BM25 ranking insane: bollywood={scores['b']} > etf={scores['a']}"
            )

    # SimHash dedup (#2) — syndicated near-dupes must collapse
    dup_text_a = "ETF tax treatment in India guide for investors with LTCG STCG rates"
    dup_text_b = "ETF tax treatment in India: guide for investors with LTCG STCG rates"  # near-identical
    unique_text = "Completely unrelated content about Bollywood movie box office"
    h_a = _simhash(dup_text_a)
    h_b = _simhash(dup_text_b)
    h_u = _simhash(unique_text)
    if _hamming_distance(h_a, h_b) > 5:
        failures.append(
            f"SimHash insane: near-dupe hamming={_hamming_distance(h_a, h_b)} (should be ≤5)"
        )
    if _hamming_distance(h_a, h_u) < 10:
        failures.append(
            f"SimHash insane: unrelated hamming={_hamming_distance(h_a, h_u)} (should be ≥10)"
        )
    # _dedup_by_content keeps the better-tier one on collision
    dedup_in = [
        {
            "title": "ETF tax",
            "body": dup_text_a,
            "tier": 3,
            "href": "a",
            "canonical_url": "a",
        },
        {
            "title": "ETF taxation",  # different title, same body (syndicated)
            "body": dup_text_b,
            "tier": 1,
            "href": "b",
            "canonical_url": "b",
        },  # higher tier
        {
            "title": "Bollywood",
            "body": unique_text,
            "tier": 3,
            "href": "u",
            "canonical_url": "u",
        },
    ]
    kept, dropped = _dedup_by_content(dedup_in, threshold=5)
    if len(kept) != 2:
        failures.append(f"SimHash dedup wrong: kept={len(kept)} expected 2")
    elif not any(r["href"] == "b" for r in kept):
        failures.append("SimHash dedup dropped higher-tier result on collision")

    # htmldate integration (#6) — verify it activates on HTML snippets
    html_with_meta = (
        "<html><head><meta property='article:published_time' "
        "content='2025-03-15T10:00:00Z'></head><body>x</body></html>"
    )
    d_html = _extract_date(html_with_meta)
    if not d_html.startswith("2025-03-15"):
        failures.append(
            f"htmldate integration wrong: got {d_html!r} expected 2025-03-15"
        )

    # Tier-budget parser (#5)
    tb = _parse_tier_budget("T0:3,T1:5,T3:8")
    if tb != {0: 3, 1: 5, 3: 8}:
        failures.append(f"_parse_tier_budget wrong: {tb}")
    if _parse_tier_budget("") is not None:
        failures.append("_parse_tier_budget empty spec should return None")
    if _parse_tier_budget("garbage") is not None:
        failures.append("_parse_tier_budget garbage should return None")

    # Exclude-URLs parser (#7)
    eu = _parse_exclude_urls("https://a.com/x,https://b.com/y")
    if eu != ["https://a.com/x", "https://b.com/y"]:
        failures.append(f"_parse_exclude_urls wrong: {eu}")
    if _parse_exclude_urls(None) is not None:
        failures.append("_parse_exclude_urls None should return None")

    # Language detect extended coverage (#4)
    lang_cases = [
        ("2025 – ויקיפדיה", "he"),
        ("Ελληνική γλώσσα", "el"),
        ("한국어 텍스트", "ko"),
        ("これは日本語のテキストです", "ja"),
    ]
    for text, expected in lang_cases:
        got = _detect_language(text)
        if got != expected:
            failures.append(
                f"_detect_language({text!r}) = {got!r} expected {expected!r}"
            )

    # F-13: Playwright browser assets present? Missing browsers kill Layer-3
    # + adaptive crawl at runtime with confusing errors. WARNING, not failure —
    # every non-JS flow works without them. Driver-free check: spawning the
    # playwright driver inside self-check proved flaky (teardown races).
    # Base 35 = 30 legacy + 5 (WS4 recency x2, WS5 quote-grounding x3).
    # Structural guard (2026-09-15 live incident): a rename edit left BOTH the
    # old Ollama-only worker and the new provider dispatcher defined under the
    # same name `_llm_call` — Python silently used the LAST def, so the
    # OpenAI-compatible path was dead code while every test still passed
    # (tests exercised probe/workers directly, never the dispatch entry).
    # AST scan: every module-level def/assignment name must be UNIQUE.
    # Catches the whole shadowing class at self-check time, forever.
    _own_src = Path(__file__).read_text(encoding="utf-8")
    import ast as _ast

    _tree = _ast.parse(_own_src)
    _top_names: Counter = Counter()
    for _node in _tree.body:
        if isinstance(_node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            _top_names[_node.name] += 1
        elif isinstance(_node, _ast.Assign):
            for _t in _node.targets:
                if isinstance(_t, _ast.Name):
                    _top_names[_t.id] += 1
        elif isinstance(_node, _ast.AnnAssign) and isinstance(_node.target, _ast.Name):
            _top_names[_node.target.id] += 1
    _dupes = sorted(n for n, c in _top_names.items() if c > 1)
    if _dupes:
        failures.append(
            f"top-level names defined more than once (last def silently "
            f"shadows the rest): {_dupes}"
        )
    # Dispatch routing proof: _llm_call must reach BOTH providers' workers.
    for _prov, _worker in (("openai", "_openai_call"), ("ollama", "_ollama_call")):
        _probe_calls: list = []
        _worker_calls: list = []

        def _fake_probe(host=None, timeout=2.0, _p=_prov):
            _probe_calls.append(1)
            return _p

        def _fake_worker(*a, **k):
            _worker_calls.append(1)
            return "routed"

        _saved_probe, _saved_worker = _llm_probe, globals()[_worker]
        globals()["_llm_probe"], globals()[_worker] = _fake_probe, _fake_worker
        try:
            _routed = _llm_call("x")
        finally:
            globals()["_llm_probe"], globals()[_worker] = _saved_probe, _saved_worker
        if _routed != "routed" or not _worker_calls:
            failures.append(
                f"_llm_call dispatch never reached {_worker} for provider "
                f"'{_prov}' (routed={_routed!r})"
            )
    n_checks = 57
    _pw_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
        Path.home() / ".cache" / "ms-playwright"
    )
    _pw_root_mac = Path.home() / "Library" / "Caches" / "ms-playwright"
    _roots = [Path(_pw_root), _pw_root_mac]
    _has_chromium = any(
        root.exists() and list(root.glob("chromium*")) for root in _roots
    )
    if not _has_chromium:
        sys.stderr.write(
            "WARN: Playwright Chromium not installed — bot-block bypass "
            "Layer 3 and adaptive crawl will fail. Fix:\n"
            "  <venv>/bin/python -m playwright install chromium\n"
        )
    else:
        n_checks += 1

    if failures:
        sys.stderr.write("SELF-CHECK FAILED:\n")
        for f in failures:
            sys.stderr.write(f"  - {f}\n")
        return 1
    sys.stderr.write(f"SELF-CHECK OK (0 failures, {n_checks} checks passed)\n")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main_with_args(args) -> int:
    """Dispatch with a pre-built argparse Namespace (test seam for the F-05
    exit-code contract — no subprocess needed). Applies the same --no-cache
    handling as main()."""
    if getattr(args, "no_cache", False):
        global DEFAULT_CACHE_ENABLED
        DEFAULT_CACHE_ENABLED = False
    return _dispatch(args)


def _llm_flags_from_args(args) -> tuple[str, str | None]:
    """Tri-state resolution shared by pro/agentic/deep-search dispatch arms.
    Returns (llm_mode, error) — error set when both flags are given."""
    on = getattr(args, "use_llm", False)
    off = getattr(args, "no_llm", False)
    if on and off:
        return "auto", "--use-llm and --no-llm are mutually exclusive"
    if on:
        return "on", None
    if off:
        return "off", None
    return "auto", None


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)

    # F-41: --no-cache was parsed but never consumed — the cache layer reads
    # the DEFAULT_CACHE_ENABLED module constant, so the flag was dead outside
    # the adaptive-crawl path. Flip the runtime switch once, here. (Same
    # mechanism tests use via monkeypatch; Settings stays the default source.)
    if getattr(args, "no_cache", False):
        global DEFAULT_CACHE_ENABLED
        DEFAULT_CACHE_ENABLED = False
    return _dispatch(args)


def _dispatch(args) -> int:
    # Hard-dep gate — fail fast BEFORE any work, including self-check.
    # Exception: --help / no-cmd pass-through (argparse handles --help itself).
    if args.cmd or args.self_check:
        _check_required_deps()
    if args.self_check:
        return _self_check()
    if not args.cmd:
        _build_parser().print_help(sys.stderr)
        return 1

    # Import ddgs + cache its exception types module-level (used by _do_search
    # via _RatelimitException / _TimeoutException / _DDGSException globals).
    DDGS, *_ = _import_ddgs()

    # F-63: reload learned rate + open circuits from the last clean exit, and
    # persist again on the way out (atexit runs on sys.exit incl. 130 path).
    if DEFAULT_PERSIST_STATE:
        _restore_runtime_state()
        atexit.register(_save_runtime_state)

    if args.cmd == "backends":
        try:
            from ddgs.engines import ENGINES

            payload = {
                "schema": OUTPUT_SCHEMA_VERSION,
                "categories": {c: sorted(k) for c, k in ENGINES.items()},
            }
        except ImportError as ex:
            payload = {"error": str(ex), "categories": {}}
        _emit(payload)
        return 0

    if args.cmd == "metrics":
        payload = {
            "schema": OUTPUT_SCHEMA_VERSION,
            "metrics": _get_metrics().snapshot(),
            "circuit_breaker": _get_cb().snapshot(),
            "rate_limiter": _get_rate_limiter().snapshot(),
            "latency": _get_latency().snapshot(),
            "cache": _cache_stats(),
        }
        if getattr(args, "format", "json") == "prometheus":
            sys.stdout.write(_format_prometheus(payload))
        else:
            _emit(payload)
        return 0

    if args.cmd == "cleanup":
        removed = _cache_cleanup(args.max_age_days * 86400)
        _emit(
            {
                "schema": OUTPUT_SCHEMA_VERSION,
                "cmd": "cleanup",
                "removed": removed,
                "max_age_days": args.max_age_days,
            }
        )
        return 0

    if args.cmd == "host-login":
        return host_login(args.host, timeout_s=args.timeout)

    if args.cmd == "extract":
        ddgs = _build_ddgs(getattr(args, "dht", DEFAULT_DHT), args.proxy, args.timeout)
        _get_rate_limiter().acquire(timeout=10.0)
        try:
            content = _extract_with_fallback(
                ddgs, args.url, proxy_for_log=args.proxy or "<none>"
            )
        except FetchBlocked as ex:
            # Structured failure (2026-09-03): exception leaked as a raw
            # traceback through run(), breaking the (rc, payload) contract.
            _emit(
                {
                    "schema": OUTPUT_SCHEMA_VERSION,
                    "url": args.url,
                    "error": f"fetch_blocked: {ex}",
                    "hint": "all bypass tiers exhausted (bot protection). "
                    "Unlocks: (1) session cookie wallet at "
                    "~/.config/geokit/websearch/cookies.d/<host>.txt, "
                    "(2) one-time manual login: "
                    "host-login <host>, "
                    "(3) --proxy (residential), or fetch the DOI from an "
                    "academic API instead",
                }
            )
            return 4
        except BinaryDocument as ex:
            # Signal AI agent to download the URL locally and invoke pdf-ocr
            # skill on the file. Skill stays pure-extract; no PDF parsing here.
            _emit(
                {
                    "schema": OUTPUT_SCHEMA_VERSION,
                    "url": args.url,
                    "host": _host_of(args.url),
                    "tier": domain_tier(_host_of(args.url)),
                    "binary": True,
                    "content_type": ex.content_type,
                    "error": "binary_content",
                    "hint": (
                        "Non-HTML content. Download via curl/wget to a local "
                        "file, then invoke the pdf-ocr skill on the local path "
                        "for SOTA text extraction (born-digital + OCR routing)."
                    ),
                    "content": None,
                }
            )
            return 3
        # Other SearchError subclasses (SSRFBlocked, BudgetExceeded, InvalidURL,
        # FetchBlocked, etc.) propagate to __main__'s _emit_search_error catch,
        # which emits the full structured format (error type, reason, hint).
        # NO truncation — full page content
        _emit(
            {
                "schema": OUTPUT_SCHEMA_VERSION,
                "url": args.url,
                "host": _host_of(args.url),
                "tier": domain_tier(_host_of(args.url)),
                "fmt": "text_markdown",
                "content": content,
                "chars": len(content),
            }
        )
        return 0

    if args.cmd == "crawl":
        # --query alone → adaptive crawl (Crawl4AI semantic relevance + stop).
        # --query --bfs → BFS with query-prioritized link selection (strategy
        #   articles visited before testimonials/blog/cart).
        # no --query → plain BFS (depth-order).
        use_adaptive = bool(getattr(args, "query", None)) and not getattr(
            args, "bfs", False
        )
        if use_adaptive:
            payload = _crawl_adaptive_cached(
                args.url,
                args.query,
                max_pages=args.max_pages,
                call_deadline=time.monotonic() + args.call_budget,
                no_cache=args.no_cache,
            )
            _emit(payload)
            # F-05: an error payload is an error — exit 1, not 0.
            return 1 if "error" in payload else 0
        ddgs = _build_ddgs(getattr(args, "dht", DEFAULT_DHT), args.proxy, args.timeout)
        deadline = time.monotonic() + args.call_budget
        payload = _crawl_site(
            ddgs,
            args.url,
            max_pages=args.max_pages,
            max_chars=args.max_chars,
            call_deadline=deadline,
            proxy_for_log=args.proxy or DEFAULT_PROXY or "<none>",
            max_depth=getattr(args, "max_depth", 0),
            query=getattr(args, "query", None),
        )
        _emit(payload)
        # F-05: BFS crawl error payloads (SSRF start, invalid URL) exit 1.
        return 1 if "error" in payload else 0

    proxy_for_log = getattr(args, "proxy", DEFAULT_PROXY)

    if args.cmd in {"text", "news"}:
        ddgs = _build_ddgs(getattr(args, "dht", DEFAULT_DHT), args.proxy, args.timeout)
        deadline = time.monotonic() + getattr(args, "call_budget", DEFAULT_CALL_BUDGET)
        try:
            payload = _search_with_defense(
                ddgs,
                args.cmd,
                args.query,
                region=args.region,
                safesearch=args.safesearch,
                timelimit=args.timelimit,
                max_results=args.max_results,
                page=args.page,
                backend=args.backend,
                cache_ttl=getattr(args, "cache_ttl", None) or DEFAULT_CACHE_TTL,
                neg_cache_ttl=getattr(args, "neg_cache_ttl", None)
                or DEFAULT_NEG_CACHE_TTL,
                call_deadline=deadline,
                proxy_for_log=proxy_for_log,
                max_retries=getattr(args, "retries", None),
            )
        except SearchError as ex:
            _emit(
                {
                    "schema": OUTPUT_SCHEMA_VERSION,
                    "query": args.query,
                    "category": args.cmd,
                    "error": str(ex),
                    "results": [],
                    "metrics": _get_metrics().snapshot(),
                }
            )
            # F-05: unrecoverable search failure = exit 1 (was 3).
            return 1
        _apply_sort(payload["results"], args.sort)
        # Auto-extract full content for top-N results if --extract given
        extract_n = getattr(args, "extract", 0)
        if extract_n > 0 and payload["results"]:
            query_terms = {t for t in re.split(r"\s+", args.query) if len(t) > 2}
            payload["extracted"] = _extract_top_urls(
                ddgs,
                payload["results"],
                extract_n,
                getattr(args, "extract_chars", DEFAULT_AGENTIC_EXTRACT_CHARS),
                query_terms,
                query=args.query,
            )
        _emit(payload)
        # F-05/F-48: genuine empty = no results, or extraction ran but
        # nothing usable came out of any candidate.
        return _search_exit_code(payload, extract_ran=extract_n > 0)

    if args.cmd == "agentic":
        llm_mode, flag_err = _llm_flags_from_args(args)
        if flag_err:
            _emit({"error": "conflicting_flags", "detail": flag_err})
            return 2
        ddgs = _build_ddgs(getattr(args, "dht", DEFAULT_DHT), args.proxy, args.timeout)
        top_n = args.top_n_extract
        payload = _agentic_loop(
            ddgs,
            args.query,
            llm_mode=llm_mode,
            llm_model=getattr(args, "model", None),
            summarize=getattr(args, "summarize", False),
            max_iter=args.max_iter,
            max_results_per_iter=args.max_results_per_iter,
            top_n_extract=top_n,
            relevance_domains=args.relevance_domains,
            max_per_domain=args.max_per_domain,
            region=args.region,
            safesearch=args.safesearch,
            timelimit=args.timelimit,
            backend=args.backend,
            cache_ttl=getattr(args, "cache_ttl", None) or DEFAULT_CACHE_TTL,
            neg_cache_ttl=getattr(args, "neg_cache_ttl", None) or DEFAULT_NEG_CACHE_TTL,
            extract_chars=args.extract_chars,
            call_budget=args.call_budget,
            proxy_for_log=proxy_for_log,
            convergence_new_min=getattr(
                args, "convergence_new_min", DEFAULT_AGENTIC_CONVERGENCE_NEW_MIN
            ),
            convergence_window=getattr(
                args, "convergence_window", DEFAULT_AGENTIC_CONVERGENCE_WINDOW
            ),
            exclude_urls=_parse_exclude_urls(getattr(args, "exclude_urls", None)),
            tier_budget=_parse_tier_budget(getattr(args, "tier_budget", None)),
        )
        _apply_sort(payload["results"], args.sort)
        # --adaptive: deep-search mode — top search hit → adaptive crawl
        # using the user's query as semantic filter. Returns site-relevant
        # content from the best-matching domain instead of flat extract.
        if getattr(args, "adaptive", False) and payload.get("results"):
            query_terms = set(
                payload.get("query_terms")
                or {t for t in re.split(r"\s+", args.query.lower()) if len(t) > 2}
            )
            seeds = _rank_adaptive_seeds(payload["results"], query_terms)[:3]
            adaptive_deadline = time.monotonic() + min(args.call_budget, 60.0)
            adaptive_payload = None
            # F-65: seed scoring is PRE-crawl knowledge - a topically perfect
            # candidate may still be an image viewer yielding nothing. Walk
            # candidates until one produces pages, within the SAME budget.
            for seed_url in seeds:
                remaining = adaptive_deadline - time.monotonic()
                if remaining <= 5.0:
                    break
                adaptive_payload = _crawl_adaptive_cached(
                    seed_url,
                    args.query,
                    max_pages=3,
                    call_deadline=time.monotonic() + remaining,
                    no_cache=args.no_cache,
                )
                if adaptive_payload.get("n_pages_extracted", 0) > 0:
                    break
            if adaptive_payload:
                payload["adaptive"] = adaptive_payload
        _emit(payload)
        # F-05: terminal backend failure with NOTHING to show = error (1).
        return (
            1 if (payload.get("terminal_error") and not payload.get("results")) else 0
        )

    if args.cmd == "deep-search":
        llm_mode, flag_err = _llm_flags_from_args(args)
        if flag_err:
            _emit({"error": "conflicting_flags", "detail": flag_err})
            return 2
        ddgs = _build_ddgs(getattr(args, "dht", DEFAULT_DHT), args.proxy, args.timeout)
        proxy_for_log = getattr(args, "proxy", DEFAULT_PROXY)
        payload = _research_composite(
            ddgs,
            args.query,
            depth=args.depth,
            llm_mode=llm_mode,
            llm_model=getattr(args, "model", None),
            summarize=getattr(args, "summarize", False),
            region=args.region,
            safesearch=args.safesearch,
            timelimit=args.timelimit,
            backend=args.backend,
            cache_ttl=getattr(args, "cache_ttl", None) or DEFAULT_CACHE_TTL,
            neg_cache_ttl=getattr(args, "neg_cache_ttl", None) or DEFAULT_NEG_CACHE_TTL,
            extract_chars=getattr(args, "extract_chars", DEFAULT_AGENTIC_EXTRACT_CHARS),
            call_budget=args.call_budget,
            proxy_for_log=proxy_for_log,
            exclude_urls=_parse_exclude_urls(getattr(args, "exclude_urls", None)),
            top_n_override=getattr(args, "top_n", None),
            max_crawl_pages=getattr(args, "max_pages", None),
            max_iter_override=getattr(args, "max_iter", None),
            tier_budget=_parse_tier_budget(getattr(args, "tier_budget", None)),
        )
        _emit(payload)
        return 0

    if args.cmd == "pro":
        llm_mode, flag_err = _llm_flags_from_args(args)
        if flag_err:
            _emit({"error": "conflicting_flags", "detail": flag_err})
            return 2
        ddgs = _build_ddgs(getattr(args, "dht", DEFAULT_DHT), args.proxy, args.timeout)
        proxy_for_log = getattr(args, "proxy", DEFAULT_PROXY)
        try:
            payload = _pro_search(
                ddgs,
                args.query,
                llm_mode=llm_mode,
                model=args.model,
                max_rounds=args.max_rounds,
                coverage_threshold=args.coverage_threshold,
                top_n_per_facet=args.top_n_per_facet,
                region=args.region,
                safesearch=args.safesearch,
                timelimit=args.timelimit,
                backend=args.backend,
                cache_ttl=getattr(args, "cache_ttl", None) or DEFAULT_CACHE_TTL,
                neg_cache_ttl=getattr(args, "neg_cache_ttl", None)
                or DEFAULT_NEG_CACHE_TTL,
                extract_chars=getattr(
                    args, "extract_chars", DEFAULT_AGENTIC_EXTRACT_CHARS
                ),
                call_budget=args.call_budget,
                proxy_for_log=proxy_for_log,
                think=args.think,
                stream=args.stream,
                semantic_coverage=args.semantic_coverage,
                embed_model=args.embed_model,
                auto_crawl=not args.no_auto_crawl,
                crawl_pages_per_domain=args.crawl_pages,
            )
        except OllamaUnavailable as ex:
            # Fail-fast (§5 H1): --use-llm explicitly requested, Ollama unavailable.
            # Do NOT silently fall back to heuristic — that masks the error.
            _emit(
                {
                    "schema": OUTPUT_SCHEMA_VERSION,
                    "query": args.query,
                    "error": "ollama_unavailable",
                    "message": str(ex),
                    "hint": "Drop --use-llm for the heuristic path, or start Ollama.",
                }
            )
            return 1
        _emit(payload)
        return 0

    if args.cmd == "batch":
        ddgs = _build_ddgs(getattr(args, "dht", DEFAULT_DHT), args.proxy, args.timeout)
        results, errors = [], []
        per_q = args.call_budget / max(1, len(args.queries))

        def _one(q):
            try:
                p = _search_with_defense(
                    ddgs,
                    "text",
                    q,
                    region=args.region,
                    safesearch=args.safesearch,
                    timelimit=args.timelimit,
                    max_results=args.max_results,
                    page=1,
                    backend=args.backend,
                    cache_ttl=getattr(args, "cache_ttl", None) or DEFAULT_CACHE_TTL,
                    neg_cache_ttl=getattr(args, "neg_cache_ttl", None)
                    or DEFAULT_NEG_CACHE_TTL,
                    call_deadline=time.monotonic() + per_q,
                    proxy_for_log=proxy_for_log,
                    max_retries=getattr(args, "retries", None),
                )
                return q, p, None
            except SearchError as ex:
                return q, None, str(ex)

        def _extract(q: str, p: dict) -> dict:
            # Parity with text/news --extract: full page content for top-N
            # results per query. Runs inside the batch pool (parallel across
            # queries; _extract_top_urls pools internally per query).
            _apply_sort(p["results"], args.sort)
            if args.extract > 0 and p["results"]:
                query_terms = {t for t in re.split(r"\s+", q) if len(t) > 2}
                p["extracted"] = _extract_top_urls(
                    ddgs,
                    p["results"],
                    args.extract,
                    args.extract_chars,
                    query_terms,
                    query=q,
                )
            return p

        with ThreadPoolExecutor(
            max_workers=args.concurrency, thread_name_prefix="ws"
        ) as ex_:
            for fut in as_completed({ex_.submit(_one, q): q for q in args.queries}):
                q, p, err = fut.result()
                if p is not None:
                    results.append({"query": q, **_extract(q, p)})
                else:
                    errors.append({"query": q, "error": err})

        _emit(
            {
                "schema": OUTPUT_SCHEMA_VERSION,
                "n_queries": len(args.queries),
                "n_ok": len(results),
                "n_errors": len(errors),
                "concurrency": args.concurrency,
                "results": results,
                "errors": errors,
                "metrics": _get_metrics().snapshot(),
            }
        )
        return 0 if not errors else 1

    _build_parser().print_help(sys.stderr)
    return 1


def _cache_stats() -> dict[str, Any]:
    files, size = 0, 0
    if DEFAULT_CACHE_DIR.exists():
        for f in DEFAULT_CACHE_DIR.rglob("*.json"):
            try:
                files += 1
                size += f.stat().st_size
            except OSError as ex:
                log.debug("cache_stat_failed", path=str(f), error=str(ex))
    return {
        "dir": str(DEFAULT_CACHE_DIR),
        "enabled": DEFAULT_CACHE_ENABLED,
        "files": files,
        "size_bytes": size,
        "size_mb": round(size / 1048576, 2),
        "ttl_pos_s": DEFAULT_CACHE_TTL,
        "ttl_neg_s": DEFAULT_NEG_CACHE_TTL,
    }


def _emit_search_error(ex: SearchError) -> None:
    """Emit a structured JSON error for any SearchError subclass, honoring the
    stdout = ONE JSON document contract. Fields are conditionally added based
    on the exception type (SSRFBlocked.resolved_ips, BudgetExceeded.limit, etc.)."""
    payload: dict[str, Any] = {
        "schema": OUTPUT_SCHEMA_VERSION,
        "error": type(ex).__name__,
        "message": str(ex),
    }
    # Enrich with type-specific fields when present.
    for attr in (
        "url",
        "content_type",
        "reason",
        "resolved_ips",
        "budget",
        "limit",
        "actual",
    ):
        val = getattr(ex, attr, None)
        if val is not None:
            payload[attr] = val
    if isinstance(ex, SSRFBlocked):
        payload["hint"] = (
            "URL resolves to a forbidden target (private/loopback/link-local/"
            "metadata). If this is a legitimate internal resource, add the host "
            "to WEB_SEARCH_TRUSTED_HOSTS."
        )
    elif isinstance(ex, BudgetExceeded):
        payload["hint"] = (
            "Response exceeded a byte/char/page budget. Raise the corresponding "
            "WEB_SEARCH_MAX_* env var if the resource is legitimately large."
        )
    elif isinstance(ex, BinaryDocument):
        payload["binary"] = True
        payload["hint"] = (
            "Non-HTML content. Download via curl/wget to a local file, then "
            "invoke the pdf-ocr skill on the local path for SOTA text extraction."
        )
    _emit(payload)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # F-12: SIGINT previously produced a raw traceback + 0-byte stdout.
        # Preserve the output contract: ONE structured JSON document, then
        # exit 130 (128+SIGINT). Work completed so far is lost by design —
        # stateless skill; caches keep whatever finished before the signal.
        _emit(
            {
                "schema": OUTPUT_SCHEMA_VERSION,
                "error": "interrupted",
                "message": "SIGINT received — run aborted; partial results discarded",
                "hint": "Completed sub-operations remain in cache; rerun to resume",
            }
        )
        sys.exit(130)
    except SearchError as ex:
        # Catch-all for policy errors (SSRFBlocked, BudgetExceeded, InvalidURL,
        # BinaryDocument, FetchBlocked, etc.) that escape subcommand handlers.
        # Emits ONE structured JSON document to stdout per the output contract.
        _emit_search_error(ex)
        sys.exit(1)
