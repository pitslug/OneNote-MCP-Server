"""Shared test setup.

Env vars are pinned BEFORE onenote_mcp_server / notebook_cache are imported so the
module-level singletons (CACHE, token cache paths) never touch the real home dir.
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="onenote_mcp_tests_"))
os.environ["ONENOTE_DATA_CACHE"] = str(_TMP / "cache")
os.environ["ONENOTE_TOKEN_CACHE"] = str(_TMP / "token_cache.json")
os.environ["ONENOTE_CACHE_TOKENS"] = "false"
os.environ.setdefault("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000000")

import pytest

from notebook_cache import NotebookCache


@pytest.fixture()
def fresh_cache(tmp_path, monkeypatch):
    """A NotebookCache rooted in tmp_path, installed as the module singleton."""
    cache = NotebookCache(root=str(tmp_path / "nbcache"), enabled=True, max_mb=500, mem_mb=8)
    import onenote_mcp_server as srv
    import notebook_cache
    monkeypatch.setattr(srv, "CACHE", cache)
    monkeypatch.setattr(notebook_cache, "CACHE", cache)
    return cache


class FakeResponse:
    """Minimal stand-in for httpx.Response."""

    def __init__(self, status_code=200, json_data=None, text="", content=b"", headers=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.content = content or text.encode("utf-8")
        self.headers = headers or {}

    def json(self):
        return self._json
