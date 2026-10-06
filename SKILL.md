---
name: web-search
description: >-
  Web search, news, page extraction, site crawl, and bot-block bypass
  (Cloudflare/Turnstile) via ddgs 9.x metasearch across 8+ backends
  (DuckDuckGo, Brave, Google, Yandex, Yahoo, Wikipedia, Mojeek, Startpage).
  Returns zero-truncation markdown content + structured JSON. Use for: web
  search, news lookup, deep research, webpage content extraction, site
  crawling, literature scan, source cross-reference, bypassing bot blocks,
  multi-query batch search. Use BEFORE training memory for any factual or
  time-sensitive claim. Pure infrastructure — the AI agent does all
  cognition (analysis, synthesis, claim validation).
---

# Web Search (ddgs + deep extraction)

**Pure search + extraction infrastructure.** Wraps ddgs 9.x metasearch with
industrial-grade rate-limit elimination, full content extraction, BFS site
crawling, and bot-block bypass. The AI agent does ALL cognitive work
(analysis, facts, synthesis). The tool fetches, defends, and structures.

## Prerequisites

```bash
# All deps are HARD — fail-fast if missing (no degraded fallback per AGENTS.md).
# The full set (11 packages) is derived from REQUIRED_DEPS in web_search.py —
# single source of truth. fastembed powers the opt-in semantic reranker
# (WEB_SEARCH_SEMANTIC_RERANK=1) but is always installed.
uv add ddgs curl_cffi playwright trafilatura crawl4ai beautifulsoup4 \
   fastembed readability-lxml html2text rank-bm25 htmldate
uv run playwright install chromium  # headless browser for Cloudflare JS challenges
```

**Recommended: skill-local venv (self-contained, zero-hassle).** The skill
**auto-bootstraps** a `.venv` next to itself on first run (creates venv +
installs all deps + Playwright Chromium). One-time cost (~30s), then every
subsequent run from any cwd is instant and uses the skill's own venv.

To pre-create (skip the auto-bootstrap on first run), from this skill's
directory (the folder containing `SKILL.md` and `web_search.py`):
```bash
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python \
    ddgs curl_cffi playwright trafilatura crawl4ai beautifulsoup4 \
    fastembed readability-lxml html2text rank-bm25 htmldate
.venv/bin/python -m playwright install chromium
```

Env overrides:
- `WEB_SEARCH_NO_SKILL_VENV=1` — force cwd's venv (skip re-exec)
- `WEB_SEARCH_NO_BOOTSTRAP=1` — skip auto-bootstrap (use if `uv` unavailable)
- `WEB_SEARCH_MIN_CONTENT_CHARS=200` — informativeness floor: search results
  whose extracted content is shorter are dropped as boilerplate junk (F-48);
  lower to 0 to disable

Verify:
```bash
./web_search.py --self-check        # from this skill's dir
# → SELF-CHECK OK (0 failures, 30+ checks passed)
```

## Quick reference

```bash
SCRIPT="<skill-dir>/web_search.py"    # skill-dir = folder containing SKILL.md

# Text search (auto-extracts top-3 full pages by default)
uv run python $SCRIPT text "RBI repo rate decision"

# News (time-filtered, latest sorted)
uv run python $SCRIPT news "gold ETF tax" --timelimit m

# Read a single URL (full page content, no truncation)
uv run python $SCRIPT extract https://example.com

# Crawl a site — BFS extracts ALL sub-pages
uv run python $SCRIPT crawl https://quantifiedstrategies.com/ --max-pages 10

# Adaptive crawl — Crawl4AI semantic relevance + confidence stop (--query triggers it)
uv run python $SCRIPT crawl https://en.wikipedia.org/wiki/Exchange-traded-fund \
    --query "ETF creation redemption mechanism" --max-pages 5
# NOTE: --query alone triggers adaptive mode (no --adaptive flag needed).

# Deep-research mode — agentic search + adaptive crawl on top hit
uv run python $SCRIPT agentic "ETF tax treatment India" --adaptive --extract

# Iterative deep search with content extraction
uv run python $SCRIPT agentic "gold ETF LTCG tax india" --extract

# ORCHESTRATOR — progressive disclosure (key_findings + confidence)
uv run python $SCRIPT deep-search "mean reversion strategy backtest" --depth auto

# PERPLEXITY-STYLE — self-reflective agentic (heuristic default, --use-llm opts into Ollama)
uv run python $SCRIPT pro "how do ETFs create and redeem shares"
uv run python $SCRIPT pro "kubernetes pod security" --use-llm --model qwen3.5:9b

# Parallel multi-query
uv run python $SCRIPT batch -q "query one" -q "query two" --concurrency 3

# Introspection
uv run python $SCRIPT backends     # list search engines per category
uv run python $SCRIPT metrics      # cache + circuit + latency state

# Maintenance
uv run python $SCRIPT cleanup --max-age-days 14

# Internal verification (no network)
uv run python $SCRIPT --self-check
```

## Subcommand guide

| Subcommand | Returns | Use when |
|------------|---------|----------|
| `text` | Snippets + **full page content** (top-3 auto-extracted) | Quick lookup or deep read |
| `news` | News snippets + full article content | Current events, market news |
| `extract` | Full page content (single URL, zero truncation) | Read a specific page |
| `crawl` | BFS: ALL sub-pages + full content per page | Deep site extraction (no `--query`) |
| `crawl --query Q` | Crawl4AI semantic crawl (confidence-scored, KB-cached) | Targeted research — returns only pages answering Q |
| `agentic --adaptive` | Iterative search + adaptive crawl on top hit | Deep-research mode (question → best site → semantic) |
| `agentic` | Iterative search: per-iter **backend rotation** (5 engines) + **query breadth modifiers** + **convergence stop** + full content extract (top-8 by relevance) | Broad research coverage |
| `deep-search` | **Orchestrator**: search → extract → key_findings → confidence + optional crawl. Progressive disclosure via `--depth auto\|shallow\|medium\|deep`. Depth knobs: `--top-n` (default 12), `--max-pages` (default 10), `--max-iter` (default 8) | One-shot deep research (agent gets key_findings first, decides if more depth needed) |
| `pro` | **Perplexity-style agentic**: decompose → multi-round search+extract → coverage reflection → synthesis with [n] citations. Heuristic path by default (zero-config); `--use-llm` opts into local Ollama for LLM decomposition, gap reflection, prose synthesis, follow-up questions | Deep multi-faceted research with synthesis |
| `batch` | Parallel results for N queries | Multi-topic scan |
| `backends` | Available search engines | Introspection |
| `metrics` | System health snapshot | Debugging |

## Full content access (zero truncation)

**Every search result includes BOTH snippet AND full page content.**

- `agentic`: extracts top-N pages after iterative search (default `--top-n-extract 12`)
- `crawl`: extracts EVERY page visited, full content stored (no truncation — tested up to 3.8M chars)
- `extract`: single URL, full content returned as markdown
- `agentic --extract`: extracts top-N pages after iterative search

Content format: **`text_markdown`** — headers (`#`), links (`[text](url)`),
lists (`*`), bold (`**text**`), code blocks. Directly readable by AI agents.

### SOTA markdown cascade (auto, no flag needed)

Every successful fetch (Layer 0/1/2/3) runs through a quality cascade that
picks the **best output per page**. Both extractors are HARD deps — no
degraded fallback.

```
HTML fetched → _html_to_markdown(html, url) cascade:
  1. trafilatura.extract()        # SOTA article extraction (#1 in 3 benchmarks)
                                  # best for: news, wikipedia, blogs, anything with a clear main article
  2. crawl4ai DefaultMarkdownGenerator  # SOTA LLM-markdown (BM25 noise filter available)
                                  # best for: non-article pages, JS-heavy, structured content
```

**Quality tradeoff:**
- `trafilatura` extracts the main article body and strips nav/sidebar/footer/ads noise. Best signal-to-noise. May flatten structure on pages without clear article semantics (returns plain text).
- `crawl4ai raw_markdown` preserves ALL structure (headers, links, lists, tables) but includes some noise. Set `WEB_SEARCH_CRAWL4AI_FIT=1` for BM25-pruned `fit_markdown`.

