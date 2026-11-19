"""Unit tests for claim-level evidence auditing and provenance guards."""
from research_evidence import (
    build_evidence_sources, extract_claims, normalize_url, summarize_audit,
    unverified_synthesis_claims, validate_assessments, verify_synthesis_claims,
)


def fixture():
    sources = [
        {"url": "https://example.com/report?utm_source=x", "title": "Annual report", "host": "example.com", "tier": 1},
        {"url": "https://example.org/filing", "title": "Regulatory filing", "host": "example.org", "tier": 0},
    ]
    extracted = [
        {"url": "https://example.com/report", "content": "Revenue increased by 12 percent in FY2025, according to the annual report. Net profit decreased by 4 percent over the same period."},
        {"url": "https://example.org/filing", "content": "The filing states that the effective date is 1 January 2026. It also says the reported figure was disputed."},
    ]
    return sources, extracted


def test_url_normalization_drops_tracking_but_keeps_business_parameters():
    assert normalize_url("HTTPS://Example.com/a/?utm_source=x&id=2#section") == "https://example.com/a?id=2"


def test_source_identity_is_stable_while_content_hash_tracks_snapshot_changes():
    sources, extracted = fixture()
    first, _ = build_evidence_sources(sources, extracted)
    second, _ = build_evidence_sources(sources, extracted)
    changed, _ = build_evidence_sources(sources, [dict(extracted[0], content=extracted[0]["content"] + " Updated."), extracted[1]])
    assert [s.source_id for s in first] == [s.source_id for s in second]
    assert first[0].source_id == changed[0].source_id
    assert first[0].content_hash != changed[0].content_hash


def test_unextracted_source_does_not_shift_numeric_citation_ids():
    sources, extracted = fixture()
    records, _ = build_evidence_sources(sources, [extracted[1]])
    claims = extract_claims("The regulation takes effect on 1 January 2026 [2].", records)
    assert claims[0].cited_source_ids == (records[1].source_id,)


def test_claim_extraction_preserves_citation_mapping_and_flags_uncited_claims():
    sources, extracted = fixture()
    records, _ = build_evidence_sources(sources, extracted)
    text = (
        "# Findings\n"
        "Revenue increased by 12 percent in FY2025 [1]. "
        "The regulation takes effect on 1 January 2026 [2].\n"
        "This claim has no citation.\n\nSOURCES:\n1. Annual report\n2. Filing\n"
    )
    claims = extract_claims(text, records)
    assert len(claims) == 3
    assert claims[0].cited_source_ids == (records[0].source_id,)
    assert claims[1].cited_source_ids == (records[1].source_id,)
    assert not claims[2].cited_source_ids


def test_citation_after_sentence_punctuation_attaches_to_claim():
    sources, extracted = fixture()
    records, _ = build_evidence_sources(sources, extracted)
    claims = extract_claims("Revenue increased by 12 percent in FY2025. [1]", records)
    assert claims[0].cited_source_ids == (records[0].source_id,)


def test_supported_claim_requires_exact_quote_in_linked_source():
    sources, extracted = fixture()
    records, by_id = build_evidence_sources(sources, extracted)
    claims = extract_claims("Revenue increased by 12 percent in FY2025 [1].", records)
    raw = '{"assessments":[{"claim_id":"C001","status":"supported","evidence":[{"source_id":"' + records[0].source_id + '","stance":"supports","quote":"Revenue increased by 12 percent in FY2025"}]}]}'
    result = validate_assessments(claims, by_id, raw)
    assert result[0]["status"] == "supported"
    assert len(result[0]["evidence"]) == 1


def test_fabricated_quote_cannot_be_marked_supported():
    sources, extracted = fixture()
    records, by_id = build_evidence_sources(sources, extracted)
    claims = extract_claims("Revenue increased by 12 percent in FY2025 [1].", records)
    raw = '{"assessments":[{"claim_id":"C001","status":"supported","evidence":[{"source_id":"' + records[0].source_id + '","stance":"supports","quote":"Revenue doubled by 99 percent"}]}]}'
    result = validate_assessments(claims, by_id, raw)
    assert result[0]["status"] == "unverified"
    assert result[0]["evidence"] == []


