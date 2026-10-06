#!/usr/bin/env python3
"""Benchmark harness for web-search skill.

Runs queries across modes (pro heuristic, pro --use-llm, agentic, research)
and optional model comparison. Captures depth, quality, and latency metrics.

Usage:
    uv run python bench/bench_pro.py                    # default: 4 queries × 4 modes
    uv run python bench/bench_pro.py --model lfm2.5-8b-A1B:latest
    uv run python bench/bench_pro.py --queries 2        # fewer queries (quick)
    uv run python bench/bench_pro.py --modes pro_heuristic,pro_llm  # subset
    uv run python bench/bench_pro.py --report-only      # re-analyze saved results

Output: bench/results/*.json (per run) + comparison table to stdout.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SKILL_DIR / "web_search.py"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

DEFAULT_QUERIES = [
    ("rust-ownership", "how does rust ownership and borrowing work"),
    ("k8s-vs-swarm", "kubernetes vs docker swarm vs nomad comparison"),
    ("crispr-cas9", "what is CRISPR Cas9 gene editing mechanism"),
    ("etf-mechanism", "ETF creation redemption arbitrage mechanism"),
]

MODES = {
    "pro_heuristic": {
        "cmd": "pro",
        "extra": ["--max-rounds", "2", "--top-n-per-facet", "5", "--call-budget", "90"],
    },
    "pro_llm": {
        "cmd": "pro",
        "extra": [
            "--use-llm",
            "--no-think",
            "--max-rounds",
            "2",
            "--top-n-per-facet",
            "5",
            "--call-budget",
            "150",
        ],
    },
    "pro_llm_think": {
        "cmd": "pro",
        "extra": [
            "--use-llm",
            "--max-rounds",
            "1",
            "--top-n-per-facet",
            "5",
            "--call-budget",
            "280",
        ],
    },
    "agentic": {
        "cmd": "agentic",
        "extra": ["--max-iter", "3", "--top-n-extract", "8", "--call-budget", "90"],
    },
    "research_deep": {
        "cmd": "deep-search",
        "extra": [
            "--depth",
            "deep",
            "--top-n",
            "8",
            "--max-pages",
            "8",
            "--call-budget",
            "120",
        ],
    },
}


def run_one(label: str, query: str, mode_key: str, model: str | None) -> dict:
    """Run a single benchmark combination. Returns metrics dict."""
    mode = MODES[mode_key]
    args = [
        "uv",
        "run",
        "python",
        str(SCRIPT),
        mode["cmd"],
        query,
        *mode["extra"],
        "--no-cache",
    ]
    if model and "--use-llm" in mode["extra"]:
        args += ["--model", model]

    suffix = (
        mode_key
        if not model
        else f"{mode_key}_{model.replace(':', '_').replace('.', '_')}"
    )
    outfile = RESULTS_DIR / f"{label}_{suffix}.json"

    print(f"  >>> [{label}] {suffix}", end="", flush=True)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=290,
            cwd=str(SKILL_DIR),
        )
        elapsed = time.monotonic() - start
        if proc.returncode != 0:
            print(f"  FAILED (exit={proc.returncode})")
            return {"error": f"exit_{proc.returncode}", "latency_s": round(elapsed, 1)}
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except subprocess.TimeoutExpired:
        print("  TIMEOUT")
        return {"error": "timeout"}
    except json.JSONDecodeError:
        print("  JSON-ERROR")
        return {"error": "json_decode"}

    data["_latency_s"] = round(elapsed, 1)
    data["_mode"] = suffix
    with open(outfile, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"  {elapsed:.0f}s")
    return data


def extract_metrics(d: dict, mode_type: str) -> dict:
    """Extract unified metrics from any command's output."""
    if d.get("error"):
        return {"error": d["error"]}
    meta = d.get("meta", {})
    if mode_type.startswith("pro"):
        return {
            "n_results": d.get("n_results", 0),
            "n_extracted": d.get("n_extracted", meta.get("pages_extracted", 0)),
            "pages_crawled": meta.get("pages_crawled", 0),
            "n_sources": d.get("n_sources", 0),
            "coverage": d.get("coverage", "-"),
            "confidence": d.get("confidence", "-"),
            "synthesis_chars": len(d.get("synthesis", "")),
            "verified_cites": len(d.get("verified_citations", [])),
            "follow_ups": len(d.get("follow_ups", [])),
            "sub_queries": len(d.get("sub_queries", [])),
            "latency_s": d.get("_latency_s", 0),
        }
    if mode_type == "agentic":
        return {
            "n_results": len(d.get("results", [])),
            "n_extracted": len([e for e in d.get("extracted", []) if e.get("content")]),
            "pages_crawled": 0,
            "n_sources": len(d.get("citations", [])),
            "coverage": "-",
            "confidence": "-",
            "synthesis_chars": 0,
            "verified_cites": 0,
            "follow_ups": 0,
            "sub_queries": d.get("n_iterations", 0),
            "latency_s": d.get("_latency_s", 0),
        }
    if mode_type.startswith("research"):
        return {
            "n_results": len(d.get("results", [])),
            "n_extracted": len([e for e in d.get("extracted", []) if e.get("content")]),
            "pages_crawled": len(d.get("crawled", [])),
            "n_sources": len(d.get("sources", d.get("citations", []))),
            "coverage": "-",
            "confidence": d.get("confidence", "-"),
            "synthesis_chars": sum(
                len(kf.get("passage", "")) for kf in d.get("key_findings", [])
            ),
            "verified_cites": 0,
            "follow_ups": 0,
            "sub_queries": 1,
            "latency_s": d.get("_latency_s", 0),
        }
    return {}


