"""P0-2 pins: passage-level evidence windows replace [:4000] head-truncation.

_synthesize_llm and _verify_quote_grounding must consume the SAME
_evidence_window helper. Pins: kill-switch head-mode byte-equivalence,
deep-passage selection, zero-overlap fallback (never empty), budget cap,
deep-quote grounding (the headline fix), legacy-caller compatibility.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web_search as ws

HEAD_JUNK = ("cookie consent banner text about privacy policy settings. " * 60) + "\n\n"
DEEP_MARK = "magma ocean thermal evolution constrained by hafnium isotopes"
DEEP_PARA = DEEP_MARK + " in the Hadean eon; " + "luhafnium evidence detail. " * 40


def _deep_content() -> str:
    head = HEAD_JUNK * 8  # ~8x 450 chars of off-topic boilerplate > 4000 chars
    return head + "\n\n" + DEEP_PARA + "\n\n" + HEAD_JUNK * 4


def test_unit__evidence_window__head_mode_env_is_byte_exact_kill_switch(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_EVIDENCE_MODE", "head")
    content = _deep_content()
    got = ws._evidence_window(content, {"magma", "ocean", "hafnium"})
    assert got == content[:4000]


def test_unit__evidence_window__deep_passage_beats_head_boilerplate(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_EVIDENCE_MODE", raising=False)
    got = ws._evidence_window(_deep_content(), {"magma", "ocean", "hafnium"})
    assert DEEP_MARK in got
    assert "cookie consent" not in got


def test_unit__evidence_window__zero_overlap_falls_back_to_head(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_EVIDENCE_MODE", raising=False)
    content = HEAD_JUNK * 10
    got = ws._evidence_window(content, {"magma", "ocean"})
    assert got == content[:4000]  # never empty, deterministic head fallback


def test_unit__evidence_window__empty_content_returns_empty(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_EVIDENCE_MODE", raising=False)
    assert ws._evidence_window("", {"magma"}) == ""


def test_unit__evidence_window__respects_char_budget(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_EVIDENCE_MODE", raising=False)
    paras = "\n\n".join(
        f"magma ocean paragraph {i} with relevant terms repeated here. " * 3
        for i in range(200)
    )
    got = ws._evidence_window(paras, {"magma", "ocean"})
    assert 0 < len(got) <= 4000


def test_unit__verify_grounding__deep_quote_now_grounded(monkeypatch):
    # The headline P0-2 regression: quote from >4000-char offset used to be
    # impossible to ground (verifier scanned only the head).
    monkeypatch.delenv("WEB_SEARCH_EVIDENCE_MODE", raising=False)
    content = _deep_content()
    sources = [{"url": "https://x.test/a"}]
    extracted = [{"url": "https://x.test/a", "content": content}]
    raw = f'answer [1]\n\nSOURCES:\n1. "{DEEP_MARK} in the Hadean eon"'
    records, _ = ws._verify_quote_grounding(
        raw, sources, extracted, query="magma ocean hafnium"
    )
    assert records[0]["grounded"] is True


def test_unit__verify_grounding__absent_quote_still_ungrounded(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_EVIDENCE_MODE", raising=False)
    content = _deep_content()
    sources = [{"url": "https://x.test/a"}]
    extracted = [{"url": "https://x.test/a", "content": content}]
    raw = (
        'answer [1]\n\nSOURCES:\n1. "completely fabricated quantum teleportation quote"'
    )
    records, _ = ws._verify_quote_grounding(
        raw, sources, extracted, query="magma ocean hafnium"
    )
    assert records[0]["grounded"] is False


def test_unit__verify_grounding__legacy_no_query_head_scan(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_EVIDENCE_MODE", raising=False)
    head_quote = "cookie consent banner text about privacy policy settings"
    content = head_quote + " tail. " * 200 + "\n\n" + DEEP_PARA
    sources = [{"url": "https://x.test/a"}]
    extracted = [{"url": "https://x.test/a", "content": content}]
    raw = f'answer\n\nSOURCES:\n1. "{head_quote}"'
    records, _ = ws._verify_quote_grounding(raw, sources, extracted)
    assert records[0]["grounded"] is True


def test_unit__synthesize_llm__prompt_carries_deep_passage(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_EVIDENCE_MODE", raising=False)
    captured = {}

    def fake_llm_call(prompt, model=None, system=None, **kw):
        captured["prompt"] = prompt
        return 'answer [1]\n\nSOURCES:\n1. "placeholder quote that grounds nowhere"'

    monkeypatch.setattr(ws, "_llm_call", fake_llm_call)
    sources = [{"url": "https://x.test/a", "title": "t", "host": "x.test"}]
    extracted = [{"url": "https://x.test/a", "content": _deep_content()}]
    ws._synthesize_llm("magma ocean hafnium", extracted, sources)
    assert DEEP_MARK in captured["prompt"]
    assert "cookie consent" not in captured["prompt"]
