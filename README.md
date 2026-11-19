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
| Evidence windows | The LLM reads query-matched passages, not just the first 4,000 characters of each page |
| Agentic research loop | Facet planning → multi-round retrieval → independent-domain coverage → targeted gap searches |
| Diverse evidence | Sources ranked for relevance, configured authority tier, host diversity and lexical novelty |
| Citation diagnostics | Quote grounding plus claim-level citation presence, valid-index and lexical-overlap checks |
| Claim-level audit (opt-in) | `--verify-claims` assesses each cited factual claim and rejects evidence quotes absent from the cited source snapshot |
| Smart model routing | A smaller model can handle planning and gap analysis; the main model synthesizes |
| Keyless | No API keys for search. Local models are optional. |

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
query ─> research facets ─> search 8+ backends ─> extract pages
      ─> score evidence ─> independent-domain facet coverage
      ─> targeted gap searches ─> diverse source selection
      ─> synthesis (local Ollama / OpenAI-compatible endpoint)
      ─> quote grounding + citation audit ─> optional claim audit ─> one JSON research report
```

The `pro` payload exposes the planned facets, per-facet independent-domain
coverage, selected sources, quote-grounding records and a claim-citation audit.
The audit reports uncited claims, invalid citation indices and lexical overlap
between each claim and its cited source. **Lexical overlap and quote grounding
are diagnostics, not proof of semantic entailment or factual correctness.**
See [Research quality and citation diagnostics](docs/RESEARCH_QUALITY.md) for
field definitions and limitations.

Historical benchmark records remain in `bench/`; live search results and
provider latency vary, so those records are not current performance guarantees.

Optional claim-level audit (one additional model call):

```bash
.venv/bin/python web_search.py pro "how do ETFs create and redeem shares" --use-llm --verify-claims
```

The JSON response includes `claim_audit[]` and `claim_audit_summary`. The
audit checks whether each sentence-sized claim is supported by a verbatim span
in a source cited by that claim. It is model-assisted entailment assessment,
not formal proof or calibrated factual confidence; see
[`docs/CLAIM_EVIDENCE_AUDIT.md`](docs/CLAIM_EVIDENCE_AUDIT.md).

Optional claim-level verification and counter-evidence discovery:

```bash
.venv/bin/python web_search.py pro "how do ETFs create and redeem shares" --use-llm --counter-evidence
```

`--counter-evidence` implies `--verify-claims`. It performs up to eight targeted
searches for contrary evidence on hosts not already used in the investigation.
The verifier distinguishes original citations from counter-evidence and checks
quotes against captured pages. A different host is not proof of organizational
independence. See [the claim evidence audit guide](docs/CLAIM_EVIDENCE_AUDIT.md).

## Persistent research projects

Save Pro runs into a local SQLite project workspace:

```bash
.venv/bin/python web_search.py pro "Indian EV battery market" --use-llm --counter-evidence --project indian-ev-battery
.venv/bin/python web_search.py projects
.venv/bin/python web_search.py projects --project-id indian-ev-battery --runs 10
```

Run IDs can be retrieved with `projects --project-id ID --run-id RUN_ID`.
Use `--include-content` to include captured source snapshots. Delete a
project and all of its stored runs/evidence with
`projects --project-id ID --delete --yes`.

The store defaults to `~/.cache/web-search/research.db`; override it with
`--research-db PATH` or `WEB_SEARCH_RESEARCH_DB`. It stores source snapshots
(up to 100,000 characters per source, 100 sources per run), claims, accepted
evidence spans, synthesis summaries and run-to-run diffs. Claim status changes
are compared only when the normalized query is unchanged. This is an optional
single-user local store, not a multi-tenant or encrypted enterprise database.

## Configuration

Model, egress and security behavior are env-configurable: `WEB_SEARCH_LLM_MODEL`,
`WEB_SEARCH_LLM_EGRESS_POLICY` (`warn`, `local_only`, `allowlist`),
`WEB_SEARCH_LLM_ALLOWED_HOSTS`, and the two-part trusted-private-host opt-in
(`WEB_SEARCH_TRUSTED_HOSTS` + `WEB_SEARCH_TRUSTED_HOSTS_ALLOW_PRIVATE=1)).
Full settings table in `SKILL.md`.

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
