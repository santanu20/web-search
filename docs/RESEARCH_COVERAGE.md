# Research coverage matrix

The `pro` response includes:

- `research_coverage`: counts of covered, partial and gap facets.
- `coverage_matrix[]`: one evidence-coverage row per decomposed research facet.

## Evaluation

A facet is evaluated against the extracted pages using tokenized lexical
matching. The system records matched and missing terms, evidence-page count,
publisher count, evidence-origin cluster count, primary-source candidate count,
and a short excerpt from each selected page.

A facet is `covered` only when:
1. the union of matched facet terms reaches the configured term-coverage
   threshold (default 0.5); and
2. at least two independent-origin clusters are represented (default 2).

A facet with some relevant evidence but not both conditions is `partial`.
A facet with no matching pages is `gap`.

Same-publisher sources and detected exact/near-duplicate syndicated pages
inherit a shared independence cluster from `source_intelligence`. Unprofiled
pages may be shown as candidate evidence but cannot satisfy the minimum
independent-origin requirement.

## Gap handling

For partial and gap facets, the matrix provides a `recommended_query` that
combines the original research question, the facet, and an explicit request for
primary/independent evidence. These are recommendations for the next run; the
current `pro` workflow does not automatically spend additional search budget
on them after synthesis.

## Limitations

- Facets come from query decomposition; a poor decomposition can omit an
  important research dimension.
- Matching is lexical. Semantic similarity and entailment are not measured by
  this matrix.
- Two evidence-origin clusters are not proof of publisher or institutional
  independence; ownership and source lineage are heuristic.
- The matrix measures evidence presence and diversity, not factual correctness,
  truth, completeness, or calibrated confidence.
- A page may mention the facet without supporting a proposition. Claim-level
  verification remains a separate audit step.

Use the matrix to identify weakly researched dimensions and to plan follow-up
searches, not as a standalone quality verdict.
