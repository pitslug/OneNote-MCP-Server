"""Two-tier notebook cache for the OneNote MCP server (see CACHE_DESIGN.md §2-§5).

Tiers:
    * in-memory (hot)  — recently returned PNGs / text, byte-bounded, lost on restart.
    * disk (persistent)— per-page folders on the mounted volume; survives restart.

Design choices:
    * **Validator = ``lastModifiedDateTime``.** A page's cached artifacts are keyed on
      it; when it changes the whole ``pages/<id>/`` folder is dropped and rebuilt, so a
      fresh PNG can never sit next to stale text (atomic per-page invalidation).
    * **Ink key also includes ``max_px`` + ``RASTER_VERSION``** so different render
      sizes coexist and bumping the rasterizer busts every PNG.
    * **Single-flight** per cache key (``asyncio.Lock``) prevents two concurrent
      renders of the same page under the HTTP server (thundering herd).
    * **Listings** use a short in-memory TTL (no cheaper validator); not persisted.
    * **Eviction** is filesystem-native: access touches the folder mtime; when total
      disk use exceeds the budget, whole oldest folders are removed (never a lone file).

No SQLite: LRU/size come straight from the filesystem, avoiding async-SQLite pitfalls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from collections import OrderedDict
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger("onenote.cache")


def _env_bool(name: str, default: bool) -> bool:
    return (os.getenv(name, str(default)).lower() in ("1", "true", "yes"))


def _sha(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


class NotebookCache:
    def __init__(
        self,
        root: Optional[str] = None,
        *,
        enabled: Optional[bool] = None,
        max_mb: Optional[int] = None,
        mem_mb: Optional[int] = None,
        raster_version: Optional[str] = None,
    ):
        self.enabled = _env_bool("ONENOTE_CACHE_ENABLED", True) if enabled is None else enabled
        self.root = Path(root or os.getenv("ONENOTE_DATA_CACHE", str(Path.home() / ".onenote_mcp_cache")))
        self.pages_dir = self.root / "pages"
        self.max_bytes = int((max_mb if max_mb is not None else int(os.getenv("ONENOTE_CACHE_MAX_MB", "500"))) * 1024 * 1024)
        self.mem_bytes = int((mem_mb if mem_mb is not None else int(os.getenv("ONENOTE_MEM_CACHE_MB", "128"))) * 1024 * 1024)
        self.raster_version = raster_version or os.getenv("ONENOTE_RASTER_VERSION", "1")

        self._locks: Dict[Tuple, asyncio.Lock] = {}
        # Running estimate of bytes on disk (None until first measured). Lets
        # enforce_budget skip the full tree walk while comfortably under budget.
        self._disk_used: Optional[int] = None
        # in-memory hot tier: key -> bytes/str ; byte-bounded LRU
        self._mem: "OrderedDict[Tuple, bytes]" = OrderedDict()
        self._mem_size = 0
        # listings TTL cache: key -> (expires_at, value)
        self._listings: Dict[str, Tuple[float, object]] = {}

        if self.enabled:
            try:
                self.pages_dir.mkdir(parents=True, exist_ok=True)
                self._chmod(self.root, 0o700)
                self._chmod(self.pages_dir, 0o700)
            except OSError as exc:
                logger.warning("Cache disabled — cannot create %s: %s", self.root, exc)
                self.enabled = False

    # ---- small helpers -------------------------------------------------
    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError:
            pass  # no-op on some mounts

    def _lock_for(self, key: Tuple) -> asyncio.Lock:
        # setdefault is atomic within a single event loop (no await mid-creation).
        return self._locks.setdefault(key, asyncio.Lock())

    def _page_dir(self, page_id: str) -> Path:
        return self.pages_dir / _sha(page_id)

    def _ink_name(self, last_mod: str, max_px: int) -> str:
        return f"ink_{_sha(last_mod, 12)}_{max_px}_r{self.raster_version}.png"

    def _read_meta(self, folder: Path) -> Optional[dict]:
        try:
            return json.loads((folder / "meta.json").read_text())
        except (OSError, ValueError):
            return None

    def _write_atomic(self, path: Path, data: bytes) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        self._chmod(path, 0o600)

    def _ensure_page_dir(self, page_id: str, last_mod: str, extra_meta: Optional[dict] = None) -> Path:
        folder = self._page_dir(page_id)
        meta = self._read_meta(folder)
        if meta and meta.get("last_mod") != last_mod:
            self._wipe_page(page_id)  # stale — drop the whole folder
            meta = None
        folder.mkdir(parents=True, exist_ok=True)
        self._chmod(folder, 0o700)
        if not meta:
            meta = {"page_id": page_id, "last_mod": last_mod}
            if extra_meta:
                meta.update(extra_meta)
            self._write_atomic(folder / "meta.json", json.dumps(meta).encode("utf-8"))
        return folder

    def _wipe_page(self, page_id: str) -> None:
        folder = self._page_dir(page_id)
        if self._disk_used is not None and folder.exists():
            self._disk_used = max(0, self._disk_used - self._dir_size(folder))
        shutil.rmtree(folder, ignore_errors=True)

    @staticmethod
    def _touch(folder: Path) -> None:
        try:
            os.utime(folder, None)  # bump mtime for LRU
        except OSError:
            pass

    # Sync bundles for the disk tier, run via asyncio.to_thread so file I/O
    # (reads, atomic writes, budget walks) never stalls the event loop.
    def _disk_lookup(self, page_id: str, last_mod: str, name: str) -> Optional[bytes]:
        folder = self._page_dir(page_id)
        meta = self._read_meta(folder)
        fpath = folder / name
        if meta and meta.get("last_mod") == last_mod and fpath.exists():
            data = fpath.read_bytes()
            self._touch(folder)
            return data
        return None

    def _disk_store(self, page_id: str, last_mod: str, name: str, data: bytes) -> None:
        folder = self._ensure_page_dir(page_id, last_mod)
        self._write_atomic(folder / name, data)
        self._touch(folder)
        if self._disk_used is not None:
            self._disk_used += len(data)
        self.enforce_budget()

    # ---- memory tier ---------------------------------------------------
    def _mem_get(self, key: Tuple) -> Optional[bytes]:
        val = self._mem.get(key)
        if val is not None:
            self._mem.move_to_end(key)
        return val

    def _mem_put(self, key: Tuple, val: bytes) -> None:
        if key in self._mem:
            self._mem_size -= len(self._mem[key])
        self._mem[key] = val
        self._mem.move_to_end(key)
        self._mem_size += len(val)
        while self._mem_size > self.mem_bytes and self._mem:
            _k, _v = self._mem.popitem(last=False)
            self._mem_size -= len(_v)

    # ---- disk budget / eviction ---------------------------------------
    def _dir_size(self, path: Path) -> int:
        total = 0
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
        return total

    def enforce_budget(self) -> None:
        if not self.enabled:
            return
        # Fast path: the running estimate says we're under budget - skip the walk.
        # (Estimate drifts only downward-safe: writes/wipes update it, so a full
        # rescan happens no later than the first write after crossing the budget.)
        if self._disk_used is not None and self._disk_used <= self.max_bytes:
            return
        try:
            folders = [d for d in self.pages_dir.iterdir() if d.is_dir()]
        except OSError:
            return
        sizes = {d: self._dir_size(d) for d in folders}
        total = sum(sizes.values())
        if total > self.max_bytes:
            # evict whole folders, oldest mtime first
            for d in sorted(folders, key=lambda p: p.stat().st_mtime):
                if total <= self.max_bytes:
                    break
                shutil.rmtree(d, ignore_errors=True)
                total -= sizes[d]
                logger.info("Cache eviction: dropped %s (%d bytes)", d.name, sizes[d])
        self._disk_used = total

    # ---- public: ink PNG ----------------------------------------------
    async def ink_png(
        self,
        page_id: str,
        max_px: int,
        last_mod: Optional[str],
        produce: Callable[[], Awaitable[bytes]],
    ) -> bytes:
        """Return the rasterized ink PNG, from cache when fresh else via ``produce``."""
        if not self.enabled or not last_mod:
            return await produce()  # no validator -> don't cache

        mem_key = ("ink", page_id, last_mod, max_px, self.raster_version)
        # Lock key deliberately excludes last_mod: it churns on every page edit and
        # would leak one Lock per revision. Per (page, size) still gives single-flight.
        async with self._lock_for(("ink", page_id, max_px)):
            hit = self._mem_get(mem_key)
            if hit is not None:
                return hit

            name = self._ink_name(last_mod, max_px)
            data = await asyncio.to_thread(self._disk_lookup, page_id, last_mod, name)
            if data is not None:
                self._mem_put(mem_key, data)
                return data

            data = await produce()
            await asyncio.to_thread(self._disk_store, page_id, last_mod, name, data)
            self._mem_put(mem_key, data)
            return data

    # ---- public: page text --------------------------------------------
    async def page_text(
        self,
        page_id: str,
        last_mod: Optional[str],
        produce: Callable[[], Awaitable[str]],
    ) -> str:
        if not self.enabled or not last_mod:
            return await produce()

        mem_key = ("text", page_id, last_mod)
        async with self._lock_for(("text", page_id)):  # no last_mod: see ink_png
            hit = self._mem_get(mem_key)
            if hit is not None:
                return hit.decode("utf-8")

            raw = await asyncio.to_thread(self._disk_lookup, page_id, last_mod, "text.txt")
            if raw is not None:
                self._mem_put(mem_key, raw)
                return raw.decode("utf-8")

            text = await produce()
            await asyncio.to_thread(self._disk_store, page_id, last_mod, "text.txt", text.encode("utf-8"))
            self._mem_put(mem_key, text.encode("utf-8"))
            return text

    # ---- public: listings (TTL) ---------------------------------------
    async def listing(
        self,
        key: str,
        ttl: Optional[float],
        produce: Callable[[], Awaitable[object]],
    ) -> object:
        if not self.enabled:
            return await produce()
        ttl = float(os.getenv("ONENOTE_LISTING_TTL", "60")) if ttl is None else ttl
        now = time.time()
        # Sweep expired entries so churning keys (find queries, lastmod probes)
        # don't accumulate forever.
        for k in [k for k, (exp, _) in self._listings.items() if exp <= now]:
            del self._listings[k]
        cached = self._listings.get(key)
        if cached and cached[0] > now:
            return cached[1]
        async with self._lock_for(("listing", key)):
            cached = self._listings.get(key)  # re-check after acquiring
            if cached and cached[0] > time.time():
                return cached[1]
            value = await produce()
            self._listings[key] = (time.time() + ttl, value)
            return value

    # ---- public: invalidation -----------------------------------------
    def invalidate_page(self, page_id: str) -> None:
        self._wipe_page(page_id)
        for k in [k for k in self._mem if len(k) > 1 and k[1] == page_id]:
            self._mem_size -= len(self._mem.pop(k))

    def invalidate_listings(self) -> None:
        """Drop all listing caches — call after any write-back creates a page."""
        self._listings.clear()


# Module-level singleton, configured from env.
CACHE = NotebookCache()
