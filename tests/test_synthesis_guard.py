"""Pins for the empty-synthesis guards (found live 2026-10-07).

Live defect: pro shipped a 75-char title-only synthesis with confidence 0.9
after a thinking-native model burned its token budget on reasoning. Two
guards now enforce honesty: _synthesize_llm retries once with doubled
headroom, and _guard_pro_synthesis falls back to the structured heuristic
synthesis when the body is empty but key findings exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import web_search as ws  # noqa: E402


def _fixtures():
    sources = [{"url": "https://x.test/a", "title": "t", "host": "x.test"}]
    extracted = [{"url": "https://x.test/a", "content": "substantive content " * 50}]
    return sources, extracted


def test_unit__synthesize_llm__empty_answer_retries_once_with_bigger_budget(
    monkeypatch,
):
    calls: list[dict] = []

    def fake_llm_call(prompt, model=None, system=None, **kw):
        calls.append(kw)
        return "" if len(calls) == 1 else "Recovered answer with a citation [1]."

    monkeypatch.setattr(ws, "_llm_call", fake_llm_call)
    monkeypatch.setattr(ws, "_verify_quote_grounding", lambda raw, *a, **k: ([], raw))
    sources, extracted = _fixtures()

    synthesis, verified, grounding = ws._synthesize_llm(
        "test query", extracted, sources
    )

    assert len(calls) == 2, "empty answer must trigger exactly one retry"
    assert calls[1]["max_tokens"] == min(calls[0]["max_tokens"] * 2, 16384)
    assert "Recovered answer" in synthesis
    assert grounding == []


def test_unit__synthesize_llm__healthy_answer_no_retry(monkeypatch):
    calls: list[dict] = []

    def fake_llm_call(prompt, model=None, system=None, **kw):
        calls.append(kw)
        return "Direct answer [1] without wasting the budget."

    monkeypatch.setattr(ws, "_llm_call", fake_llm_call)
    monkeypatch.setattr(ws, "_verify_quote_grounding", lambda raw, *a, **k: ([], raw))
    sources, extracted = _fixtures()

    synthesis, _, _ = ws._synthesize_llm("test query", extracted, sources)

    assert len(calls) == 1
    assert "Direct answer" in synthesis


def test_unit__guard__title_only_with_findings_falls_back_to_heuristic():
    findings = [
        {"passage": "Real finding one", "relevance": 9, "url": "https://x.test/a"}
    ]
    sources = [{"url": "https://x.test/a"}]
    synthesis, fallback = ws._guard_pro_synthesis(
        "# some query\n\n", "some query", findings, sources, {}, use_llm=True
    )
    assert fallback == "heuristic_empty_llm"
    assert "## Key findings" in synthesis
    assert "Real finding one" in synthesis


def test_unit__guard__healthy_body_untouched():
    healthy = "# some query\n\nA body long enough to be a real answer. " * 3
    synthesis, fallback = ws._guard_pro_synthesis(
        healthy, "some query", [{"passage": "f"}], [{}], {}, use_llm=True
    )
    assert synthesis == healthy
    assert fallback is None


def test_unit__guard__no_findings_left_alone():
    title_only = "# some query\n\n"
    synthesis, fallback = ws._guard_pro_synthesis(
        title_only, "some query", [], [], {}, use_llm=False
    )
    assert synthesis == title_only
    assert fallback is None
