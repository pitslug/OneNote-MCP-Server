"""Graph collection endpoints paginate via @odata.nextLink; we must follow it."""
import json

import pytest

import onenote_mcp_server as srv


async def test_graph_get_all_follows_next_link(monkeypatch):
    calls = []

    async def fake_request(endpoint, method="GET", data=None):
        calls.append(endpoint)
        if "$skiptoken" in endpoint:
            return {"value": [{"id": "3"}]}
        return {
            "value": [{"id": "1"}, {"id": "2"}],
            "@odata.nextLink": f"{srv.GRAPH_BASE_URL}/me/onenote/pages?$skiptoken=abc",
        }

    monkeypatch.setattr(srv, "make_graph_request", fake_request)

    items = await srv._graph_get_all("/me/onenote/pages")
    assert [i["id"] for i in items] == ["1", "2", "3"]
    # The absolute nextLink URL must be re-based onto the endpoint form.
    assert calls[1] == "/me/onenote/pages?$skiptoken=abc"


async def test_graph_get_all_rebases_nonverbatim_next_link(monkeypatch):
    """A nextLink whose host doesn't string-match GRAPH_BASE_URL must still be
    re-based onto an endpoint path, never concatenated onto the base URL."""
    calls = []

    async def fake_request(endpoint, method="GET", data=None):
        calls.append(endpoint)
        if "$skiptoken" in endpoint:
            return {"value": [{"id": "2"}]}
        return {
            "value": [{"id": "1"}],
            "@odata.nextLink": "https://GRAPH.Microsoft.com/v1.0/me/onenote/pages?$skiptoken=z",
        }

    monkeypatch.setattr(srv, "make_graph_request", fake_request)

    items = await srv._graph_get_all("/me/onenote/pages")
    assert [i["id"] for i in items] == ["1", "2"]
    assert calls[1] == "/me/onenote/pages?$skiptoken=z"


async def test_find_pages_sees_pages_beyond_first_batch(fresh_cache, monkeypatch):
    """A section with more pages than one Graph batch must not silently truncate."""

    async def fake_request(endpoint, method="GET", data=None):
        if endpoint == "/me/onenote/notebooks":
            return {"value": [{"id": "nb1", "displayName": srv.SIDEKICK_NOTEBOOK}]}
        if endpoint.startswith("/me/onenote/notebooks/nb1/sections"):
            return {"value": [{"id": "s1", "displayName": "Notes"}]}
        if "$skiptoken" in endpoint:
            return {"value": [{"id": "p3", "title": "Third", "lastModifiedDateTime": "2026-01-03"}]}
        if "/sections/s1/pages" in endpoint:
            return {
                "value": [
                    {"id": "p1", "title": "First", "lastModifiedDateTime": "2026-01-01"},
                    {"id": "p2", "title": "Second", "lastModifiedDateTime": "2026-01-02"},
                ],
                "@odata.nextLink": f"{srv.GRAPH_BASE_URL}/me/onenote/sections/s1/pages?$skiptoken=x",
            }
        raise AssertionError(f"unexpected endpoint {endpoint}")

    monkeypatch.setattr(srv, "make_graph_request", fake_request)

    out = json.loads(await srv._find_pages_impl())
    assert out["status"] == "success"
    assert out["count"] == 3
    assert {p["id"] for p in out["pages"]} == {"p1", "p2", "p3"}