**Force a specific extractor** (debugging/testing):
```bash
WEB_SEARCH_EXTRACTOR=trafilatura python $SCRIPT extract https://...
WEB_SEARCH_EXTRACTOR=crawl4ai     python $SCRIPT extract https://...
WEB_SEARCH_EXTRACTOR=auto         python $SCRIPT extract https://...  # default cascade
```

## Agentic depth architecture

`agentic QUERY` runs an iterative deepening loop with 5 diversity axes. The
loop is **pure infrastructure** — no LLM cognition. All cognitive work
(query decomposition, reflection, synthesis) is the AI agent's job.

```
iter 0: backend=brave      query=<original>                                 page=1
iter 1: backend=duckduckgo query=<original> + "guide OR explained OR overview"  page=1
iter 2: backend=yandex     query=<original> + "2024 OR 2025 OR latest"         page=1
iter 3: backend=google     query=<original> + "analysis OR comparison OR review" page=1
iter 4: backend=yahoo      query=<original> + "example OR case study OR findings" page=1
iter 5+: rotation wraps → page increments to 2,3,...
```

**Stop conditions** (any one fires):
- `max_iter` reached (default 5)
- `converged`: N consecutive iters with `n_new < CONVERGENCE_NEW_MIN` (default 2 iters < 2 new results each = corpus exhausted)
- `budget_exhausted`: `--call-budget` seconds elapsed
- `rate_limited_partial` / `ddgs_error_partial`: terminal backend failure

**Result handling:**
- Dedup by canonical URL across all iters
- Final sort: tier-weighted (`tier*100 + relevance*10`)
- Diversify: max `--max-per-domain` (default 2) per host
- Extract top-N (default 8) by **relevance-first** ranking (`-relevance_score, tier`) — kills the old failure mode where T0 spam (i18n Wikipedia, gov.in redirects) wasted all extract slots while perfect T3 matches never got fetched

**Per-iteration metadata** in `iterations[]` array:
```json
{
  "index": 0, "query": "ETF tax treatment India",
  "backend": "brave",            // iter's preferred backend
  "backends_pool": "brave,duckduckgo,yandex,google,yahoo",  // fallback chain
  "page": 1, "n_results": 10, "n_new": 10,
  "relevant_domains_total": 1,  // query-language-aware (F3: filters i18n spam)
  "consecutive_low_yield": 0,    // F1 convergence counter
  "cache_status": "miss"
}
```

**LLM-assisted iteration (auto by default):** when a local LLM answers the
provider probe, `agentic` gets two assist passes — (1) per-iteration query
refinement: after iter 0, the LLM proposes one alternative phrasing targeting
the observed coverage gap (falls back to the static breadth modifier if the
LLM call fails or budget < 30s); (2) final relevance rerank: ONE extra LLM
call scores the top-30 diversified results 0-10, re-sorting before extraction
(`llm_relevance` per result, -1 = unscored; never raises, budget-guarded at
60s). Opt out with `--no-llm`; force with `--use-llm` (fail-fast if no local
LLM). Payload carries `llm_assisted`, `llm_provider`, `llm_reranked_n`; each
iteration's `query_source` is `original|static|llm`. deep-search inherits the
same flags through its agentic phase.

**Why no LLM query decomposition in skill:** by design — `agentic` is a
single-query deepening primitive. For multi-perspective research, the AI
agent decomposes the question itself and fires multiple `batch -q` queries:

```bash
# Agent does cognition (decomposition), skill does fetch (depth per sub-query)
uv run python $SCRIPT batch \
    -q "ETF STCG tax India" \
    -q "ETF LTCG tax India" \
    -q "gold ETF tax India" \
    -q "ETF dividend tax India" \
    -q "NRI ETF tax India" \
    --concurrency 3
```

### Feedback loop (agent-driven refinement)

The skill is single-shot per call. The agent closes the feedback loop by
reading the payload, identifying useless extracts, and re-firing `agentic`
with `--exclude-urls`. Fresh URLs surface in subsequent iterations:

```bash
# Iteration 1: broad agentic search
uv run python $SCRIPT agentic "ETF tax India" > /tmp/r1.json
# Agent reads /tmp/r1.json: extracts #2 and #4 were off-topic (Bollywood,
#   election commission). Extract their URLs.

# Iteration 2: exclude the useless URLs, get fresh coverage
uv run python $SCRIPT agentic "ETF tax India" \
    --exclude-urls "https://bollymoviereviewz.com/...,https://results.eci.gov.in/"
# → fresh URLs surface because excluded ones are pre-seeded into dedup set

# Iteration 3+: combine with --tier-budget for source-type diversity
uv run python $SCRIPT agentic "ETF tax India" \
    --exclude-urls "$(cat /tmp/useless.txt)" \
    --tier-budget "T0:3,T1:5,T3:5"
```

For large exclusion lists, write URLs to a file (one per line, `#` comments)
and pass `--exclude-urls @/tmp/useless.txt`.

### Deep-research recipe (agent-driven multi-facet coverage)

The skill has **no LLM cognition by design** — `agentic` is a single-query
deepening primitive. For multi-faceted research (Perplexity-style coverage
without the LLM), the AI agent decomposes the question itself and fires
`batch -q` per facet, then deepens each facet with `agentic --top-n-extract`:

```bash
# Step 1: decompose the question into facets (agent cognition)
FACETS=(
  "ETF STCG tax India"
  "ETF LTCG tax India"
  "gold ETF tax India"
  "ETF dividend tax India"
  "NRI ETF tax India"
)

# Step 2: broad coverage — one search per facet, parallel
ARGS=(); for f in "${FACETS[@]}"; do ARGS+=(-q "$f"); done
uv run python $SCRIPT batch "${ARGS[@]}" --concurrency 3 > /tmp/broad.json

# Step 3: deepen the weakest facet (agent reads broad.json, picks thinnest)
uv run python $SCRIPT agentic "NRI ETF tax India" \
    --top-n-extract 20 --max-iter 12 > /tmp/deep_nri.json

# Step 4: exclude already-seen URLs, re-search for fresh coverage
uv run python $SCRIPT agentic "ETF dividend tax India" \
    --exclude-urls "$(jq -r '.results[].canonical_url' /tmp/broad.json | paste -sd,)"
```

This pattern gives Perplexity-equivalent multi-perspective coverage using
only the skill's infrastructure primitives. The `pro` subcommand (below)
automates this loop with optional LLM-driven reflection.


## Research orchestrator (progressive disclosure)

The `research` command is a **thin orchestrator** that composes existing primitives
(search → extract → crawl) with a depth-escalation strategy. The agent gets
**progressive disclosure** — key findings first (cheap), full pages on demand.

```bash
# Auto-depth (default): starts medium, escalates to deep if confidence < 0.6
uv run python $SCRIPT deep-search "volatility risk premium VIX strategy" --depth auto

# Shallow: search only (fastest, ~2K chars context)
uv run python $SCRIPT deep-search "Rust ownership lifetimes" --depth shallow

# Medium: search + extract top 12 + key_findings (~180K chars)
uv run python $SCRIPT deep-search "mean reversion backtest" --depth medium

# Deep: + crawl top 1-2 domains, 10 pages each (~480K chars)
uv run python $SCRIPT deep-search "Kubernetes pod security" --depth deep

# Deep with explicit knobs (override the defaults above)
uv run python $SCRIPT deep-search "CRISPR Cas9 mechanism" --depth deep \
    --top-n 20 --max-pages 15 --max-iter 12
```

**Output structure** (progressive disclosure within the JSON):

| Layer | Field | Context cost | When to read |
|-------|-------|-------------|--------------|
| 1 (cheap) | `key_findings[]` | ~2K chars | **Always first** — best query-relevant passage per page |
| 2 (medium) | `extracted[]` | ~80K chars | If key_findings insufficient for the task |
| 3 (expensive) | `crawled[]` | ~160K chars | If site-wide depth needed (depth=deep only) |
| meta | `confidence` + `next_steps_hint` | ~200 chars | Decides whether to go deeper |

