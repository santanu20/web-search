# web-search

[![CI](https://github.com/santanu20/web-search/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/santanu20/web-search/actions/workflows/ci.yml)

Metasearch CLI for research-grade web Q&A: 8+ search backends (DuckDuckGo, Brave,
Google, Yandex, Yahoo, Wikipedia, Mojeek, Startpage) behind one interface, with
bot-block bypass, full-page extraction, and optional local-LLM synthesis
(any OpenAI-compatible or Ollama endpoint).

## Modes

| Command | What it does |
|---|---|
| `text` | Web search with cleaned page extraction |
| `news` | News search |
| `batch` | Many queries, one JSON array out |
| `agentic` | Multi-query crawl + convergence |
| `deep-search` | Multi-round research loop |
| `pro` | Deep research + LLM synthesis with inline `[n]` citations and quote grounding |

Every mode prints exactly one JSON document to stdout (logs go to stderr), so the
output pipes cleanly into other tools. Exit codes: `0` ok, `1` error, `3` empty.

## Quickstart

```bash
uv venv .venv --python 3.13
.venv/bin/python web_search.py text "docker vs podman"
```

If a dependency is missing the CLI prints the exact install line for your venv
(`uv pip install --python .venv/bin/python ...`). Nothing is installed silently.

Optional LLM synthesis (no API key, fully local):

```bash
ollama pull qwen3.5:9b
.venv/bin/python web_search.py pro "why did the challenger shuttle fail" --use-llm
```

Model and behavior are env-configurable (`WEB_SEARCH_LLM_MODEL`,
`WEB_SEARCH_LLM_LIGHT_MODEL`, `WEB_SEARCH_EVIDENCE_MODE`, ...). See `SKILL.md`
for the full environment table.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Answer-quality harness (live network, reporter not hard asserts):

```bash
.venv/bin/python tests/eval_answers.py --json
```
