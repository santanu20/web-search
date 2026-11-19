# Research quality and citation diagnostics

The \`pro\` command is a bounded multi-step research workflow. It is designed to
improve coverage and evidence handling; it is not a guarantee of correctness
or parity with any hosted research product.

## Research loop

1. Build a query plan with distinct facets. A local LLM may decompose the
   question; otherwise a deterministic question-type planner adds angles for
   comparisons, mechanisms, policy questions, evidence, counterarguments and
   limitations.
2. Search the facets and extract full pages.
3. Measure facet coverage. A facet is covered only when significant query
   terms occur in content from at least two distinct hosts.
4. Search for missing or weakly corroborated facets until the configured
   coverage threshold, convergence condition, round limit or time budget is
   reached.
5. Select a capped set of relevant sources, limit per-host concentration and
   reduce redundant evidence before assigning citation numbers.
6. Synthesize with explicit instructions to treat web-page content as
   untrusted data, preserve material disagreement and cite factual claims.
7. Report citation and coverage diagnostics alongside the answer.

## Output diagnostics

| Field | Meaning | Does not establish |
|---|---|---|
| \`research_facets\` | Planned retrieval angles | That every angle was fully researched |
| \`facet_coverage\` | Fraction of facets with lexical matches on at least two hosts | Semantic completeness or source independence in the scholarly sense |
| \`facet_coverage_detail\` | Matching domains, URLs and terms per facet | That those sources agree or are correct |
| \`citation_audit.n_claims\` | Sentence-like answer units audited | A perfect claim segmentation |
| \`citation_audit.n_uncited_claims\` | Audited statements without a numeric citation | That every uncited statement is necessarily factual |
| \`citation_audit.n_invalid_citations\` | Citation indices outside the supplied source list | That in-range citations support their claims |
| \`citation_audit.citation_coverage\` | Fraction of audited statements with at least one valid citation index | Semantic entailment |
| \`citation_audit.claims[].lexical_overlap\` | Share of significant claim terms also found in the cited source text | Whether the cited passage entails the claim |
| \`citation_grounding\` | Whether a model-supplied quotation can be found in the cited extracted page | Whether the claim around that quotation is true |

**Important:** citation presence, lexical overlap and quote grounding are
separate diagnostics. None is a substitute for claim-level entailment review,
source-authority assessment or human judgment. The code intentionally labels
lexical overlap as a diagnostic, not as a factual-support probability.

## Source selection

Source selection uses query-term relevance, repository-configured domain tiers,
host caps and lexical novelty. T0/T1 tiers are user-configurable through
\`WEB_SEARCH_TIER0_DOMAINS\` and \`WEB_SEARCH_TIER1_DOMAINS\`; the existing
structural \`.gov\`, \`.edu\`, \`.mil\` and \`.ac.\` heuristics remain. A domain
tier is a ranking hint, not an endorsement of every page on that host.

Crawl seeds are ranked by query relevance, tier and facet breadth rather than
raw search-result count. Search engines and known non-content hosts remain
excluded by the existing crawl blocklist.

## Security and time boundaries

Web-page text is treated as untrusted source material in LLM synthesis prompts.
The configured call budget now reserves up to 20% for crawling when auto-crawl
is enabled; research rounds stop before that reserve and crawling stops at the
overall deadline. LLM provider request timeouts remain independently
configurable. A call budget is an orchestration budget, not a guarantee that
every external library can be interrupted instantly.

## Evaluation

The answer-quality harness reports facet coverage, claim citation coverage,
uncited and invalid citation counts, source-domain count, and P50/P95 latency
in addition to the existing lexical answer coverage and quote-grounding rates.
These remain evaluation signals. Use a fixed, independently labeled question
set to compare releases; live web results are variable and benchmark records
are not ground truth.