**Confidence scoring** (0-1):
- `≥0.7` = `sufficient_evidence` — agent can trust the findings
- `0.4-0.7` = `partial_evidence` — agent should consider `next_steps_hint`
- `<0.4` = `insufficient_evidence` — agent should refine query or go `--depth deep`

**Stateless** — each call is independent. Agent tracks seen URLs via `--exclude-urls`.

**Depth knobs (override the hardcoded defaults):**

| Flag | Default | Effect |
|------|---------|--------|
| `--top-n N` | `12` | pages extracted in the agentic phase (was hardcoded 5 — the shallow-results bug). `0` disables extraction. |
| `--max-pages N` | `10` | pages crawled per top-domain in `--depth deep` (was hardcoded 5). `0` skips crawl. |
| `--max-iter N` | `8` | agentic loop iterations. |

**Profile presets (WEB_SEARCH_PROFILE):** one env var sets the whole
rate/backoff posture — polite for single-host crawls, aggressive for
multi-backend batch research. Standard is byte-identical to the historic
defaults. Per-knob env vars still win over the preset.

| Knob | polite | standard | aggressive |
|------|--------|----------|------------|
| `RATE_LIMIT` initial | 0.5 | 2.0 | 5.0 |
| `RATE_CAP` | 1.0 | 10.0 | 20.0 |
| `RATE_FLOOR` | 0.05 | 0.2 | 0.5 |
| `RATE_INCREASE` | 0.05 | 0.2 | 0.5 |
| `MAX_RETRIES` | 3 | 4 | 6 |
| `BACKOFF_BASE` | 3.0 | 1.5 | 1.0 |
| `BACKOFF_CAP` | 60.0 | 30.0 | 15.0 |
| `CB_FAILURE_THRESHOLD` | 2 | 3 | 5 |
| `CB_COOLDOWN` | 120.0 | 60.0 | 20.0 |

**LLM-synthesized answer (WS7):** pass `--summarize` to get a Perplexity-style
final answer on top of the results — same synthesis engine as `pro` (cited
prose + quote grounding + follow-ups, `think` off for speed). Fails fast when
no local LLM runtime answers; mutually exclusive with `--no-llm`. Payload adds
`summarized`, `synthesis`, `citation_grounding[]`, `ungrounded_citations`.
Available on both `agentic` and `deep-search`.


## Pro mode (Perplexity-style agentic research)

`pro QUERY` runs a self-reflective agentic loop: **decompose → multi-round
search+extract → coverage reflection → synthesis with citations**. Two paths:

| Path | Trigger | Behaviour |
|------|---------|-----------|
| **auto** (default) | no flag | probes local LLM once (Ollama `/api/tags` or OpenAI-compatible `/v1/models`); LLM path if reachable, heuristic otherwise. Logged as `pro_llm_auto selected=llm\|heuristic`; payload carries `llm_mode`/`llm_provider`. |
| **heuristic** | `--no-llm` | noun-phrase decomposition, keyword coverage scoring, structured markdown synthesis. Forced, no LLM contact at all. |
| **LLM** | `--use-llm` | LLM decomposition (sub-questions), gap reflection, prose synthesis with `[n]` citations, quote-grounded SOURCES verification, follow-up questions. **Fail-fast** if no local LLM reachable (explicit opt-in must not silently degrade). |

```bash
# AUTO (default): LLM path if a local LLM answers the probe, heuristic otherwise
uv run python $SCRIPT pro "rust ownership and borrowing"

# Force heuristic (zero LLM contact)
uv run python $SCRIPT pro "rust ownership and borrowing" --no-llm

# Force LLM path — fail-fast if no local LLM reachable
uv run python $SCRIPT pro "how do ETFs create and redeem shares" --use-llm

# Override model (no hardcoded model anywhere)
uv run python $SCRIPT pro "kubernetes pod security" --use-llm --model qwen3.5:9b

# Tune reflection loop
uv run python $SCRIPT pro "CRISPR Cas9 mechanism" --use-llm \
    --max-rounds 4 --coverage-threshold 0.85 --top-n-per-facet 6

# Fast mode (--no-think) — 44x faster than chain-of-thought (default: think ON)
uv run python $SCRIPT pro "what is kubernetes pod security" --use-llm

# Full quality: chain-of-thought + streaming + semantic coverage
uv run python $SCRIPT pro "ETF creation redemption arbitrage mechanism" --use-llm \
    --think --stream --semantic-coverage

# Override models (no hardcoded model anywhere)
uv run python $SCRIPT pro "kubernetes pod security" --use-llm \
    --model llama3.2 --embed-model all-minilm
```

### Pipeline

```
1. DECOMPOSE
   heuristic: split on conjunctions → noun-phrase facets (1-6)
   llm:       Ollama prompt → 3-6 sub-questions

2. MULTI-ROUND LOOP (max_rounds, default 5)
   round 0: batch search all facets (parallel) + extract top-15
   round 1+: coverage check → if <threshold: generate gap queries → search+extract
             stop conditions: coverage ≥ threshold | no gaps | converged | budget

3. AUTO-CRAWL (default ON, --no-auto-crawl to disable)
   rank domains by result count → crawl top-3 (skip search engines/social)
   10 pages each → adds ~20-30 full pages of evidence beyond search snippets

4. SYNTHESIZE
   heuristic: structured markdown — key findings ranked by relevance, [[n]] citations
   llm:       prose answer with [n] citations (post-checked against source list),
              20 sources × 4000 chars evidence, num_ctx=32768, follow-up questions,
              then quote-grounding: model appends a SOURCES: section with one
              verbatim ≤40-word quote per citation; each quote is fuzzy-verified
              (difflib, ratio floor 0.65) against the extracted page content and
              the per-citation verdict lands in `citation_grounding[]`.
```

### Output schema (progressive disclosure)

```json
{
  "schema": "2.0",
  "query": "...",
  "mode": "heuristic|llm",
  "llm_mode": "auto|on|off",
  "llm_available": true,
  "llm_provider": "ollama|openai|null",
  "model": "qwen3.5:9b | null",
  "sub_queries": ["facet1", "facet2", ...],
  "rounds": [{"round": 0, "status": "ok", "n_queries": 3, "n_new_results": 20, ...}],
  "n_rounds": 2,
  "coverage": 0.85,
  "coverage_detail": {"rust": ["url1","url2"], "ownership": ["url1"], ...},
  "confidence": 0.9,
  "confidence_reason": "sufficient_evidence",
  "synthesis": "# Question\n\n## Key findings\n1. ... [[1]]\n...",
  "verified_citations": [0, 1, 2],
  "citation_grounding": [
    {"index": 1, "quote": "verbatim quote from the source", "grounded": true, "ratio": 1.0},
    {"index": 2, "quote": "quote not found in extracted content", "grounded": false, "ratio": 0.31}
  ],
  "ungrounded_citations": 1,
  "follow_ups": ["related question 1", ...],
  "key_findings": [{"url":"...", "passage":"...", "relevance": 0.8}],
  "sources": [{"url":"...", "title":"...", "host":"...", "tier": 0}]
}
```

### Anti-hallucination safeguards (LLM path)

- **Verbatim evidence only**: LLM receives full extracted passages (≤2000 chars each, top-10 sources), not snippets.
- **Numbered citations**: prompt instructs `[1]`, `[2]` etc. matching the provided source list.
- **Post-check**: every `[n]` in synthesis is regex-extracted and verified against the source list. Out-of-range citations logged as `llm_citation_overflow`.
- **Quote grounding (WS5)**: the LLM must append a `SOURCES:` section with one verbatim ≤40-word quote per cited source. Each quote is fuzzy-verified against the source's extracted content (exact substring → ratio 1.0; else sliding-window `difflib.SequenceMatcher`, floor 0.65). Verdicts surface in `citation_grounding[]` + `ungrounded_citations`. Ungrounded citations are **flagged, never removed** — the verifier reports, the agent judges.
- **No fabrication instruction**: system prompt explicitly says "If evidence does not cover an aspect, say so explicitly."

