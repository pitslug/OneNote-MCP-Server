"""Tests for the two-tier NotebookCache."""
import asyncio
import json

import pytest

from notebook_cache import NotebookCache


def make_cache(tmp_path, **kw) -> NotebookCache:
    kw.setdefault("enabled", True)
    kw.setdefault("max_mb", 500)
    kw.setdefault("mem_mb", 8)
    return NotebookCache(root=str(tmp_path / "nb"), **kw)


# ---- characterization: existing behavior we must not break -----------------

async def test_ink_png_caches_by_last_mod(tmp_path):
    cache = make_cache(tmp_path)
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        return b"PNGDATA"

    a = await cache.ink_png("p1", 1200, "2026-01-01T00:00:00Z", produce)
    b = await cache.ink_png("p1", 1200, "2026-01-01T00:00:00Z", produce)
    assert a == b == b"PNGDATA"
    assert calls == 1  # second hit served from cache


async def test_ink_png_new_last_mod_invalidates(tmp_path):
    cache = make_cache(tmp_path)

    async def v1():
        return b"OLD"

    async def v2():
        return b"NEW"

    await cache.ink_png("p1", 1200, "T1", v1)
    out = await cache.ink_png("p1", 1200, "T2", v2)
    assert out == b"NEW"


async def test_page_text_roundtrip(tmp_path):
    cache = make_cache(tmp_path)

    async def produce():
        return "hello <b>world</b>"

    a = await cache.page_text("p2", "T1", produce)
    b = await cache.page_text("p2", "T1", produce)
    assert a == b == "hello <b>world</b>"


async def test_disk_survives_memory_flush(tmp_path):
    cache = make_cache(tmp_path)

    async def produce():
        return b"X" * 100

    await cache.ink_png("p3", 800, "T1", produce)
    cache._mem.clear()
    cache._mem_size = 0

    async def boom():
        raise AssertionError("should have hit disk")

    out = await cache.ink_png("p3", 800, "T1", boom)
    assert out == b"X" * 100


async def test_invalidate_page_drops_disk_and_memory(tmp_path):
    cache = make_cache(tmp_path)

    async def produce():
        return b"DATA"

    await cache.ink_png("p4", 800, "T1", produce)
    cache.invalidate_page("p4")
    assert not cache._page_dir("p4").exists()
    assert not any(k[1] == "p4" for k in cache._mem)


async def test_eviction_drops_oldest_folder_over_budget(tmp_path):
    cache = make_cache(tmp_path)
    cache.max_bytes = 150_000

    async def big():
        return b"A" * 100_000

    await cache.ink_png("old-page", 800, "T1", big)
    await asyncio.sleep(0.05)  # ensure distinct mtimes for LRU ordering
    await cache.ink_png("new-page", 800, "T1", big)

    assert not cache._page_dir("old-page").exists()
    assert cache._page_dir("new-page").exists()


async def test_listing_ttl_caches(tmp_path):
    cache = make_cache(tmp_path)
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        return json.dumps({"n": calls})

    a = await cache.listing("k1", 60, produce)
    b = await cache.listing("k1", 60, produce)
    assert a == b
    assert calls == 1


# ---- leak fixes -------------------------------------------------------------

async def test_expired_listings_are_pruned(tmp_path):
    """Expired listing entries must not accumulate forever."""
    cache = make_cache(tmp_path)

    async def produce():
        return "v"

    for i in range(20):
        await cache.listing(f"short:{i}", 0.01, produce)
    await asyncio.sleep(0.05)
    await cache.listing("fresh", 60, produce)

    assert len(cache._listings) <= 2  # the fresh key (and at most the newest short one)


async def test_expired_listing_locks_are_pruned_too(tmp_path):
    """The per-listing locks must be swept along with their expired entries."""
    cache = make_cache(tmp_path)

    async def produce():
        return "v"

    for i in range(20):
        await cache.listing(f"short:{i}", 0.01, produce)
    await asyncio.sleep(0.05)
    await cache.listing("fresh", 60, produce)

    listing_locks = [k for k in cache._locks if k[0] == "listing"]
    assert len(listing_locks) <= 2


async def test_disk_write_failure_still_returns_data(tmp_path, monkeypatch):
    """The disk tier is best-effort: a failed write must not fail the tool call."""
    cache = make_cache(tmp_path)

    def broken_write(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(cache, "_write_atomic", broken_write)

    async def produce():
        return b"FRESH"

    out = await cache.ink_png("p9", 800, "T1", produce)
    assert out == b"FRESH"


async def test_locks_do_not_grow_per_last_mod(tmp_path):
    """Repeated renders of the same page with churning last_mod must reuse locks."""
    cache = make_cache(tmp_path)

    async def produce():
        return b"D"

    for i in range(5):
        await cache.ink_png("same-page", 800, f"T{i}", produce)

    assert len(cache._locks) <= 1
