"""Tests for local research workspace persistence and temporal diffs."""
import hashlib
import os
import stat

import pytest

from research_store import ResearchStore, validate_project_id


def _record(content="A source describes battery recycling regulations and licensed collection.", title="Regulatory report"):
    url = "https://regulator.example/report"
    source_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return url, source_id, {
        "url": url, "href": url, "host": "regulator.example",
        "title": title, "content": content, "tier": 0,
    }


def _payload(content=None, status="supported", query="battery recycling regulations"):
    url, source_id, extracted = _record(content or None)
    return {
        "query": query,
        "mode": "llm",
        "model": "test-model",
        "synthesis": "Battery recycling regulations require licensed collection [1].",
        "sources": [{"url": url, "title": extracted["title"], "host": extracted["host"], "tier": 0}],
        "source_profiles": [{
            "source_id": source_id, "publisher_id": "regulator.example",
            "source_type": "government_publication",
        }],
        "claim_audit": [{
            "claim_id": "C001",
            "claim": "Battery recycling regulations require licensed collection.",
            "status": status,
            "evidence": [{
                "source_id": source_id, "source_role": "cited",
                "stance": "supports", "quote": "licensed collection",
            }],
        }],
        "claim_audit_summary": {"claim_count": 1, "supported": int(status == "supported")},
        "source_intelligence": {"source_count": 1, "publisher_count": 1},
        "research_coverage": {"facet_count": 1, "covered": 1},
    }, extracted


def test_project_id_validation_normalizes_whitespace_and_rejects_empty():
    assert validate_project_id("  Indian   EV market ") == "Indian EV market"
    with pytest.raises(ValueError):
        validate_project_id("  ")
    with pytest.raises(ValueError):
        validate_project_id("x" * 101)


def test_save_first_run_persists_sources_claims_and_returns_baseline_diff(tmp_path):
    db = tmp_path / "workspace.db"
    store = ResearchStore(db)
    payload, extracted = _payload()
    result = store.save_project_run("ev-market", payload, [extracted])
    assert result["status"] == "saved"
    assert result["source_snapshot_count"] == 1
    assert result["claim_count"] == 1
    assert result["diff"]["new_source_count"] == 1

    run = store.get_run("ev-market", result["run_id"], include_content=True)
    assert run is not None
    assert run["query"] == payload["query"]
    assert run["sources"][0]["content"] == extracted["content"]
    assert run["claims"][0]["status"] == "supported"
    assert run["claims"][0]["evidence"][0]["source_role"] == "cited"


def test_same_query_rerun_detects_changed_source_and_claim_status_change(tmp_path):
    store = ResearchStore(tmp_path / "workspace.db")
    first_payload, first_extract = _payload(
        "Battery recycling regulations require licensed collection and certified processing."
    )
    first = store.save_project_run("ev-market", first_payload, [first_extract])

    second_payload, second_extract = _payload(
        "Battery recycling regulations now require licensed collection and quarterly reporting.",
        status="mixed",
    )
    second = store.save_project_run("ev-market", second_payload, [second_extract])
    assert second["diff"]["same_query"] is True
    assert second["diff"]["changed_source_count"] == 1
    assert second["diff"]["claim_status_change_count"] == 1
    assert second["diff"]["claim_status_changes"][0]["old_status"] == "supported"
    assert second["diff"]["claim_status_changes"][0]["new_status"] == "mixed"
    assert second["diff"]["compared_to_run_id"] == first["run_id"]


def test_different_query_skips_claim_status_comparison(tmp_path):
    store = ResearchStore(tmp_path / "workspace.db")
    payload1, extract1 = _payload(query="battery recycling regulations")
    store.save_project_run("ev-market", payload1, [extract1])
    payload2, extract2 = _payload(query="electric vehicle charging infrastructure", status="unverified")
    result = store.save_project_run("ev-market", payload2, [extract2])
    assert result["diff"]["same_query"] is False
    assert result["diff"]["claim_status_change_count"] == 0
    assert "skipped" in result["diff"]["comparison_note"]