### LLM config (no hardcoded models)

| Var | Default | Purpose |
|-----|---------|---------|
| `WEB_SEARCH_LLM_MODEL` | `qwen3.5:9b` | generation model (override freely) |
| `WEB_SEARCH_EMBED_MODEL` | `nomic-embed-text` | embedding model for `--semantic-coverage` |
| `WEB_SEARCH_LLM_PROVIDER` | `auto` | `auto` (probe the host) \| `ollama` \| `openai` — pin to skip probing |
| `WEB_SEARCH_LLM_HOST` | `http://localhost:11434` | LLM REST base (localhost only — §7g data-egress guard). Host ending in `/v1` → OpenAI-compatible; else probed (`/api/tags` → ollama, `/v1/models` → openai) |
| `WEB_SEARCH_OLLAMA_HOST` | *(legacy alias)* | backward-compat alias for `WEB_SEARCH_LLM_HOST` |
| `WEB_SEARCH_LLM_MAX_TOKENS` | `4096` | max tokens per LLM call (synthesis auto-raises to 8192 with --think) |
| `WEB_SEARCH_LLM_TIMEOUT` | `120.0` | per-LLM-call timeout (s) (synthesis auto-raises to 300 with --think) |
| `WEB_SEARCH_LLM_THINK` | `1` | chain-of-thought ON by default for deeper reasoning; `0` for 44x faster |

CLI overrides: `--model <name>`, `--embed-model <name>`, `--think`, `--stream`, `--semantic-coverage`.

**Latency/quality knobs (think ON by default for quality; --no-think for speed):**

| Flag | Default | Effect |
|------|---------|--------|
| `--think` / `--no-think` | ON | chain-of-thought reasoning. ON = deeper decomposition + better synthesis (8192 max_tokens, 300s timeout). OFF = 44x faster (549ms vs 24s per call). Env `WEB_SEARCH_LLM_THINK=0`. |
| `--stream` | OFF | progressive synthesis output to stderr. Agent sees `llm_stream_progress` events (chars=405, 612, ...) while the LLM generates instead of waiting in silence. |
| `--semantic-coverage` | OFF | embedding-based coverage via `nomic-embed-text`. Catches paraphrased evidence keyword coverage misses (e.g. "share creation" covered when query asks "ETF creation"). Adds ~50ms/passage. |
| `--embed-model <name>` | env | override embedding model (no hardcoded model anywhere). |
| `--max-rounds N` | 5 | reflection rounds (was 3). More rounds = deeper gap-filling. |
| `--top-n-per-facet N` | 15 | pages extracted per round (was 8). More = wider evidence pool. |
| `--no-auto-crawl` | OFF | disable post-search domain crawling. Default ON: crawls top 3 domains for 10 pages each → adds ~20-30 full pages of evidence that search snippets can't surface. |
| `--crawl-pages N` | 10 | pages to crawl per top-domain in auto-crawl phase. |

**Egress guard**: if the resolved LLM host (`WEB_SEARCH_LLM_HOST`, any
provider) points off-machine, a warning is logged (`llm_egress_nonlocal`).
Local LLMs only per §7g.

**Model choice = primary quality lever.** Dense models (qwen3.5:9b, 9B active
params) produce 2-3x longer synthesis with 3-4x more verified citations than
MoE models (lfm2.5-8b-A1B, 1B active). MoE models are faster and read more
pages (less time per LLM call → more crawl budget), but write shallower prose.
This is a model capacity limit, not a config issue — no `max_tokens` bump fixes
it. Pick your tradeoff:

| Model type | Depth | Synthesis quality | Speed |
|---|---|---|---|
| Dense 9B+ (qwen3.5:9b) | fewer pages (LLM calls eat crawl time) | best prose + citations | slower |
| MoE 1B-active (lfm2.5-8b) | more pages (fast LLM → more crawl budget) | shorter, fewer citations | faster |
| Heuristic (no LLM) | most pages (zero LLM overhead) | structured markdown only | fastest |

**Fail-fast (§5 H1)**: `--use-llm` + Ollama down/model-not-found → structured
JSON error (`error: "ollama_unavailable"`), exit 1. NOT silent heuristic fallback.



The tool accesses ANY live website, including Cloudflare-protected sites:

## Bot-block bypass (4-layer)

The tool accesses ANY live website, including Cloudflare-protected sites:

```
Layer 0: Cookie reuse (cf_clearance cached → instant, no browser needed)
    ↓ if expired/missing
Layer 1: ddgs/primp (TLS impersonation → handles ~80% of sites) → raw HTML → cascade
    ↓ if bot-blocked
Layer 2: curl_cffi (complete Chrome headers + JA3 fingerprint + cookie capture) → cascade
    ↓ if JS challenge
Layer 3: Playwright stealth (headless Chromium + navigator patches)
         solves JS challenge → saves cf_clearance cookies for Layer 0 reuse → cascade
```

**Layer 1 note**: fetches raw HTML via primp (was: pre-converted markdown that
skipped the cascade). This unlocks trafilatura/crawl4ai quality on every
primp-successful fetch.

## Adaptive crawl (Crawl4AI semantic)

`crawl URL --query Q` auto-triggers Crawl4AI's `AdaptiveCrawler.digest()`
(no `--adaptive` flag needed — `--query` alone picks adaptive mode).
Scrapes pages, ranks by semantic relevance to the query, stops at a
confidence threshold (typically 70%+). Returns ONLY pages that answer the
query — no tangential noise. Results cached per `(URL, query)` for TTL.

```
BFS crawl (no --query):  brute-force, returns max_pages random links
                         → 5 pages, 245K chars (ETF + Stocks + Stock exchange + ...)
Adaptive crawl (--query): semantic, returns top-relevance pages
                         → 1 page, 111K chars (only the ETF page, score 1.0, conf 70%)
```

Adaptive output adds: `crawler: "crawl4ai-adaptive"`, `query`, `confidence`
(0-1), per-page `score`, `cache_status` (fresh|miss), `served_from` (cache|live).

**All extraction is cached** — `_extract_with_fallback` caches per-URL results
(positive + negative) using the same TTL as search cache. Repeat extracts of
the same URL = instant. Negative cache prevents hammering known-broken URLs.

**`agentic QUERY --adaptive`** = deep-search mode: runs the agentic search
loop, picks the top hit, runs adaptive crawl on it using the user's query as
semantic filter. Returns `payload.adaptive` alongside `results`/`extracted`.
Best for "research this question on the best-matching domain" workflows.

**Cookie persistence**: solve challenge ONCE via Playwright → all subsequent
pages on the same domain use cached cookies with curl_cffi (100x faster).

**Stealth patches** injected by Playwright:
- `navigator.webdriver` → undefined
- `navigator.plugins` → realistic Chrome plugin list
- `navigator.languages` → `['en-US', 'en']`
- `window.chrome.runtime` → present
- Permissions query → patched
- `--disable-blink-features=AutomationControlled` flag

**Human-paced behavior**: random 1-3.5s delays between crawl page fetches.

Verified on: quantifiedstrategies.com (Cloudflare JS challenge),
TradingView (heavy SPA), QuantConnect, Investopedia, Wikipedia, RBI.gov.in.

## Output contract

**stdout** = ONE JSON document. **stderr** = structured JSON logs (one per line).