def print_table(results: dict[str, dict[str, dict]]):
    """Print comparison table."""
    hdr = (
        f"{'Mode':<30} {'URLs':>5} {'Reads':>6} {'Crawl':>6} {'Depth':>6} "
        f"{'Srcs':>5} {'Cov':>5} {'Conf':>5} {'Synth':>6} {'Cite':>4} "
        f"{'Foll':>4} {'Decomp':>6} {'Time':>7}"
    )
    print("\n" + "=" * 110)
    print("AVERAGES")
    print("=" * 110)
    print(hdr)
    print("-" * 110)

    mode_keys = list(next(iter(results.values())).keys())
    for mk in mode_keys:
        vals = []
        for ql in results:
            if mk in results[ql] and not results[ql][mk].get("error"):
                vals.append(results[ql][mk])
        if not vals:
            print(f"  {mk:<28} (no data)")
            continue

        def avg(k: str) -> str:
            return (
                f"{sum(v.get(k, 0) if isinstance(v.get(k), (int, float)) else 0 for v in vals) / len(vals):.1f}"
                if vals
                else "-"
            )

        depth = float(avg("n_extracted")) + float(avg("pages_crawled"))
        print(
            f"  {mk:<28} {avg('n_results'):>5} {avg('n_extracted'):>6} "
            f"{avg('pages_crawled'):>6} {depth:>6.1f} {avg('n_sources'):>5} "
            f"{avg('coverage'):>5} {avg('confidence'):>5} {avg('synthesis_chars'):>6} "
            f"{avg('verified_cites'):>4} {avg('follow_ups'):>4} {avg('sub_queries'):>6} "
            f"{avg('latency_s') + 's':>7}"
        )


def main():
    ap = argparse.ArgumentParser(description="Benchmark web-search skill")
    ap.add_argument("--model", default=None, help="Ollama model for --use-llm modes")
    ap.add_argument("--queries", type=int, default=4, help="number of queries (1-4)")
    ap.add_argument(
        "--modes",
        default=None,
        help="comma-delimited mode keys (default: pro_heuristic,pro_llm,agentic,research_deep)",
    )
    ap.add_argument(
        "--report-only",
        action="store_true",
        help="re-analyze saved results without running",
    )
    args = ap.parse_args()

    queries = DEFAULT_QUERIES[: args.queries]
    mode_keys = (
        args.modes.split(",")
        if args.modes
        else ["pro_heuristic", "pro_llm", "agentic", "research_deep"]
    )

    if args.report_only:
        results = {}
        for ql, _ in queries:
            results[ql] = {}
            for mk in mode_keys:
                suffix = (
                    mk
                    if not args.model
                    else f"{mk}_{args.model.replace(':', '_').replace('.', '_')}"
                )
                f = RESULTS_DIR / f"{ql}_{suffix}.json"
                if f.exists():
                    results[ql][suffix] = extract_metrics(json.loads(f.read_text()), mk)
        print_table(results)
        return

    results = {}
    for ql, q in queries:
        results[ql] = {}
        print(f"\n--- {ql} ---")
        for mk in mode_keys:
            data = run_one(ql, q, mk, args.model)
            mode_type = MODES[mk]["cmd"]
            suffix = (
                mk
                if not args.model
                else f"{mk}_{args.model.replace(':', '_').replace('.', '_')}"
            )
            results[ql][suffix] = extract_metrics(data, mode_type)

    print_table(results)
    print(f"\nResults saved to {RESULTS_DIR}/")


if __name__ == "__main__":
    main()
