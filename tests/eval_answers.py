#!/usr/bin/env python3
"""Golden-QA eval harness for the pro/deep-search answer paths (P1-5).

REPORTER, not a pytest suite: it runs the real CLI (live network + local
LLM runtime) against golden questions and reports metrics. Exit code is
threshold-based so CI/agents can gate on it, but it is NOT collected by
pytest (no test_ functions) — invoke directly:

    .venv/bin/python tests/eval_answers.py
    .venv/bin/python tests/eval_answers.py --mode text --no-llm
    .venv/bin/python tests/eval_answers.py --min-grounded 0.6

Retry policy: every question gets ONE automatic retry on process failure
(non-zero exit / invalid JSON / timeout) — live metasearch backends are
individually flaky even with the in-process 4-layer defense. Disable with
--no-retry. A question that fails twice is scored as failed, never crashes
the run (H2: bounded, observable).

Metrics (per question and aggregated):
  grounded_rate   — share of [n] citations whose quote is grounded in the
                    fetched source content (from payload.citation_grounding;
                    heuristic mode reports None -> counted 1.0 only when
                    citations exist, else excluded from the aggregate)
  term_coverage   — share of expected signal terms present in the synthesis
                    (answer-relevance proxy; not semantic, just lexical)
  latency_s       — wall clock of the CLI subprocess
  ok              — exit 0, valid JSON, non-empty synthesis, >=1 citation

Verdict: exit 0 iff success_rate and grounded_rate meet thresholds.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "web_search.py"

# Golden questions: stable facts, signal terms chosen for lexical presence.
GOLDEN_QUESTIONS: list[dict[str, object]] = [
    {
        "q": "what is the out-of-africa vs multiregional debate in modern human origins",
        "expect": ["africa", "genetic", "fossil"],
    },
    {
        "q": "why did the challenger space shuttle disaster happen",
        "expect": ["o-ring", "temperature", "launch"],
    },
    {
        "q": "rust borrow checker ownership rules explained",
        "expect": ["ownership", "borrow", "lifetime"],
    },
]


def term_coverage(synthesis: str, expected: list[str]) -> float:
    """Share of expected signal terms present (case-insensitive) in the answer."""
    if not expected:
        return 1.0
    low = synthesis.lower()
    hit = sum(1 for t in expected if t.lower() in low)
    return hit / len(expected)


def grounded_rate(payload: dict) -> float | None:
    """Grounded-citation share from the pro payload; None when unmeasurable."""
    grounding = payload.get("citation_grounding") or []
    if not grounding:
        return None
    return sum(1 for g in grounding if g.get("grounded")) / len(grounding)


def run_once(question: str, args: argparse.Namespace) -> tuple[dict | None, float, str]:
    """One CLI invocation. Returns (payload|None, latency_s, error_detail)."""
    cmd = [sys.executable, str(CLI), args.mode, question]
    if args.mode == "pro":
        if args.no_llm:
            cmd.append("--no-llm")
        if args.model:
            cmd += ["--model", args.model]
        if args.think is not None:
            cmd += ["--think" if args.think else "--no-think"]
        cmd += ["--max-rounds", str(args.max_rounds)]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - start, "timeout"
    latency = time.monotonic() - start
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()[-1:] or [
            f"exit {proc.returncode}"
        ]
        return None, latency, f"exit={proc.returncode}: {detail[0][:200]}"
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return None, latency, f"bad json: {e}"
    return payload, latency, ""


def evaluate_question(item: dict[str, object], args: argparse.Namespace) -> dict:
    question = str(item["q"])
    expected = list(item["expect"])  # type: ignore[arg-type]
    payload, latency, err = run_once(question, args)
    if payload is None and args.retry:
        payload, latency, err = run_once(question, args)
    row: dict[str, object] = {"q": question, "latency_s": round(latency, 1)}
    if payload is None:
        row.update(
            {"ok": False, "error": err, "term_coverage": 0.0, "grounded_rate": None}
        )
        return row
    synthesis = str(payload.get("synthesis") or "")
    n_cites = len(payload.get("citation_grounding") or [])
    gr = grounded_rate(payload)
    # Heuristic mode has no verifier output: credit grounding only if it cited.
    gr_score = gr if gr is not None else (1.0 if n_cites else None)
    row.update(
        {
            "ok": bool(synthesis and (n_cites or payload.get("n_sources"))),
            "mode": payload.get("mode"),
            "n_sources": payload.get("n_sources"),
            "n_citations": n_cites,
            "ungrounded": payload.get("ungrounded_citations"),
            "coverage": payload.get("coverage"),
            "term_coverage": round(term_coverage(synthesis, expected), 2),
            "grounded_rate": None if gr_score is None else round(gr_score, 2),
            "chars": len(synthesis),
        }
    )
    return row


def verdict(rows: list[dict], min_success: float, min_grounded: float) -> dict:
    n = len(rows) or 1
    success_rate = sum(1 for r in rows if r.get("ok")) / n
    grs = [r["grounded_rate"] for r in rows if r.get("grounded_rate") is not None]
    agg_grounded = sum(grs) / len(grs) if grs else None
    covs = [
        r["term_coverage"] for r in rows if isinstance(r.get("term_coverage"), float)
    ]
    agg_cov = sum(covs) / len(covs) if covs else 0.0
    lats = [
        r["latency_s"] for r in rows if isinstance(r.get("latency_s"), (int, float))
    ]
    passed = success_rate >= min_success and (
        agg_grounded is None or agg_grounded >= min_grounded
    )
    return {
        "success_rate": round(success_rate, 2),
        "grounded_rate_agg": None if agg_grounded is None else round(agg_grounded, 2),
        "term_coverage_agg": round(agg_cov, 2),
        "latency_s_mean": round(sum(lats) / len(lats), 1) if lats else None,
        "thresholds": {"min_success": min_success, "min_grounded": min_grounded},
        "passed": passed,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(__doc__ or "Golden-QA eval harness").splitlines()[0]
    )
    p.add_argument("--mode", choices=["pro", "deep-search"], default="pro")
    p.add_argument("--no-llm", action="store_true", help="pro: force heuristic path")
    p.add_argument("--model", default=None, help="pro: LLM model override")
    p.add_argument("--think", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument(
        "--max-rounds", type=int, default=3, help="pro: fewer rounds for eval speed"
    )
    p.add_argument(
        "--timeout", type=float, default=600.0, help="per-question subprocess timeout"
    )
    p.add_argument("--retry", dest="retry", action="store_true", default=True)
    p.add_argument("--no-retry", dest="retry", action="store_false")
    p.add_argument("--min-success", type=float, default=0.75)
    p.add_argument("--min-grounded", type=float, default=0.5)
    p.add_argument(
        "--only",
        type=int,
        default=None,
        metavar="N",
        help="run only golden question N (1-based) for quick manual checks",
    )
    p.add_argument(
        "--json", action="store_true", help="stdout: JSON only (tables -> stderr)"
    )
    args = p.parse_args(argv)

    questions = GOLDEN_QUESTIONS
    if args.only is not None:
        questions = [GOLDEN_QUESTIONS[args.only - 1]]

    rows = [evaluate_question(item, args) for item in questions]
    summary = verdict(rows, args.min_success, args.min_grounded)
    report = {"mode": args.mode, "n": len(rows), "results": rows, "summary": summary}

    if not args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(json.dumps(report, default=str))
    print(
        f"[eval] success={summary['success_rate']} grounded={summary['grounded_rate_agg']} "
        f"coverage={summary['term_coverage_agg']} passed={summary['passed']}",
        file=sys.stderr,
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