```json
{
  "schema": "2.0",
  "query": "...",
  "category": "text",
  "results": [
    {
      "title": "...",
      "body": "~150 char snippet...",
      "href": "https://original-url",
      "canonical_url": "https://url-no-tracking-params",
      "host": "example.com",
      "tier": 0,
      "rank": 1,
      "date": "2025-08-01",
      "age_days": 8,
      "language": "en",
      "relevance_score": 3,
      "source": "yandex"
    }
  ],
  "extracted": [
    {
      "url": "https://...",
      "content": "# Full page markdown content...",
      "chars": 45230,
      "key_passage": "Most relevant paragraph...",
      "tier": 0
    }
  ],
  "citations": [
    {"url": "...", "title": "...", "host": "...", "tier": 0, "snippet": "..."}
  ],
  "meta": {
    "cache_status": "fresh|miss|stale|neg",
    "served_from": "cache|live|coalesced_cache",
    "attempts": [{"backend": "yandex", "ok": true, "latency_s": 1.2}],
    "backends_tried": ["google", "wikipedia", "yandex"],
    "rate_limited": false
  }
}
```

### Binary content (PDF/DOCX/…) — agent routing signal

When `extract` (or an `agentic`/`crawl` sub-page) hits a non-HTML URL
(`application/pdf`, `.docx`, etc.), the skill does **NOT** attempt parsing.
It returns a structured signal so the AI agent can route to the `pdf-ocr`
skill for SOTA born-digital + OCR text extraction:

```json
{
  "schema": "2.0",
  "url": "https://rbi.org.in/circular.pdf",
  "host": "rbi.org.in",
  "tier": 0,
  "binary": true,
  "content_type": "application/pdf",
  "error": "binary_content",
  "hint": "Non-HTML content. Download via curl/wget to a local file, then invoke the pdf-ocr skill on the local path for SOTA text extraction (born-digital + OCR routing).",
  "content": null
}
```

Detection is two-layer: (1) URL path ends in a known binary extension
(`.pdf`, `.docx`, `.xlsx`, `.pptx`, `.zip`, `.epub`, …) → instant, no fetch;
(2) `Content-Type` header sniff after Layer 2/3 fetch (catches extensionless
PDFs like `https://arxiv.org/pdf/2401.00001`). Agent flow on signal:

```bash
curl -sL -o /tmp/doc.pdf "<url>"          # download
uv run python ~/.omp/agent/skills/pdf-ocr/scripts/pdf_ocr.py \
    /tmp/doc.pdf /tmp/doc.md               # extract via pdf-ocr skill
```

**Crawl output** adds:
```json
{
  "start_url": "https://site.com/",
  "host": "site.com",
  "max_depth": 0,
  "robots": {"fetched": true, "crawl_delay": 2, "sitemap_seeded": 37},
  "n_pages_extracted": 5,
  "n_errors": 0,
  "n_binary_skipped": 2,
  "binary_urls": [
    {"url": "https://site.com/report.pdf", "content_type": "application/pdf", "depth": "1"}
  ],
  "n_total_links_found": 431,
  "total_content_chars": 194280,
  "pages": [
    {"url": "...", "title": "...", "content": "full markdown...", "chars": 22599,
     "depth": 0, "n_links_found": 71, "latency_s": 0.6}
  ],
  "all_links_found": ["url1", "url2", ...]
}
```

The `robots` object reports crawl-start intelligence: whether robots.txt was
fetched, the declared `Crawl-delay` (topped up beyond the 1-3.5s human delay),
and how many URLs were seeded into the frontier from sitemaps. Disallowed
links are never fetched (logged `crawl_robots_disallow`); the start URL itself
is never robots-gated (it is a directed fetch).

`all_links_found` is capped at 100 entries and `links_not_followed` at 50 in
the payload (full sets live in `n_total_links_found` counts) — re-run crawl
with a narrower `--max-depth` if you need exhaustive link enumeration.

`binary_urls[]` collects every PDF/DOCX/etc. sub-page encountered during BFS
so the agent can batch-download + batch-invoke pdf-ocr without losing them.

### Domain tiers (objective source-quality metadata)

- **T0**: official/regulatory (sec.gov, rbi.org.in, federalreserve.gov, bis.org,
  wikipedia.org, .edu, .gov)
- **T1**: tier-1 financial news (reuters.com, bloomberg.com, ft.com,
  economictimes.indiatimes.com, investopedia.com)
- **T3**: unclassified

### Sort strategies (`--sort`)

- `latest` (default): newest dated first, undated sinks by authority
- `authority`: T0→T1→T3, then date
- `relevance`: keyword overlap, then authority

## Rate-limit elimination (9-layer)

| # | Layer | What | Default |
|---|-------|------|---------|
| 1 | DHT cache (env `WEB_SEARCH_DHT=1`) | P2P distributed cache | off |
| 2 | Local TTL cache | File-backed, negative cache | 24h/30m |
| 3 | Single-flight | Coalesces concurrent identical queries | on |
| 4 | Adaptive limiter (AIMD) | Self-tuning: speeds up on success | 2→10 req/s |
| 5 | Circuit breaker | Per-backend quarantine after 3 fails | 60s cooldown |
| 6 | Latency selection | Prefers fast backends (p50/p99) | rolling 50 |
| 7 | Backoff + jitter | Exponential on rate-limit/timeout | 1.5s, 30s cap |
| 8 | Backend rotation | Shuffles 8+ backends, rotates on retry | auto |
| 9 | Proxy | http/https/socks5, creds redacted | none |

## How the AI agent should use this tool

### Quick factual lookup
```bash
uv run python $SCRIPT text "current fed funds rate" --timelimit m --extract 0
# Fast: snippets only, no page extraction
```

### Deep research (agent orchestrates)
```bash
# Step 1: broad search with full content
uv run python $SCRIPT text "SEC climate disclosure rules ETF impact"
# Step 2: read specific pages discovered
uv run python $SCRIPT extract https://www.sec.gov/rules/climate
# Step 3: crawl a key source for ALL sub-pages
uv run python $SCRIPT crawl https://www.sec.gov/climate-disclosure --max-pages 10
```

### PDF / binary document in results (agent routes to pdf-ocr)
The skill detects PDF/DOCX/etc. URLs (via extension pre-check or Content-Type
sniff) and returns `error: "binary_content"` — it does NOT parse them. The
AI agent must download + invoke the pdf-ocr skill:

```bash
# Step 1: extract attempt returns binary signal
uv run python $SCRIPT extract https://rbi.org.in/circulars/2024/notification.pdf
# → {"error": "binary_content", "content_type": "application/pdf", "hint": "..."}

# Step 2 (agent): download + route to pdf-ocr skill
curl -sL -o /tmp/circular.pdf "https://rbi.org.in/circulars/2024/notification.pdf"
uv run python ~/.omp/agent/skills/pdf-ocr/scripts/pdf_ocr.py /tmp/circular.pdf /tmp/circular.md

# Same flow when PDFs surface inside crawl results — they appear in
# payload.binary_urls[] for batch handling.
```

### Crawl a protected site (Cloudflare bypass)
```bash
uv run python $SCRIPT crawl https://quantifiedstrategies.com/ --max-pages 10
# First page: Playwright solves challenge (~20s) + caches cookies
# Subsequent pages: cookie reuse via curl_cffi (~1-3s each)
```

### Cross-reference (batch)
```bash
uv run python $SCRIPT batch \
    -q "claim site:reuters.com" \
    -q "claim site:bloomberg.com" \
    --concurrency 3
```

### Breaking news
```bash
uv run python $SCRIPT news "market crash" --timelimit d --sort latest
```

## Environment variables

