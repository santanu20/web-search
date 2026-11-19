"""Explicit, inspectable research plan and task graph for Pro runs.

The plan is deterministic for a normalized query and set of facets. Task
completion is derived from evidence and stage outputs; a planned task is never
reported as completed merely because it was included in the plan.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

PLAN_SCHEMA = "research.plan.v1"


def _stable_id(prefix: str, key: str) -> str:
    return f"{prefix}_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"


def build_research_plan(
    query: str,
    facets: Iterable[str],
    *,
    max_rounds: int,
    call_budget_s: float,
    top_n_per_facet: int,
    verify_claims: bool = False,
    counter_evidence: bool = False,
    semantic_coverage: bool = False,
    auto_crawl: bool = True,
    expected_independent_origins: int = 2,
) -> dict[str, Any]:
    """Create a deterministic graph describing the investigation to execute."""
    query = str(query or "").strip()
    normalized_query = " ".join(query.casefold().split())
    plan_id = _stable_id("plan", normalized_query)
    objective_id = _stable_id("task", normalized_query + "|objective")
    facets_clean: list[str] = []
    seen: set[str] = set()
    for raw in facets:
        value = " ".join(str(raw or "").split()).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            facets_clean.append(value)
    if not facets_clean:
        facets_clean = [query] if query else ["Research objective"]

    facet_budget = max(0.0, float(call_budget_s) * 0.55 / max(1, len(facets_clean)))
    crawl_budget = max(0.0, float(call_budget_s) * (0.15 if auto_crawl else 0.0))
    verification_budget = max(0.0, float(call_budget_s) * (0.10 if verify_claims else 0.0))
    counter_budget = max(0.0, float(call_budget_s) * (0.10 if counter_evidence else 0.0))
    tasks: list[dict[str, Any]] = [{
        "task_id": objective_id,
        "kind": "objective",
        "title": "Investigate the research objective",
        "question": query,
        "dependencies": [],
        "status": "complete",
        "expected_outputs": ["research facets", "evidence requirements"],
        "acceptance_criteria": ["The research objective is recorded."],
        "budget_seconds": 0.0,
        "result": {"query": query, "facet_count": len(facets_clean)},
    }]

    facet_task_ids: list[str] = []
    for index, facet in enumerate(facets_clean, start=1):
        task_id = _stable_id("facet", normalized_query + "|" + facet.casefold())
        facet_task_ids.append(task_id)
        tasks.append({
            "task_id": task_id,
            "kind": "evidence_retrieval",
            "title": f"Investigate facet {index}: {facet[:100]}",
            "question": facet,
            "facet": facet,
            "dependencies": [objective_id],
            "status": "planned",
            "expected_outputs": [
                "relevant full-text source passages",
                "at least two distinct evidence-origin clusters",
                "primary-source candidates where available",
                "contrary or qualifying evidence",
            ],
            "acceptance_criteria": [
                "At least 50% of facet terms appear in extracted evidence.",
                f"At least {max(1, int(expected_independent_origins))} distinct evidence-origin clusters are present.",
                "Important claims are linked to source passages.",
            ],
            "budget_seconds": round(facet_budget, 2),
            "max_results_to_extract": max(0, int(top_n_per_facet)),
            "max_rounds": max(1, int(max_rounds)),
            "result": None,
        })

    crawl_id = _stable_id("task", normalized_query + "|site-crawl")
    crawl_id = _stable_id("task", normalized_query + "|site-crawl")
    if auto_crawl:
        tasks.append({
        "task_id": crawl_id,
        "kind": "source_deepening",
        "title": "Deepen evidence from high-value source domains",
        "question": query,
        "dependencies": facet_task_ids,
        "status": "planned",
        "expected_outputs": ["additional full-text pages", "site-local context"],
        "acceptance_criteria": ["Additional pages are within crawl and byte budgets."],
        "budget_seconds": round(crawl_budget, 2),
        "result": None,
    })

    synthesis_id = _stable_id("task", normalized_query + "|synthesis")
    tasks.append({
        "task_id": synthesis_id,
        "kind": "synthesis",
        "title": "Synthesize the evidence into a cited report",
        "question": query,
        "dependencies": [*facet_task_ids, *([crawl_id] if auto_crawl else [])],
        "status": "planned",
        "expected_outputs": ["cited findings", "limitations", "unresolved gaps"],
        "acceptance_criteria": ["Citations resolve to captured source records."],
        "budget_seconds": round(max(0.0, float(call_budget_s) * 0.10), 2),
        "result": None,
    })

    claim_id = _stable_id("task", normalized_query + "|claim-audit")
    if verify_claims or counter_evidence:
        tasks.append({
            "task_id": claim_id,
            "kind": "claim_verification",
            "title": "Audit material claims against cited evidence",
            "question": query,
            "dependencies": [synthesis_id],
            "status": "planned",
            "expected_outputs": ["claim statuses", "verbatim evidence spans"],
            "acceptance_criteria": [
                "No claim is labelled supported solely from an LLM assertion.",
                "Accepted evidence spans occur in captured source snapshots.",
            ],
            "budget_seconds": round(verification_budget, 2),
            "result": None,
        })

    if counter_evidence:
        counter_id = _stable_id("task", normalized_query + "|counter-evidence")
        tasks.append({
            "task_id": counter_id,
            "kind": "counter_evidence",
            "title": "Search independently for evidence that challenges material claims",
            "question": query,
            "dependencies": [synthesis_id],
            "status": "planned",
            "expected_outputs": ["contrary evidence from new publisher hosts"],
            "acceptance_criteria": [
                "Counter-evidence is attached to a specific claim.",
                "Source quotes are validated against captured source snapshots.",
            ],
            "budget_seconds": round(counter_budget, 2),
            "result": None,
        })

    if semantic_coverage:
        semantic_id = _stable_id("task", normalized_query + "|semantic-coverage")
        tasks.append({
            "task_id": semantic_id,
            "kind": "semantic_coverage",
            "title": "Measure paraphrase-aware evidence coverage",
            "question": query,
            "dependencies": facet_task_ids,
            "status": "planned",
            "expected_outputs": ["semantic coverage diagnostics"],
            "acceptance_criteria": ["Embedding-based coverage ran successfully."],
            "budget_seconds": 0.0,
            "result": None,
        })

    return {
        "schema": PLAN_SCHEMA,
        "plan_id": plan_id,
        "objective": query,
        "status": "planned",
        "task_count": len(tasks),
        "budget_seconds": round(max(0.0, float(call_budget_s)), 2),
        "tasks": tasks,
        "execution_order": [task["task_id"] for task in tasks],
        "notes": [
            "Task states describe observed execution results, not intended work.",
            "Evidence-origin counts are heuristics and do not prove institutional independence.",
        ],
    }


def annotate_research_plan(
    plan: dict[str, Any],
    *,
    coverage_matrix: Iterable[dict[str, Any]] = (),
    rounds: Iterable[dict[str, Any]] = (),
    crawl_meta: Iterable[dict[str, Any]] = (),
    claim_audit_summary: dict[str, Any] | None = None,
    counter_evidence: dict[str, Any] | None = None,
    semantic_coverage: float | None = None,
    synthesis: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    """Apply observed stage outcomes to the planned task graph."""
    result = {**plan, "tasks": [dict(task) for task in plan.get("tasks", [])]}
    tasks = result["tasks"]
    facets_by_key = {
        " ".join(str(row.get("facet") or "").casefold().split()): row
        for row in coverage_matrix
    }
    round_rows = list(rounds)
    crawl_rows = list(crawl_meta)
    claim_summary = claim_audit_summary or {}
    counter = counter_evidence or {}
    task_states: list[str] = []

    for task in tasks:
        kind = task.get("kind")
        state = "planned"
        observation: dict[str, Any] = {}
        if kind == "objective":
            state = "complete"
        elif kind == "evidence_retrieval":
            facet = " ".join(str(task.get("facet") or "").casefold().split())
            row = facets_by_key.get(facet)
            if row:
                coverage_status = row.get("status")
                state = "complete" if coverage_status == "covered" else "partial" if coverage_status == "partial" else "blocked"
                observation = {
                    "coverage_status": coverage_status,
                    "term_coverage_ratio": row.get("term_coverage_ratio"),
                    "evidence_page_count": row.get("evidence_page_count"),
                    "independent_origin_count": row.get("independent_origin_count"),
                    "missing_terms": row.get("missing_terms", []),
                    "recommended_query": row.get("recommended_query", ""),
                }
            elif round_rows and any(item.get("status") in {"budget_exhausted", "rate_limited_partial"} for item in round_rows):
                state = "blocked"
                observation = {"reason": "research budget or provider limit stopped facet retrieval"}
        elif kind == "source_deepening":
            fetched = sum(int(item.get("n_crawled", 0) or 0) for item in crawl_rows)
            if crawl_rows:
                state = "complete" if fetched > 0 else "partial"
            else:
                state = "planned"
            observation = {"crawl_domains": len(crawl_rows), "pages_crawled": fetched}
        elif kind == "synthesis":
            state = "complete" if synthesis else "blocked"
            observation = {"synthesis_chars": len(synthesis)}
        elif kind == "claim_verification":
            if claim_summary.get("verifier_ran"):
                count = int(claim_summary.get("claim_count", 0) or 0)
                state = "complete" if count > 0 and not claim_summary.get("unverified") and not claim_summary.get("missing_citation") else "partial"
                observation = dict(claim_summary)
            elif error:
                state = "blocked"
                observation = {"reason": error[:240]}
        elif kind == "counter_evidence":
            if counter.get("enabled"):
                state = "complete" if counter.get("status") == "ok" and counter.get("claims_with_candidates", 0) else "partial"
                observation = {
                    "status": counter.get("status"),
                    "queries": counter.get("queries"),
                    "results": counter.get("results"),
                    "extracted": counter.get("extracted"),
                    "claims_with_candidates": counter.get("claims_with_candidates"),
                }
            elif error:
                state = "blocked"
                observation = {"reason": error[:240]}
        elif kind == "semantic_coverage":
            if semantic_coverage is not None:
                state = "complete"
                observation = {"semantic_coverage": semantic_coverage}
        task["status"] = state
        task["result"] = observation or None
        task_states.append(state)

    counts = {
        state: sum(1 for task in tasks if task["status"] == state)
        for state in ("complete", "partial", "blocked", "planned")
    }
    if counts["blocked"]:
        overall = "blocked"
    elif counts["partial"] or counts["planned"]:
        overall = "partial"
    else:
        overall = "complete"
    result["status"] = overall
    result["task_count"] = len(tasks)
    result["task_status_counts"] = counts
    result["completed_task_ratio"] = round(counts["complete"] / len(tasks), 3) if tasks else None
    return result
