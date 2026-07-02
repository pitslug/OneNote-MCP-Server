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