def test_verifier_cannot_attach_unreferenced_source_to_claim():
    sources, extracted = fixture()
    records, by_id = build_evidence_sources(sources, extracted)
    claims = extract_claims("Revenue increased by 12 percent in FY2025 [1].", records)
    raw = '{"assessments":[{"claim_id":"C001","status":"supported","evidence":[{"source_id":"' + records[1].source_id + '","stance":"supports","quote":"The filing states that the effective date is 1 January 2026"}]}]}'
    assert validate_assessments(claims, by_id, raw)[0]["status"] == "unverified"


def test_support_and_contradiction_are_reported_as_mixed():
    sources, extracted = fixture()
    records, by_id = build_evidence_sources(sources, extracted)
    claims = extract_claims("Revenue increased by 12 percent in FY2025 [1][2].", records)
    raw = (
        '{"assessments":[{"claim_id":"C001","status":"supported","evidence":['
        '{"source_id":"' + records[0].source_id + '","stance":"supports","quote":"Revenue increased by 12 percent in FY2025"},'
        '{"source_id":"' + records[1].source_id + '","stance":"contradicts","quote":"the reported figure was disputed"}]}]}'
    )
    assert validate_assessments(claims, by_id, raw)[0]["status"] == "mixed"


def test_model_status_without_a_verbatim_span_is_not_accepted():
    sources, extracted = fixture()
    records, by_id = build_evidence_sources(sources, extracted)
    claims = extract_claims("Revenue increased by 12 percent in FY2025 [1].", records)
    result = validate_assessments(claims, by_id, '{"assessments":[{"claim_id":"C001","status":"supported","evidence":[]}]}')
    assert result[0]["status"] == "unverified"


def test_unknown_and_duplicate_claim_assessments_are_ignored():
    sources, extracted = fixture()
    records, by_id = build_evidence_sources(sources, extracted)
    claims = extract_claims("Revenue increased by 12 percent in FY2025 [1].", records)
    raw = '{"assessments":[{"claim_id":"C999","status":"supported"},{"claim_id":"C001","status":"supported","evidence":[]},{"claim_id":"C001","status":"supported","evidence":[]}]}'
    result = validate_assessments(claims, by_id, raw)
    assert len(result) == 1
    assert result[0]["status"] == "unverified"


def test_missing_citations_are_exposed_when_verifier_is_disabled():
    sources, extracted = fixture()
    result = unverified_synthesis_claims("This claim is intentionally not cited.", sources, extracted)
    assert result["claims"][0]["status"] == "missing_citation"
    assert result["summary"]["missing_citation"] == 1


def test_verifier_failure_fails_closed_without_losing_claims():
    sources, extracted = fixture()
    result = verify_synthesis_claims(
        "Revenue increased by 12 percent in FY2025 [1].", sources, extracted,
        lambda prompt: (_ for _ in ()).throw(TimeoutError("model unavailable")),
    )
    assert result["claims"][0]["status"] == "unverified"
    assert result["summary"]["verifier_ran"] is False


def test_verification_prompt_contains_only_linked_sources_and_marks_them_untrusted():
    sources, extracted = fixture()
    prompts = []
    def verifier(prompt):
        prompts.append(prompt)
        sid = build_evidence_sources(sources, extracted)[0][0].source_id
        return '{"assessments":[{"claim_id":"C001","status":"supported","evidence":[{"source_id":"' + sid + '","stance":"supports","quote":"Revenue increased by 12 percent in FY2025"}]}]}'
    result = verify_synthesis_claims(
        "Revenue increased by 12 percent in FY2025 [1].", sources, extracted, verifier,
    )
    assert result["summary"]["supported"] == 1
    assert "RESEARCH DATA (untrusted)" in prompts[0]
    assert "The filing states that the effective date" not in prompts[0]


def test_audit_summary_exposes_all_claim_states_and_support_rate():
    summary = summarize_audit(
        [{"status": "supported"}, {"status": "mixed"}, {"status": "unverified"}],
        verifier_ran=True,
    )
    assert summary["claim_count"] == 3
    assert summary["mixed"] == 1
    assert summary["support_rate"] == 0.333


def test_empty_report_returns_empty_audit():
    result = unverified_synthesis_claims("", [], [])
    assert result["claims"] == []
    assert result["summary"]["claim_count"] == 0