### Search
| Var | Default | Purpose |
|-----|---------|---------|
| `WEB_SEARCH_REGION` | `us-en` | region/locale |
| `WEB_SEARCH_PROFILE` | `standard` | rate/backoff posture preset: `polite` \| `standard` \| `aggressive` (see table below). Individual env knobs still override the preset. Invalid value fails fast at startup. |
| `WEB_SEARCH_ROBOTS` | `1` | set `0` to disable robots.txt awareness (politeness gating + crawl-delay top-up + sitemap discovery) |
| `WEB_SEARCH_SITEMAP` | `1` | set `0` to disable sitemap.xml frontier seeding at crawl start |
| `WEB_SEARCH_SITEMAP_MAX` | `500` | max URLs seeded into the crawl frontier from sitemaps (clamped 1-5000) |
| `WEB_SEARCH_CHALLENGE_ATTEMPTS` | `2` | Playwright bot-challenge re-solve passes (0 = legacy single settle; clamp 0-5) |
| `WEB_SEARCH_CHROME_UA_MAJOR` | *(auto)* | Pin the advertised Chrome major. Default: derived from the installed Playwright Chromium binary (`--version`, mtime-cached), falling back to the built-in constant |
| `WEB_SEARCH_LANG_GATE` | `1` | set `0` to disable the query-language gate (F-59) |
| `WEB_SEARCH_LANG_FLOOR` | `5` | fail-open floor: mismatched-language results are dropped only when ≥N same-language results survive |
| `WEB_SEARCH_SAFESEARCH` | `moderate` | on/moderate/off |
| `WEB_SEARCH_MAX_RESULTS` | `10` | results per call |
| `WEB_SEARCH_BACKEND` | `auto` | backend or comma-delimited |
| `WEB_SEARCH_TIMEOUT` | `10` | per-request timeout (s) |
| `WEB_SEARCH_PROXY` | (none) | proxy URL (or `DDGS_PROXY`) |
| `WEB_SEARCH_DDGS_THREADS` | `0` | ddgs internal thread count (0=auto) |
| `WEB_SEARCH_MAX_PER_DOMAIN` | `3` | max results per domain (dedup) |
| `WEB_SEARCH_LOG_LEVEL` | `INFO` | log level (DEBUG/INFO/WARNING/ERROR) |

### Rate-limit defense
| Var | Default | Purpose |
|-----|---------|---------|
| `WEB_SEARCH_MAX_RETRIES` | `4` | max retries per call |
| `WEB_SEARCH_RATE_LIMIT` | `2.0` | initial rate (req/s) |
| `WEB_SEARCH_RATE_BURST` | `5` | token bucket burst |
| `WEB_SEARCH_RATE_CAP` | `10.0` | max adaptive rate |
| `WEB_SEARCH_RATE_FLOOR` | `0.2` | minimum AIMD rate (req/s) |
| `WEB_SEARCH_RATE_INCREASE` | `0.2` | AIMD additive increase step (req/s) |
| `WEB_SEARCH_RATE_DECREASE_FACTOR` | `0.5` | AIMD multiplicative decrease on rate-limit |
| `WEB_SEARCH_BACKOFF_BASE` | `1.5` | exponential backoff base (s) |
| `WEB_SEARCH_BACKOFF_CAP` | `30.0` | max backoff sleep (s) |
| `WEB_SEARCH_BACKOFF_JITTER` | `0.3` | jitter fraction (±30%) |
| `WEB_SEARCH_CB_FAILURE_THRESHOLD` | `3` | circuit breaker threshold |
| `WEB_SEARCH_CB_COOLDOWN` | `60.0` | circuit cooldown (s) |
| `WEB_SEARCH_CALL_BUDGET` | `600.0` | per-call total budget (s) |

### AIMD tuning (Adaptive Inline Multiplicative Decrease)

The rate limiter is **self-tuning**: on every successful request it
**additively** increases rate by `RATE_INCREASE` (slow ramp-up); on every
rate-limit/timeout it **multiplicatively** decreases by
`RATE_DECREASE_FACTOR` (fast back-off). Bounded by `[RATE_FLOOR, RATE_CAP]`.

```
success → rate = min(CAP, rate + INCREASE)        # +0.2 req/s, slow climb
429/timeout → rate = max(FLOOR, rate * DECREASE)  # ×0.5, fast halving
```

| Knob | Default | Effect when raised | Effect when lowered |
|------|---------|--------------------|---------------------|
| `RATE_LIMIT` | `2.0` | starts hotter (faster first burst) | more conservative cold-start |
| `RATE_CAP` | `10.0` | allows faster steady-state | caps throughput tighter |
| `RATE_FLOOR` | `0.2` | never throttles below higher min | can stall longer on persistent 429s |
| `RATE_INCREASE` | `0.2` | recovers faster after back-off | gentler ramp, safer for strict hosts |
| `RATE_DECREASE_FACTOR` | `0.5` | backs off less (aggressive) | backs off more (gentler, e.g. 0.3) |

**Recipes:**
- **Polite single-host crawl**: `RATE_CAP=1.0 RATE_FLOOR=0.1 RATE_INCREASE=0.05`
- **Aggressive batch (many backends)**: `RATE_CAP=20.0 RATE_INCREASE=0.5`
- **Strict anti-ban (e.g. Google)**: `RATE_DECREASE_FACTOR=0.3 RATE_FLOOR=0.05`

Backoff is separate (per-retry sleep, not rate): `BACKOFF_BASE` ×
`2^attempt`, capped at `BACKOFF_CAP`, jittered ±`BACKOFF_JITTER`. Default
sequence ≈ 1.5s → 3s → 6s → 12s → 24s (+30% jitter), max 30s.

### Cache
| Var | Default | Purpose |
|-----|---------|---------|
| `WEB_SEARCH_NO_CACHE` | `0` | set `1` to disable |
| `WEB_SEARCH_CACHE_TTL` | `86400` | positive cache TTL (s) |
| `WEB_SEARCH_NEG_CACHE_TTL` | `1800` | negative cache TTL (s) |
| `WEB_SEARCH_CACHE_DIR` | `~/.cache/web-search` | cache location |

### Sort + extract
| Var | Default | Purpose |
|-----|---------|---------|
| `WEB_SEARCH_EXTRACT_CHARS` | `0` | max chars per page (0=unlimited) |
| `WEB_SEARCH_AGENTIC_MAX_ITER` | `8` | agentic max iterations |
| `WEB_SEARCH_CONCURRENCY` | `3` | batch max parallel |
| `WEB_SEARCH_AGENTIC_MAX_RESULTS_PER_ITER` | `15` | max results fetched per agentic iteration |
| `WEB_SEARCH_EMIT_CITATIONS` | `1` | set `0`: drop citations[] (duplicates results fields) for lean agent payloads |
| `WEB_SEARCH_FLOOR_LINK_RATIO` | `0.5` | content floor: reject pages >50% markdown link/image scaffolding |
| `WEB_SEARCH_NOISE_PENALTY` | `0.3` | slot-score multiplier for flagged stats/discovery results |
| `WEB_SEARCH_EXTRA_NONCONTENT_PATTERNS` | *(empty)* | comma-separated extra regex fragments treated as discovery/navigation URLs |
| `WEB_SEARCH_PERSIST_STATE` | `1` | set `0`: don't save/restore AIMD rate + circuit cooldowns between runs |
| `WEB_SEARCH_STATE_TTL_S` | `1800` | persisted runtime state older than this is ignored |
| `WEB_SEARCH_AGENTIC_MAX_PER_DOMAIN` | `3` | max results per domain in agentic dedup |
| `WEB_SEARCH_AGENTIC_RELEVANCE_DOMAINS` | `5` | min domains for relevance diversification |
| `WEB_SEARCH_AGENTIC_TOP_N_EXTRACT` | `12` | top-N pages to extract in agentic |
| `WEB_SEARCH_AGENTIC_EXTRACT_CHARS` | `0` | max chars per agentic-extracted page (0=unlimited) |
| `WEB_SEARCH_AGENTIC_CONVERGENCE_NEW_MIN` | `2` | convergence stop: n_new threshold below which streak increments |
| `WEB_SEARCH_AGENTIC_CONVERGENCE_WINDOW` | `2` | convergence stop: consecutive low-yield iters required to stop |

### Quality infrastructure (#1-#7 — applies automatically)

