# Source lineage and independence signals

Pro research returns three additional fields:

- `source_intelligence`: aggregate counts for source volume, extracted pages,
  publisher domains, independence clusters, primary-source candidates and
  duplicate pairs.
- `source_profiles[]`: per-source identity, publisher-domain key, inferred
  source type, primary-source candidate flag, explainable signals, content
  snapshot hash and independence cluster.
- `source_duplicate_pairs[]`: exact-hash or near-duplicate pairs with a
  similarity value and the detection method.

## How grouping works

1. Normalize each URL and derive a deterministic source ID.
2. Derive a best-effort registrable publisher domain using a conservative set of
   common compound public suffixes.
3. Group pages from the same publisher domain.
4. Compare bounded 5-token shingles across different publishers. Near-duplicate
   pages at or above the configurable in-code threshold are joined into the
   same evidence-origin cluster.
5. Derive stable cluster IDs from the sorted source IDs.

The pairwise content comparison is bounded to 50 sources by default. It uses
exact content hashes first and Jaccard similarity of token shingles for likely
syndication or copied articles.

## Source-type and primary-source signals

Source types include government publications, financial filings/reports,
press releases, scholarly work, literature reviews, technical documentation,
news reporting, opinion, blogs, corporate pages and generic web pages.

The `primary_source_candidate` field is based on explicit signals such as an
official government/regulator host, an official investor/filing path, a press
release label, or scholarly methods/results sections. Each profile includes
the underlying `source_signals[]` so consumers can inspect the reason.

## Important limitations

- This is source triage and lineage evidence, not a source credibility or
  factual-confidence score.
- The publisher domain uses a small built-in suffix list, not the full Public
  Suffix List. Unknown suffixes are conservatively host-scoped.
- A different publisher domain does not prove institutional independence.
  Subsidiaries, common ownership, wire services, shared data vendors and
  upstream citations are not resolved yet.
- Near-duplicate detection is lexical and can miss paraphrased or translated
  syndication and can occasionally join boilerplate-heavy pages.
- Primary-source classification is heuristic. A report-looking page can still
  be secondary commentary; inspect the signals and actual source.

The purpose is to avoid treating multiple mirrors or same-publisher pages as
independent corroboration, while making the classification reasons visible.
