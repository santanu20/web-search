"""Tests for independent counter-evidence discovery and claim-audit admission."""
import json

from research_evidence import (
    assign_counter_evidence,
    build_evidence_sources,
    build_verification_prompt,
    extract_claims,
    plan_counter_queries,
    validate_assessments,
    verify_synthesis_claims,
)


def _fixture():
    original_sources = [
        {"url": "https://publisher-a.example/report", "host": "publisher-a.example", "title": "Original report"}
    ]
    original_extracted = [
        {"url": "https://publisher-a.example/report", "host": "publisher-a.example",
         "title": "Original report",
         "content": "The pilot program reduced energy costs by 18 percent in 2025, according to measured operating data."}
    ]
    counter_extracted = [
        {"url": "https://publisher-b.example/analysis", "host": "publisher-b.example",
         "title": "Independent analysis",
         "content": "An independent analysis found the reported energy cost reduction was not sustained after the pilot period. Operating costs increased when maintenance and replacement expenses were included."},
        {"url": "https://publisher-a.example/another-page", "host": "publisher-a.example",
         "title": "Same publisher",
         "content": "The pilot program reduced energy costs by 18 percent in 2025, according to measured operating data."},
        {"url": "https://publisher-c.example/irrelevant", "host": "publisher-c.example",
         "title": "Unrelated article",
         "content": "The history of medieval architecture in northern Europe is complex and interesting."},
    ]
    return original_sources, original_extracted, counter_extracted


def test_counter_query_plan_targets_cited_claims_and_respects_cap():
    sources, extracted, _ = _fixture()
    evidence_sources, _ = build_evidence_sources(sources, extracted)
    claims = extract_claims(
        "The pilot program reduced energy costs by 18 percent in 2025 [1].\n"
        "This uncited statement should not be investigated.",
        evidence_sources,
    )
    plan = plan_counter_queries(claims, max_claims=1)
    assert len(plan) == 1
    assert plan[0]["claim_id"] == "C001"
    assert "contrary evidence" in plan[0]["query"]


def test_counter_assignment_excludes_original_host_and_unrelated_content():
    sources, extracted, counter = _fixture()
    original, original_by_id = build_evidence_sources(sources, extracted)
    claims = extract_claims(
        "The pilot program reduced energy costs by 18 percent in 2025 [1].", original
    )
    candidates, _ = build_evidence_sources([], counter)
    assigned = assign_counter_evidence(
        claims, candidates, cited_sources_by_id=original_by_id, max_per_claim=3
    )
    selected = assigned["C001"]
    assert len(selected) == 1
    assert candidates[0].source_id in selected
    assert all(candidates[1].source_id != sid for sid in selected)
    assert all(candidates[2].source_id != sid for sid in selected)


def test_counter_assignment_caps_candidates_to_unique_publishers():
    sources, extracted, counter = _fixture()
    original, original_by_id = build_evidence_sources(sources, extracted)
    claims = extract_claims(
        "The pilot program reduced energy costs by 18 percent in 2025 [1].", original
    )
    expanded = counter + [
        {"url": "https://publisher-b.example/second", "host": "publisher-b.example",
         "content": "The pilot program reduced energy costs by 18 percent, but maintenance costs rose."},
        {"url": "https://publisher-d.example/analysis", "host": "publisher-d.example",
         "content": "The pilot program reduced energy costs by 18 percent, but its long-term performance is unclear."},
    ]
    candidates, _ = build_evidence_sources([], expanded)
    assigned = assign_counter_evidence(
        claims, candidates, cited_sources_by_id=original_by_id, max_per_claim=3
    )
    hosts = {
        next(src.host for src in candidates if src.source_id == sid)
        for sid in assigned["C001"]
    }
    assert len(hosts) == len(assigned["C001"])
    assert len(hosts) <= 3


def test_counter_evidence_must_be_explicitly_assigned_to_claim():
    sources, extracted, counter = _fixture()
    original, _ = build_evidence_sources(sources, extracted)
    candidates, by_id = build_evidence_sources([], counter)
    claims = extract_claims(
        "The pilot program reduced energy costs by 18 percent in 2025 [1].", original
    )
    raw = json.dumps({"assessments": [{
        "claim_id": "C001", "status": "contradicted",
        "evidence": [{
            "source_id": candidates[0].source_id, "stance": "contradicts",
            "quote": "the reported energy cost reduction was not sustained after the pilot period"
        }]
    }]})
    result = validate_assessments(claims, {**by_id}, raw)
    assert result[0]["status"] == "unverified"


