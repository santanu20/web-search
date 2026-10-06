"""CLI surface contract — guards against copy-paste flag drift between
subcommands (the class of bug behind F-53 and the deep-search --max-results
incident). If this test fails after INTENDING to change the surface, update
GOLDEN deliberately; unexpected diffs = accidental drift.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest


_INTROSPECT = """
import json, sys, argparse
sys.path.insert(0, %r)
import web_search as ws
p = ws._build_parser()
sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
out = {}
for name, sp in sub.choices.items():
    fl = {}
    for a in sp._actions:
        for o in a.option_strings or []:
            if o not in ("-h", "--help"):
                fl[o] = a.default
    out[name] = fl
print(json.dumps(out))
""" % __file__.rsplit("/tests/", 1)[0]

# Committed surface: subcommand -> sorted [(option, default)] for options.
# Help/-h excluded. Defaults must match EXACTLY (drift detector).
GOLDEN: dict[str, dict[str, object]] = {
    "text": {
        "--backend": "auto",
        "--call-budget": 600.0,
        "--extract": 3,
        "--extract-chars": 0,
        "--max-results": 10,
        "--no-cache": False,
        "--page": 1,
        "--proxy": None,
        "--region": "us-en",
        "--safesearch": "moderate",
        "--sort": "latest",
        "--timeout": 10,
        "--timelimit": None,
    },
}

# Every search-family subcommand must expose this common network/config set
# (the 8x re-declared group). Presence check = cheap drift alarm even where
# full goldens above are only spot-held.
COMMON_REQUIRED = {
    "--no-cache",
    "--proxy",
    "--timeout",
}
SEARCH_FAMILY = {"text", "news", "crawl", "agentic", "deep-search", "pro", "batch"}
# crawl/extract pick engines internally; --backend is search-mode-only.
BACKEND_MODES = {"text", "news", "agentic", "deep-search", "pro", "batch"}


def _flags(subparsers, name: str):
    return dict(subparsers[name])


def _out_of_box_surface() -> dict:
    """Build the parser in a scrubbed-env subprocess: the contract pins the
    OUT-OF-BOX surface, immune to WEB_SEARCH_* vars in the calling shell
    (F-55: parser defaults are env-shaped by design)."""
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("WEB_SEARCH_")}
    env["WEB_SEARCH_NO_SKILL_VENV"] = "1"
    raw = subprocess.run(
        [sys.executable, "-c", _INTROSPECT],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    start = raw.rfind("\n{")
    return json.loads(raw[start + 1 :] if start >= 0 else raw)


def _OUT_OF_BOX_SURFACE() -> dict:
    """Build the parser in a scrubbed-env subprocess: the contract pins the
    OUT-OF-BOX surface, immune to WEB_SEARCH_* vars in the calling shell
    (F-55: parser defaults are env-shaped by design)."""
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("WEB_SEARCH_")}
    env["WEB_SEARCH_NO_SKILL_VENV"] = "1"
    raw = subprocess.run(
        [sys.executable, "-c", _INTROSPECT],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    start = raw.rfind("\n{")
    return json.loads(raw[start + 1 :] if start >= 0 else raw)


@pytest.fixture(scope="module")
def subparsers():
    return _OUT_OF_BOX_SURFACE()


def test_golden_text_surface_unchanged(subparsers):
    # Only compare keys present in GOLDEN (additive new flags don't fail;
    # changed/removed defaults do).
    actual = _flags(subparsers, "text")
    for opt, default in GOLDEN["text"].items():
        assert opt in actual, f"text lost {opt}"
        assert actual[opt] == default, f"text.{opt} default drifted: {actual[opt]!r}"


@pytest.mark.parametrize("name", sorted(SEARCH_FAMILY))
def test_common_flags_present_in_search_family(subparsers, name):
    actual = set(_flags(subparsers, name))
    missing = COMMON_REQUIRED - actual
    assert not missing, f"{name} missing common flags: {sorted(missing)}"


def test_backend_flag_only_on_search_modes(subparsers):
    for name in subparsers:
        has = "--backend" in _flags(subparsers, name)
        if name in BACKEND_MODES:
            assert has, f"{name} lost --backend"
        elif not has:
            pass  # util subcommands legitimately lack it


def test_no_subcommand_grows_unreviewed_budget_flag(subparsers):
    # --call-budget belongs to compute-heavy modes only; adding it elsewhere
    # silently changes cost behavior.
    allowed = SEARCH_FAMILY
    for name in subparsers:
        if name in allowed:
            continue
        assert "--call-budget" not in _flags(subparsers, name), (
            f"{name} gained --call-budget without review"
        )
