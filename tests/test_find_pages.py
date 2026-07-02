"""find_pages: error results must not be cached; previews fetch concurrently."""
import asyncio
import json

import pytest

import onenote_mcp_server as srv

from conftest import FakeResponse


def _graph_fake(notebook_exists):
    async def fake_request(endpoint, method="GET", data=None):
        if endpoint == "/me/onenote/notebooks":
            if notebook_exists():
                return {"value": [{"id": "nb1", "displayName": srv.SIDEKICK_NOTEBOOK}]}
            return {"value": []}
        if endpoint.startswith("/me/onenote/notebooks/nb1/sections"):
            return {"value": [{"id": "s1", "displayName": "Notes"}]}
        if "/sections/s1/pages" in endpoint:
            return {
                "value": [
                    {"id": f"p{i}", "title": f"Page {i}", "lastModifiedDateTime": f"2026-01-0{i}"}
                    for i in range(1, 5)
                ]
            }
        raise AssertionError(f"unexpected endpoint {endpoint}")

    return fake_request


async def test_notebook_not_found_error_is_not_cached(fresh_cache, monkeypatch):
    """A transient 'notebook not found' must not be served from cache for the TTL."""
    exists = False
    monkeypatch.setattr(srv, "make_graph_request", _graph_fake(lambda: exists))

    first = json.loads(await srv.find_pages.fn())
    assert first["status"] == "error"

    exists = True  # notebook appears (created / permissions fixed)
    second = json.loads(await srv.find_pages.fn())
    assert second["status"] == "success", "stale error response served from cache"
    assert second["count"] == 4


async def test_previews_fetch_concurrently(fresh_cache, monkeypatch):
    monkeypatch.setattr(srv, "make_graph_request", _graph_fake(lambda: True))

    inflight = 0
    max_inflight = 0

    async def fake_raw(path, params=None):
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return FakeResponse(
            200, text="<html><body><p>preview text</p></body></html>",
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(srv, "_graph_get_raw", fake_raw)

    out = json.loads(await srv.find_pages.fn(include_preview=True))
    assert out["status"] == "success"
    assert all(p.get("preview") == "preview text" for p in out["pages"])
    assert max_inflight >= 2, "previews fetched sequentially"
