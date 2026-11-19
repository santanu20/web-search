"""Tests for explicit research plan graphs and observed task status annotation."""
from research_plan import annotate_research_plan, build_research_plan


def test_plan_contains_objective_facets_crawl_and_synthesis_dependencies():
    plan = build_research_plan(
        "Indian EV battery market",
        ["battery chemistry", "manufacturing capacity", "battery chemistry"],
        max_rounds=3, call_budget_s=120, top_n_per_facet=6,
    )
    assert plan["schema"] == "research.plan.v1"
    assert len(plan["tasks"]) == 4
    objective, facet_a, facet_b, synthesis = plan["tasks"]
    assert objective["kind"] == "objective"
    assert facet_a["kind"] == facet_b["kind"] == "evidence_retrieval"
    assert synthesis["kind"] == "synthesis"
    assert facet_a["dependencies"] == [objective["task_id"]]
    assert synthesis["dependencies"] == [facet_a["task_id"], facet_b["task_id"], plan["tasks"][3 - 0]["task_id"]] or synthesis["dependencies"][-1] == plan["tasks"][2]["task_id"]


def test_plan_ids_are_deterministic_for_same_normalized_query_and_facets():
    first = build_research_plan(
        "EV market", ["battery recycling", "cell manufacturing"],
        max_rounds=2, call_budget_s=90, top_n_per_facet=5,
    )
    second = build_research_plan(
        "  EV   market ", ["battery recycling", "cell manufacturing"],
        max_rounds=2, call_budget_s=90, top_n_per_facet=5,
    )
    assert first["plan_id"] == second["plan_id"]
    assert [t["task_id"] for t in first["tasks"]] == [t["task_id"] for t in second["tasks"]]


def test_optional_tasks_are_added_only_when_requested():
    base = build_research_plan("Question", ["facet"], max_rounds=1, call_budget_s=60, top_n_per_facet=3)
    full = build_research_plan(
        "Question", ["facet"], max_rounds=1, call_budget_s=60, top_n_per_facet=3,
        verify_claims=True, counter_evidence=True, semantic_coverage=True,
    )
    kinds = {task["kind"] for task in full["tasks"]}
    assert "claim_verification" in kinds
    assert "counter_evidence" in kinds
    assert "semantic_coverage" in kinds
    assert "claim_verification" not in {task["kind"] for task in base["tasks"]}


def test_unexecuted_facet_remains_planned_and_is_not_marked_complete():
    plan = build_research_plan("Question", ["facet one"], max_rounds=2, call_budget_s=60, top_n_per_facet=3)
    result = annotate_research_plan(plan, synthesis="Answer text")
    facet = next(task for task in result["tasks"] if task["kind"] == "evidence_retrieval")
    assert facet["status"] == "planned"
    assert result["status"] == "partial"


def test_facet_task_becomes_complete_only_with_coverage_and_origin_evidence():
    plan = build_research_plan("Question", ["market size"], max_rounds=2, call_budget_s=60, top_n_per_facet=3)
    matrix = [{
        "facet": "market size", "status": "covered",
        "term_coverage_ratio": 1.0, "evidence_page_count": 4,
        "independent_origin_count": 2, "missing_terms": [], "recommended_query": "",
    }]
    result = annotate_research_plan(plan, coverage_matrix=matrix, synthesis="A completed report.")
    task = next(task for task in result["tasks"] if task["kind"] == "evidence_retrieval")
    assert task["status"] == "complete"
    assert task["result"]["independent_origin_count"] == 2


def test_partial_and_gap_facets_are_not_completed():
    plan = build_research_plan("Question", ["market size", "regulation"], max_rounds=2, call_budget_s=60, top_n_per_facet=3)
    matrix = [
        {"facet": "market size", "status": "partial", "term_coverage_ratio": 0.6, "evidence_page_count": 1, "independent_origin_count": 1, "missing_terms": ["size"]},
        {"facet": "regulation", "status": "gap", "term_coverage_ratio": 0.0, "evidence_page_count": 0, "independent_origin_count": 0, "missing_terms": ["regulation"]},
    ]
    result = annotate_research_plan(plan, coverage_matrix=matrix, synthesis="Some answer.")
    states = [task["status"] for task in result["tasks"] if task["kind"] == "evidence_retrieval"]
    assert states == ["partial", "blocked"]


def test_claim_audit_and_counter_evidence_tasks_reflect_execution_status():
    plan = build_research_plan(
        "Question", ["facet"], max_rounds=2, call_budget_s=60, top_n_per_facet=3,
        verify_claims=True, counter_evidence=True,
    )
    result = annotate_research_plan(
        plan, synthesis="Answer.",
        claim_audit_summary={"verifier_ran": True, "claim_count": 2, "unverified": 0, "missing_citation": 0},
        counter_evidence={"enabled": True, "status": "ok", "queries": 2, "results": 5, "extracted": 3, "claims_with_candidates": 2},
    )
    claim_task = next(task for task in result["tasks"] if task["kind"] == "claim_verification")
    counter_task = next(task for task in result["tasks"] if task["kind"] == "counter_evidence")
    assert claim_task["status"] == "complete"
    assert counter_task["status"] == "complete"


def test_blocked_claim_audit_does_not_become_complete():
    plan = build_research_plan(
        "Question", ["facet"], max_rounds=2, call_budget_s=60, top_n_per_facet=3,
        verify_claims=True,
    )
    result = annotate_research_plan(plan, synthesis="Answer.", error="Verifier timed out")
    task = next(task for task in result["tasks"] if task["kind"] == "claim_verification")
    assert task["status"] == "blocked"


def test_plan_reports_task_counts_and_bounded_budget():
    plan = build_research_plan(
        "Question", ["A", "B"], max_rounds=3, call_budget_s=90, top_n_per_facet=4,
        verify_claims=True, counter_evidence=True, semantic_coverage=True,
    )
    result = annotate_research_plan(plan, synthesis="Answer.")
    counts = result["task_status_counts"]
    assert counts["complete"] + counts["partial"] + counts["blocked"] + counts["planned"] == result["task_count"]
    assert sum(task["budget_seconds"] for task in result["tasks"]) <= 90.01
    assert result["completed_task_ratio"] is not None


def test_search_budget_exhaustion_marks_unobserved_facets_blocked():
    plan = build_research_plan("Question", ["facet one"], max_rounds=2, call_budget_s=60, top_n_per_facet=3)
    result = annotate_research_plan(
        plan,
        rounds=[{"status": "budget_exhausted"}],
        synthesis="Fallback answer.",
    )
    task = next(task for task in result["tasks"] if task["kind"] == "evidence_retrieval")
    assert task["status"] == "blocked"
