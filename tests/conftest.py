"""pytest conftest — loads web_search.py as importable module + provides fakes.

web_search.py is a single-file CLI skill (not a package). This conftest loads
it via importlib so tests can import its classes/functions. The venv re-exec
guard is neutralized via WEB_SEARCH_NO_SKILL_VENV=1.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections import deque
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
os.environ.setdefault("WEB_SEARCH_NO_SKILL_VENV", "1")
os.environ.setdefault("WEB_SEARCH_NO_CACHE", "1")  # tests: no disk cache side-effects

_WS_PATH = SKILL_DIR / "web_search.py"


def _load_ws_module():
    if "web_search" in sys.modules:
        return sys.modules["web_search"]
    spec = importlib.util.spec_from_file_location("web_search", _WS_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["web_search"] = mod  # REQUIRED for frozen dataclass + typing
    spec.loader.exec_module(mod)
    return mod


ws = _load_ws_module()


# --------------------------------------------------------------------------- #
# P1.4 — Test doubles (fakes)
# --------------------------------------------------------------------------- #


class FakeFetcher:
    """Test double for the Fetcher Protocol. Returns scripted FetchResult
    sequence (or raises). Records every call for assertion."""

    def __init__(
        self,
        name: str = "fake",
        results: list | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.name = name
        self._results: deque = deque(results or [])
        self._raise = raise_exc
        self.calls: list[tuple[str, str]] = []

    def fetch(self, url: str, cookies: str = "") -> Any:
        self.calls.append((url, cookies))
        if self._raise:
            raise self._raise
        if self._results:
            return self._results.popleft()
        return None


class FakeExtractor:
    """Test double for the Extractor Protocol. Returns a fixed markdown string."""

    def __init__(
        self, output: str = "# Fake content\n\nExtracted text.", name: str = "fake_ext"
    ) -> None:
        self.name = name
        self._output = output
        self.calls: list[tuple[str, int]] = []

    def extract(self, html: str, url: str) -> str:
        self.calls.append((url, len(html)))
        return self._output


class FakeCookieStore:
    """In-memory cookie store matching the get/save interface ExtractCascade expects."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, host: str) -> str:
        return self._store.get(host, "")

    def save(self, host: str, cookies: str) -> None:
        self._store[host] = cookies


def make_fetch_result(
    html: str = "<html><body>Hello world</body></html>",
    content_type: str = "text/html",
    source: str = "fake",
) -> Any:
    """Build a FetchResult with sensible defaults for tests."""
    return ws.FetchResult(
        html=html, content_type=content_type, cookies="", source=source
    )
