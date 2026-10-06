"""Unit pins for eval_answers metric math (no network — the harness itself
is a live-run reporter; these pin the scoring functions it uses)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "eval_answers", Path(__file__).resolve().parent / "eval_answers.py"
)
assert _SPEC is not None and _SPEC.loader is not None
eval_answers = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("eval_answers", eval_answers)
_SPEC.loader.exec_module(eval_answers)


def test_unit__term_coverage__all_present_case_insensitive() -> None:
    assert (
        eval_answers.term_coverage(
            "Africa dispersal genetic FOSSIL record", ["africa", "genetic", "fossil"]
        )
        == 1.0
    )


def test_unit__term_coverage__partial_and_empty() -> None:
    assert (
        eval_answers.term_coverage(
            "o-ring failure only", ["o-ring", "temperature", "launch"]
        )
        == 1 / 3
    )
    assert eval_answers.term_coverage("", ["anything"]) == 0.0
    assert eval_answers.term_coverage("irrelevant", []) == 1.0


def test_unit__grounded_rate__mixed_grounding() -> None:
    payload = {
        "citation_grounding": [
            {"grounded": True},
            {"grounded": True},
            {"grounded": False},
        ]
    }
    assert eval_answers.grounded_rate(payload) == 2 / 3
    assert eval_answers.grounded_rate({"citation_grounding": []}) is None
    assert eval_answers.grounded_rate({}) is None


def test_unit__verdict__thresholds_and_none_aggregate() -> None:
    rows = [
        {"ok": True, "grounded_rate": 0.8, "term_coverage": 1.0, "latency_s": 10.0},
        {"ok": True, "grounded_rate": 0.6, "term_coverage": 0.5, "latency_s": 20.0},
        {
            "ok": False,
            "grounded_rate": None,
            "term_coverage": 0.0,
            "latency_s": 5.0,
            "error": "timeout",
        },
    ]
    v = eval_answers.verdict(rows, min_success=0.6, min_grounded=0.5)
    assert v["success_rate"] == 0.67  # verdict rounds aggregates to 2dp
    assert v["grounded_rate_agg"] == 0.7
    assert v["passed"] is True  # 0.67 >= 0.6 and 0.7 >= 0.5


def test_unit__verdict__none_aggregate_never_fails_grounded_gate() -> None:
    rows = [{"ok": True, "grounded_rate": None, "term_coverage": 0.0, "latency_s": 1.0}]
    v = eval_answers.verdict(rows, min_success=1.0, min_grounded=0.99)
    assert v["grounded_rate_agg"] is None
    assert v["passed"] is True


def test_unit__verdict__below_threshold_fails() -> None:
    rows = [{"ok": False, "grounded_rate": 0.2, "term_coverage": 0.0, "latency_s": 1.0}]
    v = eval_answers.verdict(rows, min_success=0.75, min_grounded=0.5)
    assert v["passed"] is False
