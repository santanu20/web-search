# Pro research task graph

Every Pro response includes a research_plan object with schema
research.plan.v1.

## Plan structure

- A deterministic plan ID derived from the normalized research objective.
- An objective task.
- One evidence-retrieval task per unique decomposed facet.
- An optional source-deepening task when crawling is enabled.
- A synthesis task depending on the facet and source-deepening tasks.
- Optional claim-verification, counter-evidence and semantic-coverage tasks.

Every task exposes a stable task ID and type, question/facet, dependencies,
expected outputs, acceptance criteria, a bounded budget allocation, and an
observed status/result.

## Execution states

| State | Meaning |
|---|---|
| complete | The stage ran and its acceptance signals were observed |
| partial | Some evidence exists, but coverage/corroboration remains incomplete |
| blocked | Evidence was absent or execution/budget conditions prevented completion |
| planned | The stage was not observed to run |

Task completion is derived from coverage_matrix, crawl metadata, synthesis
output, claim audit and counter-evidence outputs. The plan is not a claim that
all research dimensions were identified or that factual correctness is proven.

## Budget allocation

The plan reserves up to 55% for facet retrieval, 15% for source deepening when
enabled, 10% for synthesis, 10% for claim verification when requested, and 10%
for counter-evidence when requested. Allocations do not exceed the supplied
call budget. Semantic coverage is currently accounted as a diagnostic stage
inside the evidence workflow rather than given a separate time allocation.

## Persistence

When --project ID is set, the complete task graph and its observed task
statuses are stored with the research run and can be retrieved through the
projects command.

## Limitations

- Tasks currently describe and annotate the existing Pro pipeline; this PR
  does not turn each node into an independently resumable distributed job.
- Facet completeness depends on query decomposition and the coverage matrix's
  lexical matching.
- A complete retrieval task means evidence coverage thresholds were met, not
  that the evidence proves the conclusion.
- A future durable orchestrator can execute/retry individual task nodes and
  resume from checkpoints using this graph as its contract.
