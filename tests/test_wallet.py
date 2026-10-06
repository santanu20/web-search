"""Wallet + profile pins (independent mirror of the geokit architecture)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import web_search as ws  # noqa: E402


def test_wallet_raw_and_netscape(monkeypatch, tmp_path):
    monkeypatch.setattr(ws, "_WALLET_DIR", tmp_path)
    (tmp_path / "researchgate.net.txt").write_text("sid=abc; gsession=xyz\n")
    assert ws.wallet_cookie("researchgate.net") == "sid=abc; gsession=xyz"
    (tmp_path / "n.example.txt").write_text(
        ".n\tTRUE\t/\tTRUE\t0\tsid\tv1\n"
    )
    assert "sid=v1" in ws.wallet_cookie("n.example")


def test_profile_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "_PROFILE_DIR", tmp_path)
    assert ws._profile_for("x.example") is None
    (tmp_path / "researchgate.net").mkdir()
    assert ws._profile_for("www.researchgate.net") == tmp_path / "researchgate.net"


def test_cascade_wallet_first(monkeypatch, tmp_path):
    monkeypatch.setattr(ws, "_WALLET_DIR", tmp_path)
    (tmp_path / "w.example.txt").write_text("sid=WALLET")
    seen = {}

    class _F:
        name = "curl_cffi"

        def fetch(self, url, cookies=""):
            seen["cookies"] = cookies
            return ws.FetchResult(
                html="<html><p>body text</p></html>",
                content_type="text/html",
                cookies="cached=1; sid=STALE",
                source="curl_cffi",
            )

    class _Store:
        def get(self, host):
            return "cached=1; sid=STALE"

        def save(self, host, c):
            pass

    cascade = ws.ExtractCascade(
        fetchers=[_F()],
        extractor=SimpleNamespace(extract=lambda html, url: "text " * 40),
        cookie_store=_Store(),
    )
    assert cascade.run("https://w.example/p", "w.example")
    assert "sid=WALLET" in seen["cookies"] and "STALE" not in seen["cookies"]
