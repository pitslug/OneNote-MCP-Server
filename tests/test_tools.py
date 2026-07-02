"""Tool-level behavior: write-path cache invalidation and render clamping."""
import json

import httpx
import pytest

import onenote_mcp_server as srv
from inkml_raster import MAX_RENDER_PX

from conftest import FakeResponse

INK_HTML = """<html><body>
<inkml:ink xmlns:inkml="http://www.w3.org/2003/InkML">
  <inkml:trace>0 0 100, 500 500 100, 1000 300 100</inkml:trace>
</inkml:ink>
</body></html>"""


@pytest.fixture()
def authed(monkeypatch):
    async def fake_token():
        return "tok-123"

    monkeypatch.setattr(srv, "get_access_token", fake_token)


async def test_update_page_content_invalidates_caches(fresh_cache, authed, monkeypatch):
    """After a PATCH, cached page content and listings must be dropped."""
    page_id = "page-42"

    async def old_text():
        return "<p>old content</p>"

    await fresh_cache.page_text(page_id, "T1", old_text)
    await fresh_cache.listing(f"lastmod:{page_id}", 60, _const("T1"))
    await fresh_cache.listing("find::25:False", 60, _const("{}"))
    assert fresh_cache._page_dir(page_id).exists()

    async def fake_patch(self, url, headers=None, json=None):
        return FakeResponse(204)

    monkeypatch.setattr(httpx.AsyncClient, "patch", fake_patch)

    out = json.loads(await srv.update_page_content.fn(page_id, "<p>new</p>"))
    assert out["status"] == "success"

    assert not fresh_cache._page_dir(page_id).exists(), "page cache not invalidated"
    assert fresh_cache._listings == {}, "listing caches not invalidated"


async def test_failed_update_does_not_invalidate_caches(fresh_cache, authed, monkeypatch):
    """A rejected PATCH means the page didn't change - caches must survive."""
    page_id = "page-43"

    async def old_text():
        return "<p>old content</p>"

    await fresh_cache.page_text(page_id, "T1", old_text)
    await fresh_cache.listing("find::25:False", 60, _const("{}"))

    async def fake_patch(self, url, headers=None, json=None):
        return FakeResponse(403, text="Forbidden")

    monkeypatch.setattr(httpx.AsyncClient, "patch", fake_patch)

    out = await srv.update_page_content.fn(page_id, "<p>new</p>")
    assert "Error updating page" in out
    assert fresh_cache._page_dir(page_id).exists()
    assert fresh_cache._listings != {}


def _const(value):
    async def produce():
        return value

    return produce


async def test_update_page_title_rename_uses_replace(fresh_cache, authed, monkeypatch):
    """Graph rejects APPEND on the title target (20141); renames must send replace."""
    captured = {}

    async def fake_patch(self, url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse(204)

    monkeypatch.setattr(httpx.AsyncClient, "patch", fake_patch)

    out = json.loads(await srv.update_page_content.fn("page-1", "New Title", target_element="title"))
    assert out["status"] == "success"
    assert captured["payload"][0]["target"] == "title"
    assert captured["payload"][0]["action"] == "replace"


async def test_update_page_body_defaults_to_append(fresh_cache, authed, monkeypatch):
    """Body updates keep the historical append behavior when no action is given."""
    captured = {}

    async def fake_patch(self, url, headers=None, json=None):
        captured["payload"] = json
        return FakeResponse(204)

    monkeypatch.setattr(httpx.AsyncClient, "patch", fake_patch)

    out = json.loads(await srv.update_page_content.fn("page-1", "<p>x</p>"))
    assert out["status"] == "success"
    assert captured["payload"][0]["target"] == "body"
    assert captured["payload"][0]["action"] == "append"


async def test_update_page_rejects_unknown_action(fresh_cache, authed, monkeypatch):
    """An action Graph doesn't support is refused before any request is made."""

    async def fake_patch(self, url, headers=None, json=None):
        raise AssertionError("no request should be made for an invalid action")

    monkeypatch.setattr(httpx.AsyncClient, "patch", fake_patch)

    out = await srv.update_page_content.fn("page-1", "<p>x</p>", action="delete")
    assert "Error updating page content" in out
    assert "action" in out


# Graph's answer on consumer/personal notebooks: copyToSection is not implemented.
_UNSUPPORTED_501 = dict(
    status_code=501,
    json_data={"error": {"code": "20111", "message": "OData Feature not implemented"}},
    text='{"error":{"code":"20111","message":"OData Feature not implemented"}}',
)


async def test_copy_page_unsupported_on_personal_notebook(fresh_cache, authed, monkeypatch):
    """A 501/20111 from copyToSection becomes an actionable message, not a raw error."""

    async def fake_raw(method, path, params=None, json_data=None):
        return FakeResponse(**_UNSUPPORTED_501)

    monkeypatch.setattr(srv, "_graph_request_raw", fake_raw)

    out = json.loads(await srv.copy_page.fn("page-1", "sec-1"))
    assert out["status"] == "unsupported"
    assert "personal OneDrive notebooks" in out["error"]


async def test_move_page_unsupported_aborts_without_delete(fresh_cache, authed, monkeypatch):
    """When the copy half is unsupported, move must report it and never delete."""
    graph_calls = []

    async def fake_ink_guard(page_id, action):
        return None

    async def fake_raw(method, path, params=None, json_data=None):
        return FakeResponse(**_UNSUPPORTED_501)

    async def fake_graph(endpoint, method="GET", data=None):
        graph_calls.append((method, endpoint))
        return {}

    monkeypatch.setattr(srv, "_ink_guard", fake_ink_guard)
    monkeypatch.setattr(srv, "_graph_request_raw", fake_raw)
    monkeypatch.setattr(srv, "make_graph_request", fake_graph)

    out = json.loads(await srv.move_page.fn("page-1", "sec-1"))
    assert out["status"] == "unsupported"
    assert "personal OneDrive notebooks" in out["error"]
    deletes = [c for c in graph_calls if c[0] == "DELETE"]
    assert deletes == [], f"original page was deleted after a failed copy: {deletes}"


async def test_render_page_ink_clamps_cache_key(fresh_cache, authed, monkeypatch):
    """An absurd max_px must be clamped before it becomes a cache key."""

    async def fake_last_mod(page_id):
        return "2026-01-01T00:00:00Z"

    async def fake_raw(path, params=None):
        return FakeResponse(200, text=INK_HTML, headers={"content-type": "text/html"})

    monkeypatch.setattr(srv, "_page_last_mod", fake_last_mod)
    monkeypatch.setattr(srv, "_graph_get_raw", fake_raw)

    img = await srv.render_page_ink.fn("page-ink", max_px=10**9)
    assert img.data  # got a PNG back

    cached = list(fresh_cache._page_dir("page-ink").glob("ink_*.png"))
    assert len(cached) == 1
    assert f"_{MAX_RENDER_PX}_" in cached[0].name, f"unclamped cache key: {cached[0].name}"