| Feature | What it does | Where |
|---|---|---|
| **BM25 reranker** | IDF-weighted lexical rerank via `rank_bm25` (already installed, pure-Python, no model download). Adds `bm25_score` field per result. Used in extract selection. Replaces naive keyword-count. | `_bm25_rerank`, applied post-iter |
| **SimHash content-dedup** | 64-bit Charikar SimHash on title+body. Drops syndicated near-dupes (same AP wire story on reuters.com / moneycontrol.com / finance.yahoo.com — different URLs, ~95% identical text). Threshold 5 (snippet-tolerant). | `_dedup_by_content`, applied post-iter |
| **Query-language gate (F-59)** | Detects the query's script language; drops results in a *different* script language when ≥`WEB_SEARCH_LANG_FLOOR` same-language results survive (fail-open below floor). Kills the Yandex-fallback failure mode where backend rotation lands on yandex and injects ru/zh tutorials into English technical queries. Gate is symmetric (a Cyrillic query keeps ru, drops en) and fail-open. Logs `lang_gate_dropped` to stderr. | `_lang_gate` in `_normalize_results`, applied at the `_do_search` choke point (covers text/news/batch/agentic) |
| **Coverage-weighted relevance (F-60)** | `relevance_score` = matched-terms × (matched/total-terms) instead of raw overlap count. A result matching 3 scattered common terms of a 9-term query scores 1, not 3 — partial matches ("Unity garbage collection" for a cargo query) can no longer rival full matches. | `_score_relevance` |
| **Wikipedia resilience floor** | When EVERY search backend fails on a `text` query, the MediaWiki API (`en.wikipedia.org/w/api.php`, descriptive User-Agent) serves top-8 results as an emergency floor — payload marked `meta.emergency_backend: "wikipedia-api"`, never cached (must not mask the outage for TTL). Bridges total-backend-outage conditions. | `_wikipedia_api_search` → `_do_search` final fallback |
| **Recency-intent auto-timelimit** | Queries containing recency signals ("latest", "today", "breaking" → `d`; "new release", "recently" → `w`; current/last year dynamic → `m`) automatically get a matching engine timelimit when none was requested. Marked `meta.recency_intent` — the agent can see and override the inference. | `_detect_recency_intent` → `_do_search` |
| **JS-render retry (WS9 + WS9.1)** | Sub-threshold text (<200 chars — noscript stubs, "enable JavaScript" SPA shells) is never terminal: the cascade keeps escalating layers, then re-fetches a ≥20KB clean shell through headless Chromium exactly once (logged `js_render_retry`) before Wayback. Playwright output is terminal at any length (already rendered); a genuine stub is returned best-effort (`returning_short_text_best_effort`) only after every escalation fails. | `ExtractCascade.run` post-layer retry |
| **UA consistency (WS9 + Wave 6)** | `_CHROME_UA_MAJOR` is AUTO-DERIVED from the installed Playwright Chromium binary (mtime-keyed cache — spawn only on browser update; `WEB_SEARCH_CHROME_UA_MAJOR` pins it) and drives the curl_cffi `Sec-Ch-Ua` headers + Playwright UA. The impersonation rotation is introspected from the installed curl_cffi and picks the two closest-supported chrome profiles ≤ the advertised major (e.g. UA 151 → chrome146, chrome145), keeping Firefox/Safari for diversity. Fingerprint never drifts stale. | `_derive_chrome_ua_major`, `_impersonation_rotation` |
| **WebGL + canvas noise (Wave 6)** | Playwright stealth spoofs the WebGL debug-renderer pair (Intel / Mesa UHD 620 — headless SwiftShader is a free bot tell) and adds sparse random canvas farbling: `toDataURL` and `getImageData` return slightly different bytes on every read, defeating stable canvas-fingerprint hashes while leaving rendering visually identical. | `_STEALTH_JS` |
| **robots.txt awareness (Wave 5)** | Crawl start fetches `{origin}/robots.txt` (bounded 256KB): disallowed URLs are never fetched, declared `Crawl-delay` is topped up beyond the built-in human delay, and declared sitemaps feed the frontier. The start URL itself is a directed fetch and is never robots-gated. | `_fetch_robots_txt`, gate in `_crawl_site` enqueue loop |
| **Sitemap frontier seeding (Wave 5)** | Declared + conventional `sitemap.xml` locations (XML urlset/index **and text-format `sitemap.txt`**, gzip-aware, index recursion one level, per-URL SSRF-validated, ≤4 fetches, ≤`WEB_SEARCH_SITEMAP_MAX` URLs, 15s budget) are scored and pushed into the crawl frontier — reaches orphan pages no in-page link leads to. | `_fetch_sitemap_locs`, `_parse_locs` |
| **Anchor-text frontier scoring (Wave 5)** | Link priority = URL-slug score + up to 6 from the anchor text (`[read the RSI guide](...)` outranks bare navigation). Stem-matched against query terms, same algorithm both signals. | `_score_anchor_text`, `_extract_links_from_markdown` |
| **Challenge re-solve loop (Wave 5)** | After the initial settle, Playwright re-checks `_is_bot_blocked(html)` and re-solves up to `WEB_SEARCH_CHALLENGE_ATTEMPTS` times (content re-poll + full `page.reload` + networkidle settle) before giving up; logs `playwright_resolve_attempts` with the resolved verdict. | `_fetch_playwright` |
| **htmldate extraction** | SOTA date extraction via `htmldate` (was installed but unused — uses JSON-LD / Open Graph / meta tags / extensive patterns). Falls back to regex on plain-text snippets. Resists page-metadata spoofing. | `_extract_date` first layer |
| **Extended language detect** | Unicode-block coverage: Latin/CJK/Japanese kana/Hangul/Cyrillic/Devanagari/Arabic/Hebrew/Greek. Distinguishes Japanese (kana) from Chinese (Han only), Korean (Hangul), etc. Filters i18n spam from query-language-mismatched results. | `_detect_language` |
| **`--tier-budget T0:N,T1:N,T3:N`** | Per-tier extract caps forces source-type diversity. Default: no caps (relevance-first only). | `agentic` flag |
| **`--exclude-urls URL1,URL2`** | Feedback loop: agent marks useless URLs from prior payload, fresh URLs surface in subsequent iters. Also `@path/to/file` for bulk exclude (one URL per line, `#` comments). | `agentic` flag |

**Skipped (agent-layer concerns):** neural reranker (cross-encoder/mxbai — needs 200MB model, dep-gated), async crawl refactor (bottleneck is rate-limiter + human_delay, not HTTP latency — concurrent same-host fetches risk bot-detection; ThreadPoolExecutor already parallelizes cross-URL extracts).
| `WEB_SEARCH_EXTRACTOR` | `auto` | extractor cascade: `auto` \| `trafilatura` \| `crawl4ai` \| `builtin` |
| `WEB_SEARCH_CRAWL4AI_FIT` | `0` | set `1` to use crawl4ai BM25-pruned `fit_markdown` (noise removal) |

### LLM (pro / agentic / deep-search — auto by default, `--no-llm` to opt out)
| Var | Default | Purpose |
|-----|---------|---------|
| `WEB_SEARCH_LLM_MODEL` | `qwen3.5:9b` | generation model — no hardcoded model, override freely |
| `WEB_SEARCH_LLM_PROVIDER` | `auto` | `auto` \| `ollama` \| `openai` — pin provider, skip probing |
| `WEB_SEARCH_LLM_HOST` | `http://localhost:11434` | LLM REST base, any provider (localhost only — §7g egress guard); `/v1` suffix → OpenAI-compatible |
| `WEB_SEARCH_OLLAMA_HOST` | *(legacy alias)* | backward-compat alias for `WEB_SEARCH_LLM_HOST` |
| `WEB_SEARCH_EMBED_MODEL` | `nomic-embed-text` | embedding model for `--semantic-coverage` |
| `WEB_SEARCH_LLM_MAX_TOKENS` | `4096` | max tokens per LLM call |
| `WEB_SEARCH_LLM_TIMEOUT` | `120.0` | per-LLM-call timeout (s) |
| `WEB_SEARCH_LLM_THINK` | `1` | set `0` for fast mode (44x faster, no chain-of-thought). Default ON for deeper reasoning |

### DHT
| Var | Default | Purpose |
|-----|---------|---------|
| `WEB_SEARCH_DHT` | `0` | set `1` to enable DHT cache |
| `WEB_SEARCH_DHT_API_URL` | `http://localhost:4479` | daemon URL |

