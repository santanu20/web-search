# web-search

[![CI](https://github.com/santanu20/web-search/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/santanu20/web-search/actions/workflows/ci.yml)
[![License: MIT](https://github.com/santanu20/web-search/blob/main/LICENSE)](https://github.com/santanu20/web-search/blob/main/LICENSE)

Research-grade web search from the command line. One query fans out across
8+ search backends, pages are extracted and cleaned, and an optional local LLM
turns them into a cited answer — every quote checked against the page it came
from. Works fully offline from any API provider: no keys, no accounts.

Two ways to use it:

- **As a CLI** — pipe JSON into anything.
- **As an agent skill** — clone it into your AI agent's skills directory and the
  agent gets the whole mode catalog, config table, and output contract via
  `SKILL.md`.

## What it does

| Capability | Detail |
|---|---|
| Metasearch | DuckDuckGo, Brave, Google, Yandex, Yahoo, Wikipedia, Mojeek, Startpage behind one interface |
| Bot-block bypass | Browser-impersonating fetch layer keeps results flowing where plain HTTP gets blocked |
| Full-page reading | trafilatura / readability / html2text extraction, not link lists |
| LLM answers (optional) | Local Ollama or any OpenAI-compatible endpoint; `--use-llm` |
| Evidence windows | The LLM reads the passages that match your query, not the first 4,000 chars of each page |
| Quote-grounded citations | Every quote in the answer is verified against page content; inline `[n]` markers resolve to real sources |
| Smart model routing | A small model handles query decomposition / reranking; the big model only synthesizes — CPU-friendly |
| Keyless | No API keys for any of it. Local models optional. |

Agent-friendly output contract: every mode prints exactly **one JSON document**
to stdout (logs go to stderr). Exit codes: `0` ok, `1` error, `3` empty result.

## Who it's for

Anyone who wants sourced answers instead of ten blue links: researchers,
students, writers, journalists, and AI agents doing deep research. Runs on any
OS with Python (tested on 3.13, Linux); heavier LLM modes are happiest with a
few CPU cores or a GPU-backed Ollama.

## Install and run as a CLI

```bash
git clone https://github.com/santanu20/web-search
cd web-search
uv venv .venv --python 3.13
.venv/bin/python web_search.py text "docker vs podman"
```

If a dependency is missing the CLI prints the exact install line for your venv.
Nothing is installed silently.

## Install as an agent skill

```bash
# opencode
git clone https://github.com/santanu20/web-search ~/.config/opencode/skills/web-search
uv venv ~/.config/opencode/skills/web-search/.venv --python 3.13

# any other agent (Claude Code, etc.)
# clone into that agent's skills directory the same way
```

`SKILL.md` is the agent-facing surface: mode catalog, flags, environment
variables, and the one-JSON-doc output contract. The agent shells out to the
same CLI shown above — there is no second API to learn.

## Modes

| Command | What it does |
|---|---|
| `text` | Web search with cleaned page extraction |
| `news` | News search |
| `batch` | Many queries, one JSON array out |
| `agentic` | Multi-query crawl + convergence |
| `deep-search` | Multi-round research loop |
| `pro` | Deep research + LLM synthesis with inline `[n]` citations and quote grounding |

## How `pro` works

```
query ─> decompose (small model) ─> search 8+ backends ─> extract pages
      ─> rank ─> evidence windows (query-matched passages)
      ─> synthesis (main model, Ollama or OpenAI-compatible)
      ─> quote grounding ─> one JSON doc (synthesis + citations + coverage)
```

Measured on this exact flow (2026-10-07, live network, qwen3.5:9b on 24-core
CPU, 9 runs across 3 questions): **90% ± 14 of quoted citations grounded** in
source pages, ~20-30 sources per run, ~6-7 min per question with thinking on.
Passage-window evidence beat plain head-truncation in a controlled A/B (higher
grounded rate, tighter spread, no latency cost).

## Configuration

Model and behavior are env-configurable: `WEB_SEARCH_LLM_MODEL`,
`WEB_SEARCH_LLM_LIGHT_MODEL`, `WEB_SEARCH_EVIDENCE_MODE` (`head` restores
legacy first-4,000-chars evidence), provider host/port. Full table in
`SKILL.md`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Answer-quality harness (live network, reporter not hard asserts):

```bash
.venv/bin/python tests/eval_answers.py --json            # 3 golden questions
.venv/bin/python tests/eval_answers.py --seeds 3 --json  # multi-seed + bench/eval_latency.jsonl
```

## Related

- [scientific-research](https://github.com/santanu20/scientific-research) —
  academic pipeline that uses this tool to append a web-synthesis section to
  its research briefs.

## License

MIT