def test_new_source_is_reported_on_a_same_query_rerun(tmp_path):
    store = ResearchStore(tmp_path / "workspace.db")
    payload, extracted = _payload()
    store.save_project_run("ev-market", payload, [extracted])
    new_url = "https://independent.example/analysis"
    new_source = {
        "url": new_url, "host": "independent.example", "title": "Independent analysis",
        "content": "Independent analysis discusses battery recycling regulation and collection.",
    }
    payload2, extracted2 = _payload()
    payload2["sources"].append({"url": new_url, "host": "independent.example", "title": "Independent analysis"})
    result = store.save_project_run("ev-market", payload2, [extracted2, new_source])
    assert result["diff"]["new_source_count"] == 1
    assert result["diff"]["new_sources"][0]["url"] == new_url


def test_counter_evidence_role_and_snapshot_are_persisted(tmp_path):
    store = ResearchStore(tmp_path / "workspace.db")
    payload, extracted = _payload()
    counter_url = "https://independent.example/critique"
    counter = {
        "url": counter_url, "host": "independent.example", "title": "Independent critique",
        "content": "The reported effect was not sustained after the pilot ended.",
    }
    counter_id = hashlib.sha256(counter_url.encode("utf-8")).hexdigest()[:16]
    payload["counter_evidence"] = {
        "sources": [{"url": counter_url, "host": "independent.example", "title": "Independent critique"}]
    }
    payload["claim_audit"][0]["evidence"].append({
        "source_id": counter_id, "source_role": "counter_evidence",
        "stance": "contradicts", "quote": "not sustained after the pilot ended",
    })
    result = store.save_project_run("ev-market", payload, [extracted, counter], counter_extracted=[counter])
    run = store.get_run("ev-market", result["run_id"])
    roles = {item["source_role"] for item in run["sources"]}
    evidence_roles = {item["source_role"] for item in run["claims"][0]["evidence"]}
    assert "counter_evidence" in roles
    assert evidence_roles == {"cited", "counter_evidence"}


def test_project_listing_and_run_history_are_available(tmp_path):
    store = ResearchStore(tmp_path / "workspace.db")
    payload, extracted = _payload()
    store.save_project_run("ev-market", payload, [extracted])
    store.save_project_run("renewables", payload, [extracted])
    listed = store.list_projects()
    assert {item["project_id"] for item in listed["projects"]} == {"ev-market", "renewables"}
    history = store.list_runs("ev-market", limit=10)
    assert len(history["runs"]) == 1
    assert history["runs"][0]["summary"]["source_intelligence"]["publisher_count"] == 1


def test_delete_project_removes_its_runs_and_evidence(tmp_path):
    store = ResearchStore(tmp_path / "workspace.db")
    payload, extracted = _payload()
    store.save_project_run("ev-market", payload, [extracted])
    assert store.delete_project("ev-market") is True
    assert store.list_projects("ev-market")["projects"] == []
    assert store.delete_project("ev-market") is False


def test_project_data_isolated_by_project_id(tmp_path):
    store = ResearchStore(tmp_path / "workspace.db")
    payload, extracted = _payload()
    store.save_project_run("project-a", payload, [extracted])
    store.save_project_run("project-b", payload, [extracted])
    assert len(store.list_projects("project-a")["projects"]) == 1
    assert len(store.list_projects("project-b")["projects"]) == 1
    assert store.list_projects("project-a")["projects"][0]["run_count"] == 1


def test_run_retrieval_requires_a_valid_run_id(tmp_path):
    store = ResearchStore(tmp_path / "workspace.db")
    with pytest.raises(ValueError):
        store.get_run("ev-market", "not-a-run-id")


def test_database_file_permissions_are_owner_only_on_posix(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX file modes are not available")
    db = tmp_path / "workspace.db"
    ResearchStore(db)
    assert stat.S_IMODE(db.stat().st_mode) & 0o077 == 0