def test_counter_evidence_quote_is_admitted_only_for_assigned_claim():
    sources, extracted, counter = _fixture()
    original, _ = build_evidence_sources(sources, extracted)
    candidates, by_id = build_evidence_sources([], counter)
    claims = extract_claims(
        "The pilot program reduced energy costs by 18 percent in 2025 [1].", original
    )
    assigned = {"C001": [candidates[0].source_id]}
    raw = json.dumps({"assessments": [{
        "claim_id": "C001", "status": "contradicted",
        "evidence": [{
            "source_id": candidates[0].source_id, "stance": "contradicts",
            "quote": "the reported energy cost reduction was not sustained after the pilot period"
        }]
    }]})
    result = validate_assessments(
        claims, by_id, raw, additional_evidence_by_claim=assigned
    )
    assert result[0]["status"] == "contradicted"
    assert result[0]["evidence"][0]["source_role"] == "counter_evidence"


def test_cited_support_plus_counterevidence_contradiction_becomes_mixed():
    sources, extracted, counter = _fixture()
    original, original_by_id = build_evidence_sources(sources, extracted)
    candidates, counter_by_id = build_evidence_sources([], counter)
    claims = extract_claims(
        "The pilot program reduced energy costs by 18 percent in 2025 [1].", original
    )
    sid = candidates[0].source_id
    raw = json.dumps({"assessments": [{
        "claim_id": "C001", "status": "mixed",
        "evidence": [
            {"source_id": original[0].source_id, "stance": "supports",
             "quote": "The pilot program reduced energy costs by 18 percent in 2025"},
            {"source_id": sid, "stance": "contradicts",
             "quote": "the reported energy cost reduction was not sustained after the pilot period"},
        ]
    }]})
    result = validate_assessments(
        claims, {**original_by_id, **counter_by_id}, raw,
        additional_evidence_by_claim={"C001": [sid]},
    )
    assert result[0]["status"] == "mixed"
    assert {item["source_role"] for item in result[0]["evidence"]} == {"cited", "counter_evidence"}


def test_verification_prompt_labels_counter_sources_separately():
    sources, extracted, counter = _fixture()
    original, _ = build_evidence_sources(sources, extracted)
    counter_sources, counter_by_id = build_evidence_sources([], counter)
    claims = extract_claims(
        "The pilot program reduced energy costs by 18 percent in 2025 [1].", original
    )
    sid = counter_sources[0].source_id
    prompt = build_verification_prompt(
        claims, {original[0].source_id: original[0], **counter_by_id},
        additional_evidence_by_claim={"C001": [sid]},
    )
    assert '"roles": ["counter_evidence"]' in prompt
    assert "independently retrieved" in prompt


def test_verification_pipeline_uses_additional_sources_and_checks_quote():
    sources, extracted, counter = _fixture()
    original, _ = build_evidence_sources(sources, extracted)
    counter_sources, _ = build_evidence_sources([], counter)
    claim = extract_claims(
        "The pilot program reduced energy costs by 18 percent in 2025 [1].", original
    )[0]
    candidate = next(s for s in counter_sources if s.host == "publisher-b.example")
    additional = {"C001": [candidate.source_id]}

    def verifier(prompt):
        return json.dumps({"assessments": [{
            "claim_id": "C001", "status": "contradicted",
            "evidence": [{
                "source_id": candidate.source_id, "stance": "contradicts",
                "quote": "the reported energy cost reduction was not sustained after the pilot period"
            }]
        }]})

    result = verify_synthesis_claims(
        "The pilot program reduced energy costs by 18 percent in 2025 [1].",
        sources, extracted, verifier,
        additional_extracted=counter, additional_evidence_by_claim=additional,
    )
    assert result["claims"][0]["status"] == "contradicted"
    assert result["claims"][0]["evidence"][0]["source_role"] == "counter_evidence"


def test_counter_search_plan_is_bounded_and_never_targets_uncited_claims():
    sources, extracted, _ = _fixture()
    original, _ = build_evidence_sources(sources, extracted)
    claims = extract_claims(
        "This cited claim is sufficiently long to investigate [1].\n"
        "This second claim has no citation and should be omitted.",
        original,
    )
    plan = plan_counter_queries(claims)
    assert [item["claim_id"] for item in plan] == ["C001"]
