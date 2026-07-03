#!/usr/bin/env python3
"""Container / production entrypoint for the OneNote MCP server.

Responsibilities (see CACHE_DESIGN.md §1, §8):

* Resolve ``*_FILE`` Docker secrets into env before anything reads them.
* Seed a read-only token cache into the writable volume on first run (optional).
* ``--auth`` one-shot: run the device-code flow, persist, exit (optional convenience).
* Otherwise run the MCP server:
    - ``ONENOTE_TRANSPORT=stdio``            -> stdio (local Claude Desktop dev)
    - ``ONENOTE_TRANSPORT=streamable-http``  -> HTTP for Docker/Traefik (default)
* HTTP mode boots clean and NEVER blocks on auth. A bearer-token gate
  (``ONENOTE_API_TOKEN``) protects the MCP endpoint; ``/healthz`` and the optional
  ``/auth`` sign-in page are exempt so Traefik/Pocket-ID can front them.

The heavy lifting (tools, MSAL, Graph) lives in ``onenote_mcp_server``; importing it
registers all tools on the shared ``mcp`` instance without running its stdio ``main()``.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import shutil
import sys
from pathlib import Path

from secrets_env import load_file_backed_env

logging.basicConfig(level=os.getenv("ONENOTE_LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("onenote.entry")

# Cosmetic: silence a deprecation from a transitive dep we don't drive ---
# fastmcp imports authlib.jose (upstream will move to joserfc); we never call it.
import warnings
try:
    from authlib.deprecate import AuthlibDeprecationWarning
    warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)
except Exception:  # authlib absent or relocated — leave default warnings behavior
    pass

# 1) Secrets must resolve before onenote_mcp_server reads AZURE_CLIENT_ID etc.
load_file_backed_env()

# Import AFTER the authlib warning filter above.
from fastmcp.server.auth import AccessToken  # noqa: E402
from fastmcp.server.auth.oidc_proxy import OIDCProxy  # noqa: E402


def _static_access_token(token: str, static_token: str | None,
                         scopes: list | None = None) -> AccessToken | None:
    """AccessToken for the static ONENOTE_API_TOKEN bearer, else None.

    scopes should mirror the provider's required_scopes so the scope-enforcement
    middleware accepts the static bearer alongside OAuth tokens.
    """
    if not token or not static_token:
        return None
    if hmac.compare_digest(token, static_token):
        return AccessToken(token=token, client_id="onenote-static-bearer",
                           scopes=list(scopes or []), expires_at=None)
    return None


class DualAuthOIDCProxy(OIDCProxy):
    """OIDC proxy that ALSO accepts the static ONENOTE_API_TOKEN bearer.

    claude.ai custom connectors only speak spec OAuth (authorization code + PKCE
    with dynamic client registration) - no static headers. Pocket-ID has no DCR,
    so the proxy fronts it and presents DCR to clients. Existing Claude Code
    clients keep sending the static bearer; it's accepted here before falling
    back to normal OAuth token verification.
    """

    def __init__(self, *, static_token: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._static_token = static_token or None

    async def load_access_token(self, token: str):
        static = _static_access_token(token, self._static_token,
                                      scopes=self.required_scopes)
        if static is not None:
            return static
        return await super().load_access_token(token)


def _build_auth_provider(static_token: str | None):
    """DualAuthOIDCProxy from ONENOTE_OIDC_* env, or None when not configured.

    Presence of ONENOTE_OIDC_CONFIG_URL turns connector OAuth on; a partial
    config raises instead of booting with the endpoint ungated.
    """
    config_url = (os.getenv("ONENOTE_OIDC_CONFIG_URL") or "").strip()
    if not config_url:
        return None
    client_id = (os.getenv("ONENOTE_OIDC_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("ONENOTE_OIDC_CLIENT_SECRET") or "").strip()
    base_url = (os.getenv("ONENOTE_PUBLIC_BASE_URL") or "").strip()
    missing = [name for name, value in (
        ("ONENOTE_OIDC_CLIENT_ID", client_id),
        ("ONENOTE_OIDC_CLIENT_SECRET", client_secret),
        ("ONENOTE_PUBLIC_BASE_URL", base_url),
    ) if not value]
    if missing:
        raise RuntimeError(
            "ONENOTE_OIDC_CONFIG_URL is set but connector OAuth is incomplete; "
            "missing: " + ", ".join(missing))
    # Scopes requested from the IdP (advertised to clients via .well-known and
    # forwarded upstream). Pocket-ID rejects an authorize request without any
    # scope ("Scope is required"), so this must never be empty.
    scopes = (os.getenv("ONENOTE_OIDC_SCOPES") or "openid profile email").replace(",", " ").split()
    return DualAuthOIDCProxy(
        config_url=config_url,
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        static_token=static_token,
        required_scopes=scopes,
    )


def _seed_token_cache() -> None:
    """Copy a read-only seed cache into the writable working path on first run.

    Docker secrets mount read-only, but MSAL rewrites the cache on refresh. So the
    working cache lives on a writable volume; an optional seed secret bootstraps it.
    """
    seed = (os.getenv("ONENOTE_TOKEN_CACHE_SEED_FILE") or "").strip()
    if not seed:
        return
    working = os.getenv("ONENOTE_TOKEN_CACHE") or str(Path.home() / ".onenote_mcp_token_cache.json")
    working_path = Path(working)
    if working_path.exists():
        logger.info("Token cache already present at %s; ignoring seed", working_path)
        return
    try:
        working_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed, working_path)
        try:
            working_path.chmod(0o600)
        except OSError:
            pass
        logger.info("Seeded token cache from %s -> %s", seed, working_path)
    except OSError as exc:
        logger.warning("Could not seed token cache from %s: %s", seed, exc)


def run_auth() -> int:
    """One-shot interactive device-code sign-in. Prints the code, blocks, persists."""
    import onenote_mcp_server as srv

    try:
        app = srv.get_msal_app()
    except Exception as exc:  # AZURE_CLIENT_ID unset, etc.
        print(f"Cannot start auth: {exc}", file=sys.stderr)
        return 2

    if app.get_accounts():
        print("Already authenticated (cached account present). Nothing to do.")
        return 0

    flow = app.initiate_device_flow(scopes=srv.SCOPES)
    if "user_code" not in flow:
        print(f"Device flow failed: {flow.get('error_description', 'unknown error')}", file=sys.stderr)
        return 1

    print("\n" + flow["message"] + "\n", flush=True)  # "Go to ...devicelogin and enter CODE"
    result = app.acquire_token_by_device_flow(flow)  # blocks until you complete sign-in

    if "access_token" not in result:
        print(f"Authentication failed: {result.get('error_description', 'unknown error')}", file=sys.stderr)
        return 1

    srv._persist_cache()
    print("[ok] Signed in; token cache persisted. You can start the server now.")
    return 0


class Gateway:
    """Pure-ASGI wrapper: health + optional /auth + bearer gate over the MCP app.

    Wrapping (not re-hosting) preserves the inner app's lifespan/session-manager --
    non-HTTP scopes (``lifespan``, ``websocket``) pass straight through.
    """

    def __init__(self, app, token: str | None, auth_route: bool):
        self._app = app
        self._token = token or None
        self._auth_route = auth_route
        # Single-flight guard for /auth: each pending device flow parks a worker
        # thread for up to ~15 min, so unauthenticated hits must not stack them up.
        self._auth_pending = False
        # Strong refs to background completion tasks (asyncio only weakly refs tasks).
        self._auth_tasks: set = set()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")

        if path in ("/healthz", "/health"):
            await self._text(send, 200, "ok")
            return

        if self._auth_route and path == "/auth":
            await self._handle_auth(send)
            return

        if self._token and not self._authorized(scope):
            await self._text(send, 401, "unauthorized", extra_headers=[(b"www-authenticate", b"Bearer")])
            return

        await self._app(scope, receive, send)

    def _authorized(self, scope) -> bool:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                got = value.decode("latin-1", "replace").strip()
                prefix = "bearer "
                if got.lower().startswith(prefix):
                    return hmac.compare_digest(got[len(prefix):].strip(), self._token)
                return False
        return False

    async def _handle_auth(self, send):
        """Best-effort out-of-band device-code sign-in page (front with Pocket-ID)."""
        import onenote_mcp_server as srv

        try:
            authed = await srv.ensure_valid_token()
        except Exception:
            authed = False
        if authed:
            await self._text(send, 200, "Already authenticated. The server is ready.")
            return

        if self._auth_pending:
            await self._text(send, 200, "A sign-in is already in progress. Complete it "
                                        "on the device, or retry once it expires.")
            return

        # Claim the slot BEFORE the initiate network round-trip: a concurrent burst
        # arriving during that await must not each start (and park) a device flow.
        # Released by _complete's finally once scheduled, or the failure paths below.
        self._auth_pending = True
        scheduled = False
        try:
            app = srv.get_msal_app()
            flow = await asyncio.to_thread(app.initiate_device_flow, scopes=srv.SCOPES)
            if "user_code" not in flow:
                self._auth_pending = False
                await self._text(send, 500, "Could not start device flow.")
                return

            async def _complete():
                try:
                    result = await asyncio.to_thread(app.acquire_token_by_device_flow, flow)
                    if "access_token" in result:
                        srv._persist_cache()
                        logger.info("/auth: device sign-in completed and cache persisted")
                    else:
                        logger.warning("/auth: device sign-in failed: %s", result.get("error_description"))
                finally:
                    self._auth_pending = False

            task = asyncio.ensure_future(_complete())
            scheduled = True
            self._auth_tasks.add(task)
            task.add_done_callback(self._auth_tasks.discard)
            msg = (
                f"Go to {flow['verification_uri']} and enter code: {flow['user_code']}\n\n"
                "This page started the sign-in; complete it on that device. "
                "The server picks up the token automatically when you finish."
            )
            await self._text(send, 200, msg)
        except Exception as exc:  # noqa: BLE001
            if not scheduled:  # once _complete owns the slot, its finally releases it
                self._auth_pending = False
            await self._text(send, 500, f"Auth error: {exc}")

    @staticmethod
    async def _text(send, status: int, body: str, extra_headers=None):
        payload = body.encode("utf-8")
        headers = [(b"content-type", b"text/plain; charset=utf-8"),
                   (b"content-length", str(len(payload)).encode())]
        if extra_headers:
            headers.extend(extra_headers)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})


def _log_auth_status() -> None:
    import onenote_mcp_server as srv
    try:
        ok = asyncio.run(srv.ensure_valid_token())
    except Exception as exc:  # noqa: BLE001
        logger.info("Auth status unknown at boot (%s); serving anyway", exc)
        return
    if ok:
        logger.info("Auth: token present and refreshable - fully operational.")
    else:
        logger.warning("Auth: NO token yet - server is up; sign in via the "
                       "start_authentication tool (or /auth) to activate OneNote access.")


def build_http_app():
    """Return the gated ASGI app around the MCP streamable-HTTP endpoint.

    Two auth modes:
    * default: static bearer gate in the Gateway (ONENOTE_API_TOKEN).
    * ONENOTE_OIDC_* set: FastMCP OAuth (OIDC proxy, for claude.ai custom
      connectors). The Gateway's gate is disabled so the OAuth/.well-known
      routes stay reachable; the MCP endpoint itself is then enforced by
      FastMCP's auth middleware, where the static token remains valid via
      DualAuthOIDCProxy (Claude Code keeps working unchanged).
    """
    import onenote_mcp_server as srv

    mount_path = os.getenv("ONENOTE_HTTP_PATH", "/mcp")
    token = (os.getenv("ONENOTE_API_TOKEN") or "").strip()
    auth_provider = _build_auth_provider(token or None)
    srv.mcp.auth = auth_provider  # None -> plain app, exactly the old behavior
    inner = srv.mcp.http_app(path=mount_path)  # Starlette app w/ session-manager lifespan
    auth_route = (os.getenv("ONENOTE_ENABLE_AUTH_ROUTE", "true").lower() in ("1", "true", "yes"))

    if auth_provider is not None:
        logger.info("Connector OAuth enabled (OIDC proxy at %s); static bearer "
                    "still accepted for existing clients.",
                    os.getenv("ONENOTE_OIDC_CONFIG_URL"))
        gate_token = None  # FastMCP auth middleware enforces from here on
    else:
        gate_token = token
        if not gate_token:
            logger.warning("ONENOTE_API_TOKEN not set - MCP endpoint is UNGATED at the app "
                           "layer (rely on Traefik/network). Set it for defense in depth.")
    return Gateway(inner, gate_token, auth_route), inner


def run_http() -> int:
    import uvicorn
    app, _inner = build_http_app()
    host = os.getenv("ONENOTE_HTTP_HOST", "0.0.0.0")
    port = int(os.getenv("ONENOTE_HTTP_PORT", "8400"))
    _log_auth_status()
    logger.info("Serving MCP over streamable-HTTP on %s:%s (path %s)",
                host, port, os.getenv("ONENOTE_HTTP_PATH", "/mcp"))
    # proxy_headers + forwarded_allow_ips: trust Traefik's X-Forwarded-Proto so
    # trailing-slash redirects (/mcp -> /mcp/) stay https instead of downgrading to
    # http. An http downgrade makes clients drop the Authorization header (401).
    uvicorn.run(app, host=host, port=port, ws="none",
                proxy_headers=True,
                forwarded_allow_ips=os.getenv("ONENOTE_FORWARDED_IPS", "*"),
                log_level=os.getenv("ONENOTE_LOG_LEVEL", "info").lower())
    return 0


def run_stdio() -> int:
    import onenote_mcp_server as srv
    logger.info("Serving MCP over stdio (local dev).")
    srv.mcp.run()
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _seed_token_cache()

    if "--auth" in argv:
        return run_auth()

    transport = os.getenv("ONENOTE_TRANSPORT", "streamable-http").lower()
    if transport == "stdio":
        return run_stdio()
    return run_http()


if __name__ == "__main__":
    raise SystemExit(main())
