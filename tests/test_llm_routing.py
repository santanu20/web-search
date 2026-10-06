"""P0-3 pins: per-stage model routing + keep_alive for Ollama calls.

Light stages (refine/decompose/gap/rerank) route to a small model
(explicit model > WEB_SEARCH_LLM_LIGHT_MODEL > auto-pick smallest
non-embed/vision/ocr/clip). Synthesis stays on the main model. Every
Ollama POST carries keep_alive=30m.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web_search as ws


def _settings_with(**overrides):
    base = dict(
        model="main:9b",
        embed_model="nomic-embed-text",
        ollama_host="http://localhost:11434",
        max_tokens=512,
        timeout=5.0,
        think_default=False,
        provider="ollama",
        light_model="",
    )
    base.update(overrides)
    return ws.LlmSettings(**base)


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a): return None


def _urlopen_factory(payloads_or_bodies, captured):
    def fake_urlopen(req, timeout=None):
        captured.append(req)
        body = (
            payloads_or_bodies.pop(0)
            if isinstance(payloads_or_bodies, list)
            else payloads_or_bodies
        )
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        return _FakeResp(data)

    return fake_urlopen


def test_unit__llm_call__explicit_model_beats_light_routing(monkeypatch):
    captured = []
    import dataclasses

    cfg = dataclasses.replace(ws._settings, llm=_settings_with())
    monkeypatch.setattr(ws, "_settings", cfg)
    monkeypatch.setattr(ws, "_llm_probe", lambda host=None: "ollama")
    monkeypatch.setattr(
        ws.urllib_request,
        "urlopen",
        _urlopen_factory({"model": "x", "response": "ok"}, captured),
    )
    ws._llm_call("p", model="explicit:2b", light=True)
    sent = json.loads(captured[0].data.decode())
    assert sent["model"] == "explicit:2b"


def test_unit__llm_call__light_uses_configured_light_model(monkeypatch):
    import dataclasses

    cfg = dataclasses.replace(ws._settings, llm=_settings_with(light_model="small:1b"))
    monkeypatch.setattr(ws, "_settings", cfg)
    monkeypatch.setattr(ws, "_llm_probe", lambda host=None: "ollama")
    probe_calls = []
    monkeypatch.setattr(
        ws,
        "_pick_light_model",
        lambda host: probe_calls.append(host) or "auto:0b",
    )
    captured = []
    monkeypatch.setattr(
        ws.urllib_request,
        "urlopen",
        _urlopen_factory({"model": "x", "response": "ok"}, captured),
    )
    ws._llm_call("p", light=True)
    sent = json.loads(captured[0].data.decode())
    assert sent["model"] == "small:1b"
    assert probe_calls == []  # configured light model short-circuits auto-pick


def test_unit__llm_call__main_stage_unaffected_without_light(monkeypatch):
    import dataclasses

    cfg = dataclasses.replace(ws._settings, llm=_settings_with())
    monkeypatch.setattr(ws, "_settings", cfg)
    monkeypatch.setattr(ws, "_llm_probe", lambda host=None: "ollama")
    monkeypatch.setattr(
        ws,
        "_pick_light_model",
        lambda host: (_ for _ in ()).throw(AssertionError("must not probe")),
    )
    captured = []
    monkeypatch.setattr(
        ws.urllib_request,
        "urlopen",
        _urlopen_factory({"model": "x", "response": "ok"}, captured),
    )
    ws._llm_call("p")
    sent = json.loads(captured[0].data.decode())
    assert sent["model"] == "main:9b"


def test_unit__pick_light_model__smallest_non_special(monkeypatch):
    tags = {
        "models": [
            {"name": "main:9b", "size": 6_600_000_000},
            {"name": "small:1b", "size": 1_400_000_000},
            {"name": "nomic-embed-text", "size": 274_000_000},
            {"name": "glm-ocr", "size": 2_200_000_000},
            {"name": "llava-vision:7b", "size": 4_700_000_000},
        ]
    }
    monkeypatch.setattr(
        ws.urllib_request,
        "urlopen",
        _urlopen_factory(tags, []),
    )
    ws._LIGHT_MODEL_CACHE.pop("http://localhost:11434", None)
    got = ws._pick_light_model("http://localhost:11434")
    assert got == "small:1b"
    assert ws._LIGHT_MODEL_CACHE["http://localhost:11434"] == "small:1b"


def test_unit__pick_light_model__probe_failure_falls_back_to_main(monkeypatch):
    import dataclasses

    cfg = dataclasses.replace(ws._settings, llm=_settings_with())
    monkeypatch.setattr(ws, "_settings", cfg)

    def boom(req, timeout=None):
        raise ws.urllib_error.URLError("down")

    monkeypatch.setattr(ws.urllib_request, "urlopen", boom)
    ws._LIGHT_MODEL_CACHE.pop("http://localhost:11434", None)
    assert ws._pick_light_model("http://localhost:11434") == "main:9b"


def test_unit__ollama_call__keep_alive_in_payload(monkeypatch):
    captured = []
    monkeypatch.setattr(
        ws.urllib_request,
        "urlopen",
        _urlopen_factory({"model": "x", "response": "ok"}, captured),
    )
    ws._ollama_call("p", model="main:9b")
    sent = json.loads(captured[0].data.decode())
    assert sent["keep_alive"] == "30m"