### Domain authority tiers (user-configured, NO hardcoded bias)
| Var | Default | Purpose |
|-----|---------|---------|
| `WEB_SEARCH_TIER0_DOMAINS` | (empty) | comma-separated authoritative domains for YOUR field (T0 = highest authority). Example: `nih.gov,pubmed.ncbi.nlm.nih.gov,who.int` for medical research. |
| `WEB_SEARCH_TIER1_DOMAINS` | (empty) | comma-separated reliable secondary sources (T1). Example: `reuters.com,nature.com,arxiv.org` |

**No hardcoded domain lists.** All domains are T3 (unclassified) by default — the
system treats reuters.com, wikipedia.org, and random-blog.example.com equally.
Structural TLD heuristics still apply: `.gov`/`.edu`/`.mil`/`.ac.` → T0
(any country). Users configure T0/T1 for their specific research domain.
Defaults are set for **deep research** — generous but still bounded to prevent OOM.

| Var | Default | Purpose |
|-----|---------|---------|
| `WEB_SEARCH_TRUSTED_HOSTS` | (empty) | comma-separated allowlist for legitimate internal hosts (e.g. `localhost,intranet.corp`). Metadata endpoints (169.254.169.254 etc.) ALWAYS blocked. |
| `WEB_SEARCH_MAX_RESPONSE_BYTES` | `200000000` | max bytes per HTTP response (200 MB) — guards against decompression bombs |
| `WEB_SEARCH_MAX_HTML_BYTES` | `100000000` | max HTML body cap (100 MB) |
| `WEB_SEARCH_MAX_MARKDOWN_BYTES` | `50000000` | max extracted markdown (50 MB) |
| `WEB_SEARCH_MAX_DOCUMENT_CHARS` | `20000000` | max chars post-extract (20M) |
| `WEB_SEARCH_MAX_CRAWL_BYTES` | `2000000000` | total byte budget per crawl (2 GB) |
| `WEB_SEARCH_MAX_REDIRECTS` | `10` | redirect hops (each re-validated for SSRF) |
| `WEB_SEARCH_MAX_LINKS_PER_PAGE` | `500` | links extracted per crawl page |
| `WEB_SEARCH_MAX_TOTAL_LINKS` | `10000` | crawl frontier cap |
| `WEB_SEARCH_MAX_BROWSER_TIME_S` | `60.0` | Playwright navigation deadline (seconds) |
| `WEB_SEARCH_CALL_BUDGET` | `600.0` | overall time budget per call (10 minutes) |

SSRF protection validates the resolved IP of EVERY fetch URL AND every redirect
hop (DNS-rebinding guard: `attacker.com → 302 → 169.254.169.254` is caught
post-redirect). Forbidden ranges: RFC1918 private, loopback, link-local (includes
ALL cloud metadata endpoints), CGNAT (includes Alibaba 100.100.100.200), ULA,
TEST-NETs. Policy errors surface as structured JSON: `{"error": "SSRFBlocked",
"reason": "...", "resolved_ips": [...], "hint": "..."}`.

### Semantic reranker (P2.5 — opt-in)
| Var | Default | Purpose |
|-----|---------|---------|
| `WEB_SEARCH_SEMANTIC_RERANK` | `0` | set `1` to enable hybrid BM25+bi-encoder reranking (fastembed is a hard dep, always installed) |
| `WEB_SEARCH_SEMANTIC_WEIGHT_BM25` | `0.4` | BM25 blend weight (normalized with SEM) |
| `WEB_SEARCH_SEMANTIC_WEIGHT_SEM` | `0.6` | semantic cosine blend weight (normalized with BM25) |

When ON, results get `semantic_score` (0-1 normalized cosine) and `hybrid_score`
fields; the extract-selection sort prefers `hybrid_score`. When OFF (default),
zero overhead — pass-through to BM25. `fastembed` is a hard dep (67MB
BGE-small-en-v1.5 ONNX model, auto-downloaded to `~/.cache/fastembed/` on first
use, shared cache with other fastembed users).

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success with results (may be cached/stale/partial; `meta` fields say which) |
| `1` | Error: invalid args, unrecoverable SearchError (SSRF/budget/scheme), crawl/adaptive `error` payload, batch query failure, Ollama unavailable, agentic terminal failure with zero results |
| `2` | Missing hard dependency — install hint printed |
| `3` | Success-shaped but empty: no results AND no cache (text/news); binary-content routing signal (extract) |
| `130` | SIGINT — structured `{"error": "interrupted"}` JSON emitted, then exit |

## Key flags reference

| Flag | Subcommands | Effect |
|------|-------------|--------|
| `--extract N` | text, news | Fetch full content for top-N results (default 3) |
| `--summarize` | agentic, deep-search | Append an LLM-synthesized cited answer (pro's engine) to the payload; fail-fast if no local LLM; excludes `--no-llm` |
| `--extract 0` | text, news | Disable extraction for fast lookup |
| `--sort latest` | text, news, agentic, batch | Sort by date (default) |
| `--sort authority` | same | Sort by domain tier (T0 first) |
| `--sort relevance` | same | Sort by keyword overlap |
| `--max-pages N` | crawl | Max pages to extract (default 10) |
| `--max-depth N` | crawl | Max BFS depth from start URL (0=unlimited, default 0). 1 = start + direct children. Caps crawl horizon independently of `--max-pages` |
| *(removed)* | all search | DHT via env `WEB_SEARCH_DHT=1` (flag pruned F-57; also `--cache-ttl/--neg-cache-ttl/--call-budget` pruned to env/opts-in) |
| `--no-cache` | all search | Bypass local cache |
| `--proxy URL` | all search | Route through proxy |
| `--call-budget S` | all search | Per-call total time budget |
| `--timelimit d/w/m/y` | text, news | Time filter (day/week/month/year) |

## Notes

- **Never** parse stderr as the result — it's logs. stdout is always JSON.
- **Zero truncation** — full page content always returned, no matter how large.
- **Bot-block bypass** is automatic: ddgs → curl_cffi → Playwright (lazy, only
  when needed). Install playwright for Cloudflare JS challenge sites.
- **Cookie persistence** — cf_clearance cookies cached per-domain in-process.
  Solve challenge once, reuse for all pages in the same crawl.
- URL canonicalization strips tracking params (utm_*, fbclid, gclid, ocid).
- Proxy credentials always redacted in logs.
- DHT daemon (env WEB_SEARCH_DHT=1) spawns a detached process. Stop with `pkill -f "ddgs api"`.

## Self-improvement loop

This skill is designed to be self-improving. During use, if the AI agent
detects any issues, it should act based on severity:

**Minor issues** (broken regex, missed tracking params, new bot-block
indicators, stale domain-tier entries, link-extraction edge cases, small
logic bugs): the agent **should fix the skill itself directly** in
`web_search.py` (this skill's directory), then re-run
`--self-check` to verify. **User approval required** before applying any
edit (per AGENTS.md §2 Approval Gate).
**Major failures or architectural issues** (e.g. a bypass layer
fundamentally broken, output schema regression, rate-limit defense
failure, new Cloudflare detection mechanism requiring a new approach,
performance degradation, security vulnerability): the agent **must STOP,
report the issue with full diagnosis** — what broke, why, blast radius,
evidence (logs/output), and a proposed architectural fix or improvement
with trade-offs. **Do not attempt a patchwork fix** (AGENTS.md §5 H8).
Wait for user decision before proceeding.

## Single source

This skill is standalone and location-independent: `web_search.py`
derives its venv from its own directory (`__file__`-relative) and caches
under `$HOME/.cache/web-search` — move the folder anywhere and it works.
Keep exactly ONE copy. The former mirror at
`~/.config/opencode/skills/web-search/` was a duplicated directory that
drifted silently and was removed 2026-08-21; do not re-create a copy.
The only sanctioned cross-runtime entry is a SYMLINK
(`~/.config/opencode/skills/web-search` → this directory), which cannot
drift because it has no content of its own.
