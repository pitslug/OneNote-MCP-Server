#!/usr/bin/env python3
"""
OneNote MCP Server

A Model Context Protocol server for Microsoft OneNote integration.
This allows Claude Desktop to read and interact with OneNote notebooks.
"""

import os
import re
import random
import asyncio
import json
import logging
import html
from email import message_from_bytes
from email.policy import default as email_default_policy
from urllib.parse import urlsplit
from typing import List, Dict, Any, Optional
from pathlib import Path
import time
import atexit
from msal import PublicClientApplication, SerializableTokenCache
import httpx
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from inkml_raster import rasterize_inkml, MIN_RENDER_PX, MAX_RENDER_PX
from notebook_cache import CACHE

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastMCP instance
mcp = FastMCP("OneNote MCP Server")

# Microsoft Graph API constants
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
# OneNote endpoints can be slow, especially the first hit that provisions the
# notebook store. Default httpx timeout (5s) is too tight; override generously.
GRAPH_TIMEOUT = float(os.getenv("ONENOTE_GRAPH_TIMEOUT", "60"))
SCOPES = [
    "https://graph.microsoft.com/Notes.Read",
    "https://graph.microsoft.com/Notes.ReadWrite",
    "https://graph.microsoft.com/User.Read"
]

# Authentication configuration
# Personal Microsoft accounts use the `consumers` authority (matches the verified probe).
# Override with AZURE_AUTHORITY if this ever needs to target a work/school tenant.
AUTHORITY = os.getenv("AZURE_AUTHORITY", "https://login.microsoftonline.com/consumers")

# Token cache configuration
TOKEN_CACHE_ENABLED = os.getenv("ONENOTE_CACHE_TOKENS", "true").lower() in ("true", "1", "yes")
# Env-configurable so a Docker container can point it at a mounted secret/volume.
TOKEN_CACHE_FILE = Path(os.getenv("ONENOTE_TOKEN_CACHE", str(Path.home() / ".onenote_mcp_token_cache.json")))

# Global variables for authentication
access_token: Optional[str] = None
token_expires_at: Optional[float] = None
msal_app: Optional[PublicClientApplication] = None
# MSAL owns refresh-token storage; we just persist its serializable cache to disk.
token_cache: SerializableTokenCache = SerializableTokenCache()

def get_client_id() -> str:
    """Get the Azure client ID from environment variable."""
    client_id = os.getenv("AZURE_CLIENT_ID")
    if not client_id:
        raise Exception("AZURE_CLIENT_ID environment variable not set")
    return client_id

def _persist_cache() -> None:
    """Write the MSAL token cache to disk if it changed (chmod 600 where supported)."""
    if not TOKEN_CACHE_ENABLED:
        return
    if not token_cache.has_state_changed:
        return
    try:
        TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_CACHE_FILE, "w") as f:
            f.write(token_cache.serialize())
        try:
            TOKEN_CACHE_FILE.chmod(0o600)
        except OSError:
            pass  # chmod is a no-op on some filesystems (e.g. Windows mounts)
        logger.info(f"Token cache persisted to {TOKEN_CACHE_FILE}")
    except Exception as e:
        logger.warning(f"Failed to persist token cache: {e}")

def _load_cache() -> None:
    """Load the MSAL token cache from disk into the in-memory cache."""
    if not TOKEN_CACHE_ENABLED:
        logger.info("Token caching disabled - starting with an empty cache")
        return
    try:
        if TOKEN_CACHE_FILE.exists():
            token_cache.deserialize(TOKEN_CACHE_FILE.read_text())
            logger.info(f"Token cache loaded from {TOKEN_CACHE_FILE}")
        else:
            logger.info(f"No token cache file at {TOKEN_CACHE_FILE}")
    except Exception as e:
        logger.warning(f"Failed to load token cache: {e}")

def init_msal_app(client_id: str) -> PublicClientApplication:
    """Initialize the MSAL public client, backed by the serializable token cache."""
    return PublicClientApplication(
        client_id=client_id,
        authority=AUTHORITY,
        token_cache=token_cache,
    )

def get_msal_app() -> PublicClientApplication:
    """Return the singleton MSAL app, loading the cache on first initialization."""
    global msal_app
    if not msal_app:
        _load_cache()
        msal_app = init_msal_app(get_client_id())
    return msal_app

async def ensure_valid_token() -> bool:
    """Ensure a valid access token is available, refreshing silently via MSAL if needed.

    MSAL owns refresh-token storage inside the serializable cache; acquire_token_silent
    transparently uses the refresh token when the access token is expired. This is what
    lets a headless container mint tokens once and refresh silently thereafter.
    """
    global access_token, token_expires_at

    # Fast path: token still comfortably valid (expiry already carries a 5-min buffer).
    if access_token and token_expires_at and time.time() < token_expires_at:
        return True

    app = get_msal_app()
    accounts = app.get_accounts()
    if not accounts:
        access_token = None
        return False

    # MSAL is synchronous and hits the network on refresh - keep it off the event loop.
    result = await asyncio.to_thread(app.acquire_token_silent, SCOPES, account=accounts[0])
    _persist_cache()

    if result and "access_token" in result:
        access_token = result["access_token"]
        token_expires_at = time.time() + result.get("expires_in", 3600) - 300
        return True

    access_token = None
    return False

async def get_access_token() -> str:
    """Return a valid bearer token, or raise if not authenticated."""
    if not await ensure_valid_token():
        raise Exception(
            "Not authenticated. Please call 'start_authentication' and "
            "'complete_authentication' first."
        )
    return access_token

# Global variable to store the current authentication flow
current_flow = None

@mcp.tool()
async def start_authentication() -> str:
    """
    Start the full authentication process.
    
    Returns:
        Authentication instructions with device code
    """
    global access_token, msal_app, current_flow
    
    try:
        client_id = get_client_id()
        logger.info(f"Starting authentication with client_id: {client_id[:8]}...")

        # Ensure MSAL app exists (also loads any cached tokens)
        msal_app = get_msal_app()

        # Start device code flow (network call - keep it off the event loop)
        logger.info("Initiating device flow for authentication...")
        flow = await asyncio.to_thread(msal_app.initiate_device_flow, scopes=SCOPES)
        
        if "user_code" not in flow:
            error_msg = flow.get('error_description', 'Unknown error in device flow')
            raise Exception(f"Failed to create device flow: {error_msg}")
        
        # Return the authentication instructions
        result = {
            "status": "authentication_required",
            "instructions": f"Go to {flow['verification_uri']} and enter code: {flow['user_code']}",
            "verification_uri": flow['verification_uri'],
            "user_code": flow['user_code'],
            "expires_in": flow.get('expires_in', 900),
            "message": "Please complete authentication, then call 'complete_authentication'"
        }
        
        # Store the flow for completion
        current_flow = flow
        
        return json.dumps(result, indent=2)
        
    except Exception as e:
        logger.error(f"Start authentication error: {str(e)}")
        return json.dumps({
            "status": "error",
            "error": str(e)
        }, indent=2)

@mcp.tool()
async def complete_authentication() -> str:
    """
    Complete the authentication process after user enters device code.
    
    Returns:
        Authentication status and user info
    """
    global access_token, token_expires_at, msal_app, current_flow

    try:
        if not current_flow:
            return json.dumps({
                "status": "error",
                "error": "No authentication flow in progress. Call 'start_authentication' first."
            }, indent=2)
        
        if not msal_app:
            return json.dumps({
                "status": "error", 
                "error": "MSAL app not initialized"
            }, indent=2)
        
        logger.info("Completing device flow authentication...")

        # Complete the flow. This POLLS until the user finishes sign-in (up to the
        # flow's expiry, ~15 min) - run it on a worker thread or it freezes every
        # concurrent request of the HTTP server, health checks included.
        result = await asyncio.to_thread(msal_app.acquire_token_by_device_flow, current_flow)

        if "access_token" in result:
            # MSAL populated the token cache (incl. the refresh token); persist it.
            access_token = result["access_token"]
            token_expires_at = time.time() + result.get("expires_in", 3600) - 300
            _persist_cache()

            logger.info("Authentication successful and tokens cached!")
            
            # Test the token with a basic Graph API call
            try:
                user_info = await make_graph_request("/me")
                return json.dumps({
                    "status": "success",
                    "message": "Authentication completed successfully and tokens cached for future use",
                    "user": user_info.get("displayName", "Unknown"),
                    "email": user_info.get("mail") or user_info.get("userPrincipalName", "Unknown")
                }, indent=2)
                        
            except Exception as graph_error:
                return json.dumps({
                    "status": "partial_success",
                    "message": "Got access token but Graph API test failed",
                    "graph_error": str(graph_error)
                }, indent=2)
        else:
            error_desc = result.get('error_description', 'Unknown authentication error')
            return json.dumps({
                "status": "error",
                "error": f"Authentication failed: {error_desc}"
            }, indent=2)
            
    except Exception as e:
        logger.error(f"Complete authentication error: {str(e)}")
        return json.dumps({
            "status": "error",
            "error": str(e)
        }, indent=2)
    finally:
        # Clear the flow
        current_flow = None

@mcp.tool()
async def check_authentication() -> str:
    """
    Check current authentication status and token validity.
    
    Returns:
        Authentication status information
    """
    try:
        cache_status = "enabled" if TOKEN_CACHE_ENABLED else "disabled"
        cache_file_exists = TOKEN_CACHE_FILE.exists() if TOKEN_CACHE_ENABLED else False
        
        if await ensure_valid_token():
            try:
                user_info = await make_graph_request("/me")
                time_until_expiry = int(token_expires_at - time.time()) if token_expires_at else 0
                
                return json.dumps({
                    "status": "authenticated",
                    "user": user_info.get("displayName", "Unknown"),
                    "email": user_info.get("mail") or user_info.get("userPrincipalName", "Unknown"),
                    "token_valid_for_seconds": max(0, time_until_expiry),
                    "token_valid_for_hours": round(max(0, time_until_expiry) / 3600, 1),
                    "token_caching": cache_status,
                    "cache_file_exists": cache_file_exists,
                    "cache_file_path": str(TOKEN_CACHE_FILE) if TOKEN_CACHE_ENABLED else "N/A"
                }, indent=2)
                
            except Exception as graph_error:
                return json.dumps({
                    "status": "token_invalid",
                    "error": str(graph_error),
                    "message": "Token exists but API call failed - may need re-authentication",
                    "token_caching": cache_status
                }, indent=2)
        else:
            return json.dumps({
                "status": "not_authenticated",
                "message": "No valid authentication token. Please call 'start_authentication'",
                "token_caching": cache_status,
                "cache_file_exists": cache_file_exists
            }, indent=2)
            
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
            "token_caching": "unknown"
        }, indent=2)

# Shared HTTP client per event loop: connection/TLS reuse instead of a fresh client
# per request. Keyed weakly by loop because stdio/http/one-shot entrypoints may each
# run their own loop, and an httpx client must not cross loops. Clients are never
# aclose()d explicitly: they live for the loop's lifetime and die with the process.
import weakref
_http_clients: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

def _http_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _http_clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=GRAPH_TIMEOUT)
        _http_clients[loop] = client
    return client

# HTTP statuses worth retrying - OneNote/Graph are prone to transient 429/503/504.
RETRY_STATUSES = {429, 503, 504}
MAX_RETRIES = int(os.getenv("ONENOTE_MAX_RETRIES", "4"))

def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with a little jitter: ~1s, 2s, 4s, 8s (capped at 30s)."""
    return min(2 ** (attempt - 1), 30) + random.uniform(0, 0.5)

def _retry_after_seconds(response: "httpx.Response") -> Optional[float]:
    """Honor a Retry-After header (in seconds) if the server sent one."""
    ra = response.headers.get("Retry-After")
    if not ra:
        return None
    try:
        return float(ra)
    except ValueError:
        return None

async def make_graph_request(endpoint: str, method: str = "GET", data: Dict = None) -> Dict:
    """Make a request to Microsoft Graph API, retrying transient failures with backoff."""
    if not await ensure_valid_token():
        raise Exception("Not authenticated. Please call 'start_authentication' and 'complete_authentication' first.")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    url = f"{GRAPH_BASE_URL}{endpoint}"

    last_error = None
    response = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            client = _http_client()
            if method == "GET":
                response = await client.get(url, headers=headers)
            elif method == "POST":
                response = await client.post(url, headers=headers, json=data)
            elif method == "PATCH":
                response = await client.patch(url, headers=headers, json=data)
            elif method == "DELETE":
                response = await client.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
        except httpx.TransportError as e:
            last_error = f"network error: {e}"
            if attempt < MAX_RETRIES:
                await asyncio.sleep(_backoff_seconds(attempt))
                continue
            raise Exception(f"Graph API request failed after {attempt} attempts - {last_error}")

        if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
            delay = _retry_after_seconds(response) or _backoff_seconds(attempt)
            logger.warning(f"Graph {response.status_code} on {endpoint} (attempt {attempt}/{MAX_RETRIES}); retrying in {delay:.1f}s")
            await asyncio.sleep(delay)
            continue

        if response.status_code >= 400:
            raise Exception(f"Graph API error: {response.status_code} - {response.text}")

        # DELETE (204) and some accepted requests return no body.
        if not response.content:
            return {}
        return response.json()

    raise Exception(f"Graph API error after {MAX_RETRIES} attempts on {endpoint} - {last_error or 'transient failures'}")

async def _graph_get_all(endpoint: str, max_pages: int = 50) -> List[Dict]:
    """GET a Graph collection endpoint, following @odata.nextLink pagination.

    Graph caps each response batch (default 20 for OneNote collections); without
    following nextLink, anything past the first batch is silently dropped.
    """
    items: List[Dict] = []
    next_endpoint = endpoint
    for _ in range(max_pages):
        resp = await make_graph_request(next_endpoint)
        items.extend(resp.get("value", []))
        next_link = resp.get("@odata.nextLink")
        if not next_link:
            break
        # nextLink is absolute; re-base it since make_graph_request prefixes the host.
        if next_link.startswith(GRAPH_BASE_URL):
            next_link = next_link[len(GRAPH_BASE_URL):]
        elif next_link.lower().startswith("http"):
            # Defensive: host/casing variants must not get concatenated onto the base.
            parts = urlsplit(next_link)
            path = parts.path
            if path.lower().startswith("/v1.0"):
                path = path[len("/v1.0"):]
            next_link = path + (f"?{parts.query}" if parts.query else "")
        next_endpoint = next_link
    else:
        logger.warning(f"_graph_get_all: hit {max_pages}-page cap on {endpoint}; result truncated")
    return items

async def _graph_request_raw(method: str, path: str, params: Dict = None, json_data: Dict = None) -> "httpx.Response":
    """Raw Graph request returning the untouched httpx.Response (for multipart/HTML
    bodies or callers that need response headers, e.g. Operation-Location), with the
    same transient-retry behavior as make_graph_request."""
    token = await get_access_token()
    url = f"{GRAPH_BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {token}"}

    last_error = None
    response = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await _http_client().request(method, url, headers=headers, params=params, json=json_data)
        except httpx.TransportError as e:
            last_error = f"network error: {e}"
            if attempt < MAX_RETRIES:
                await asyncio.sleep(_backoff_seconds(attempt))
                continue
            raise Exception(f"Graph {method} failed after {attempt} attempts - {last_error}")

        if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
            delay = _retry_after_seconds(response) or _backoff_seconds(attempt)
            logger.warning(f"Graph {response.status_code} on {path} (attempt {attempt}/{MAX_RETRIES}); retrying in {delay:.1f}s")
            await asyncio.sleep(delay)
            continue

        return response

    return response

async def _graph_get_raw(path: str, params: Dict = None) -> "httpx.Response":
    """Raw Graph GET (see _graph_request_raw)."""
    return await _graph_request_raw("GET", path, params=params)

INK_REVALIDATE = float(os.getenv("ONENOTE_INK_REVALIDATE", "30"))

async def _page_last_mod(page_id: str) -> Optional[str]:
    """The page's lastModifiedDateTime via a metadata-only probe (the cache validator).

    TTL-gated by ONENOTE_INK_REVALIDATE: within the window the last value is reused
    without probing (instant hot path); past it we re-check so a freshly re-inked page
    is picked up within seconds. Returns None on failure (caller then bypasses cache).
    """
    async def _probe():
        meta = await make_graph_request(f"/me/onenote/pages/{page_id}")
        return meta.get("lastModifiedDateTime")
    try:
        return await CACHE.listing(f"lastmod:{page_id}", INK_REVALIDATE, _probe)
    except Exception:
        return None

@mcp.tool()
async def list_notebooks() -> str:
    """
    List all OneNote notebooks.
    
    Returns:
        JSON string containing notebook information
    """
    try:
        logger.info("Making request to /me/onenote/notebooks")
        notebooks = await _graph_get_all("/me/onenote/notebooks")
        logger.info(f"Graph API response received with {len(notebooks)} notebooks")

        result = []
        for notebook in notebooks:
            result.append({
                "id": notebook.get("id"),
                "name": notebook.get("displayName"),
                "created": notebook.get("createdDateTime"),
                "modified": notebook.get("lastModifiedDateTime")
            })
        
        logger.info(f"Returning {len(result)} notebooks")
        return json.dumps(result, indent=2)
    
    except Exception as e:
        logger.error(f"Error in list_notebooks: {str(e)}")
        return f"Error listing notebooks: {str(e)}"

@mcp.tool()
async def list_sections(notebook_id: str) -> str:
    """
    List sections in a specific notebook.
    
    Args:
        notebook_id: ID of the notebook to list sections from
    
    Returns:
        JSON string containing section information
    """
    try:
        sections = await _graph_get_all(f"/me/onenote/notebooks/{notebook_id}/sections")

        result = []
        for section in sections:
            result.append({
                "id": section.get("id"),
                "name": section.get("displayName"),
                "created": section.get("createdDateTime"),
                "modified": section.get("lastModifiedDateTime")
            })
        
        return json.dumps(result, indent=2)
    
    except Exception as e:
        return f"Error listing sections: {str(e)}"

@mcp.tool()
async def list_pages(section_id: str) -> str:
    """
    List pages in a specific section.
    
    Args:
        section_id: ID of the section to list pages from
    
    Returns:
        JSON string containing page information
    """
    try:
        pages = await _graph_get_all(f"/me/onenote/sections/{section_id}/pages")

        result = []
        for page in pages:
            result.append({
                "id": page.get("id"),
                "title": page.get("title"),
                "created": page.get("createdDateTime"),
                "modified": page.get("lastModifiedDateTime"),
                "content_url": page.get("contentUrl")
            })
        
        return json.dumps(result, indent=2)
    
    except Exception as e:
        return f"Error listing pages: {str(e)}"

@mcp.tool()
async def get_page_content(page_id: str) -> str:
    """
    Get the content of a specific page.

    Args:
        page_id: ID of the page to retrieve content from

    Returns:
        Page content as HTML or error message
    """
    try:
        last_mod = await _page_last_mod(page_id)

        async def _produce() -> str:
            response = await _graph_get_raw(f"/me/onenote/pages/{page_id}/content")
            if response.status_code >= 400:
                raise Exception(f"{response.status_code} - {response.text}")
            return response.text

        return await CACHE.page_text(page_id, last_mod, _produce)
    except Exception as e:
        return f"Error getting page content: {str(e)}"

def _parse_page_multipart(content_type: str, body: bytes) -> Dict[str, Any]:
    """Split a OneNote page response into its HTML and InkML parts.

    With ?includeInkML=true, Graph returns a multipart/related body: an HTML part
    (with an '<!-- InkNode is not supported -->' placeholder where ink sits) plus
    one or more InkML parts holding the real <inkml:trace> stroke data. Responses
    with no ink come back as plain HTML (not multipart) and are returned as-is.

    Note: parse response.content (raw bytes), never a post-processed string - the
    multipart boundaries must stay byte-intact or strokes get under-counted.
    """
    html_part: Optional[str] = None
    inkml_part: Optional[str] = None

    if "multipart" in content_type.lower():
        # Reconstruct a minimal MIME document so the stdlib email parser can split it.
        raw = b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body
        msg = message_from_bytes(raw, policy=email_default_policy)
        for part in msg.iter_parts():
            ctype = (part.get_content_type() or "").lower()
            try:
                payload = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True)
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8", errors="replace")
            if "inkml" in ctype:
                inkml_part = (inkml_part or "") + payload
            elif "html" in ctype:
                html_part = payload
            elif html_part is None and "xml" not in ctype:
                html_part = payload
    else:
        html_part = body.decode("utf-8", errors="replace")

    # Count ink strokes: <inkml:trace ...> or <trace ...> elements.
    trace_count = 0
    if inkml_part:
        trace_count = len(re.findall(r"<(?:inkml:)?trace\b", inkml_part))
    elif html_part and "inkml" in html_part.lower():
        # Fallback: some responses inline InkML in the HTML part instead of splitting it.
        trace_count = len(re.findall(r"<(?:inkml:)?trace\b", html_part))
        if trace_count:
            inkml_part = html_part

    return {
        "has_ink": trace_count > 0,
        "trace_count": trace_count,
        "html": html_part or "",
        "inkml": inkml_part or "",
    }

@mcp.tool()
async def get_page_ink(page_id: str) -> str:
    """Read a page INCLUDING its handwritten ink strokes.

    Fetches the page with ?includeInkML=true and splits the multipart response
    into typed/printed HTML and the raw InkML stroke data. The InkML is what gets
    rasterized to an image for vision to read. This is read-only - it never
    modifies the page, so ink stays pristine.

    Args:
        page_id: ID of the page to read.

    Returns:
        JSON with has_ink, trace_count, and any typed text. Raw ink is omitted
        (too large); call render_page_ink to read the handwriting as an image.
    """
    try:
        response = await _graph_get_raw(
            f"/me/onenote/pages/{page_id}/content",
            params={"includeInkML": "true"},
        )
        if response.status_code >= 400:
            return f"Error getting page ink: {response.status_code} - {response.text}"

        parsed = _parse_page_multipart(
            response.headers.get("content-type", ""), response.content
        )
        # Do NOT return the raw InkML/HTML: a dense page is multiple MB and blows the
        # 1MB tool-result cap. Raw strokes aren't readable by the model anyway - use
        # render_page_ink to read the handwriting as an image.
        return json.dumps(
            {
                "status": "success",
                "page_id": page_id,
                "has_ink": parsed["has_ink"],
                "trace_count": parsed["trace_count"],
                "text": _html_to_text(parsed["html"], limit=2000),
                "note": "Raw ink omitted (can be multiple MB). Call render_page_ink to read the handwriting.",
            },
            indent=2,
        )
    except Exception as e:
        return f"Error getting page ink: {str(e)}"

@mcp.tool()
async def render_page_ink(page_id: str, max_px: int = 1200) -> Image:
    """Render a page's handwritten ink to a PNG image so it can be read via vision.

    Fetches the page's InkML strokes (?includeInkML=true) and rasterizes them to an
    image. Use this to actually READ handwriting on a page. Read-only - it never
    modifies the page, so the user's ink stays pristine.

    Args:
        page_id: ID of the page to render.
        max_px: Longest edge of the output image in pixels (default 1200).

    Returns:
        A PNG image of the page's ink.
    """
    # Clamp here too (not just in the rasterizer) so cache keys stay bounded.
    max_px = max(MIN_RENDER_PX, min(int(max_px), MAX_RENDER_PX))
    last_mod = await _page_last_mod(page_id)

    async def _produce() -> bytes:
        response = await _graph_get_raw(
            f"/me/onenote/pages/{page_id}/content",
            params={"includeInkML": "true"},
        )
        if response.status_code >= 400:
            raise Exception(f"Error fetching page ink: {response.status_code} - {response.text}")
        parsed = _parse_page_multipart(response.headers.get("content-type", ""), response.content)
        if not parsed["has_ink"]:
            raise Exception("This page has no ink strokes to render.")
        return rasterize_inkml(parsed["inkml"], max_px=max_px)

    png = await CACHE.ink_png(page_id, max_px, last_mod, _produce)
    return Image(data=png, format="png")

@mcp.tool()
async def clear_token_cache() -> str:
    """
    Clear the stored authentication tokens.
    
    Returns:
        Status message
    """
    global access_token, token_expires_at

    try:
        # Clear in-memory tokens
        access_token = None
        token_expires_at = None

        # Remove accounts from the MSAL cache
        app = get_msal_app()
        for account in app.get_accounts():
            app.remove_account(account)
        _persist_cache()

        # Remove cache file
        if TOKEN_CACHE_FILE.exists():
            TOKEN_CACHE_FILE.unlink()
            
        return json.dumps({
            "status": "success",
            "message": "Token cache cleared. You will need to re-authenticate."
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": str(e)
        }, indent=2)

@mcp.tool()
async def create_notebook(name: str, description: str = None) -> str:
    """
    Create a new OneNote notebook.
    
    Args:
        name: Name of the new notebook
        description: Optional description for the notebook
    
    Returns:
        JSON string with the created notebook information
    """
    try:
        data = {"displayName": name}
        if description:
            data["description"] = description
            
        notebook = await make_graph_request("/me/onenote/notebooks", method="POST", data=data)
        
        result = {
            "status": "success",
            "message": f"Notebook '{name}' created successfully",
            "notebook": {
                "id": notebook.get("id"),
                "name": notebook.get("displayName"),
                "created": notebook.get("createdDateTime")
            }
        }
        
        return json.dumps(result, indent=2)
    
    except Exception as e:
        return f"Error creating notebook: {str(e)}"

@mcp.tool()
async def create_section(notebook_id: str, name: str) -> str:
    """
    Create a new section at the top level of a OneNote notebook.

    To create a section inside a section group (e.g. "Clients > Harmony"),
    use create_section_in_group instead.

    Args:
        notebook_id: ID of the notebook to create the section in
        name: Name of the new section

    Returns:
        JSON string with the created section information
    """
    try:
        data = {"displayName": name}
        
        section = await make_graph_request(
            f"/me/onenote/notebooks/{notebook_id}/sections", 
            method="POST", 
            data=data
        )
        
        result = {
            "status": "success",
            "message": f"Section '{name}' created successfully",
            "section": {
                "id": section.get("id"),
                "name": section.get("displayName"),
                "created": section.get("createdDateTime")
            }
        }
        
        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error creating section: {str(e)}"

def _section_group_summary(group: Dict) -> Dict:
    """Trim a Graph sectionGroup resource to the fields tools return."""
    return {
        "id": group.get("id"),
        "name": group.get("displayName"),
        "created": group.get("createdDateTime"),
        "modified": group.get("lastModifiedDateTime"),
        "sections": [
            {"id": s.get("id"), "name": s.get("displayName")}
            for s in group.get("sections", [])
        ],
    }

@mcp.tool()
async def list_section_groups(notebook_id: str = None, section_group_id: str = None) -> str:
    """
    List section groups, including the sections inside each group.

    Section groups are folders that nest sections (and other section groups)
    inside a notebook, e.g. "Clients > Harmony". Sections inside a group do NOT
    appear in list_sections, so use this to discover nested structure.

    Args:
        notebook_id: List section groups directly under this notebook.
        section_group_id: List section groups nested inside this section group.
            If neither argument is given, lists all section groups across notebooks.

    Returns:
        JSON string containing section group information with nested sections
    """
    try:
        if section_group_id:
            endpoint = f"/me/onenote/sectionGroups/{section_group_id}/sectionGroups"
        elif notebook_id:
            endpoint = f"/me/onenote/notebooks/{notebook_id}/sectionGroups"
        else:
            endpoint = "/me/onenote/sectionGroups"

        groups = await _graph_get_all(f"{endpoint}?$expand=sections")
        result = [_section_group_summary(g) for g in groups]
        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error listing section groups: {str(e)}"

@mcp.tool()
async def create_section_group(notebook_id: str, name: str) -> str:
    """
    Create a new section group in a OneNote notebook.

    Section groups act as folders for sections. After creating one, add
    sections inside it with create_section_in_group.

    Args:
        notebook_id: ID of the notebook to create the section group in
        name: Name of the new section group

    Returns:
        JSON string with the created section group information
    """
    try:
        group = await make_graph_request(
            f"/me/onenote/notebooks/{notebook_id}/sectionGroups",
            method="POST",
            data={"displayName": name},
        )

        result = {
            "status": "success",
            "message": f"Section group '{name}' created successfully",
            "section_group": {
                "id": group.get("id"),
                "name": group.get("displayName"),
                "created": group.get("createdDateTime"),
            },
        }

        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error creating section group: {str(e)}"

@mcp.tool()
async def create_section_in_group(section_group_id: str, name: str) -> str:
    """
    Create a new section inside a section group.

    Use this for nested structures like "Clients > Harmony": create (or find)
    the "Clients" section group, then create the "Harmony" section inside it.

    Args:
        section_group_id: ID of the section group to create the section in
        name: Name of the new section

    Returns:
        JSON string with the created section information
    """
    try:
        section = await make_graph_request(
            f"/me/onenote/sectionGroups/{section_group_id}/sections",
            method="POST",
            data={"displayName": name},
        )

        result = {
            "status": "success",
            "message": f"Section '{name}' created successfully in section group",
            "section": {
                "id": section.get("id"),
                "name": section.get("displayName"),
                "created": section.get("createdDateTime"),
            },
        }

        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error creating section in section group: {str(e)}"

async def _create_page_impl(section_id: str, title: str, content_html: str = None) -> str:
    """Create a new typed page in a section. Shared by the create_page tool and the
    Sidekick write-back. Escapes the title; content_html is treated as HTML."""
    try:
        # Escape the title so special characters can't break the XHTML we POST.
        title = html.escape(title, quote=True)
        # Build the HTML structure for the page
        if content_html:
            # Ensure content is wrapped in proper OneNote HTML structure
            if not content_html.strip().startswith('<html>'):
                page_html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>{title}</title>
    <meta name="created" content="{time.strftime('%Y-%m-%dT%H:%M:%S.0000000')}" />
</head>
<body>
    <div>
        <div>{content_html}</div>
    </div>
</body>
</html>"""
            else:
                page_html = content_html
        else:
            # Create a basic page with just the title
            page_html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>{title}</title>
    <meta name="created" content="{time.strftime('%Y-%m-%dT%H:%M:%S.0000000')}" />
</head>
<body>
    <div>
        <p>Page created by OneNote MCP Server</p>
    </div>
</body>
</html>"""

        # OneNote API expects multipart form data for page creation
        token = await get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/xhtml+xml"
        }

        response = await _http_client().post(
            f"{GRAPH_BASE_URL}/me/onenote/sections/{section_id}/pages",
            headers=headers,
            content=page_html
        )

        if response.status_code >= 400:
            return f"Error creating page: {response.status_code} - {response.text}"

        page = response.json()

        links = page.get("links") or {}
        result = {
            "status": "success",
            "message": f"Page '{title}' created successfully",
            "page": {
                "id": page.get("id"),
                "title": page.get("title"),
                "created": page.get("createdDateTime"),
                # User-navigable links (open the page directly, bypassing client sync lag).
                "client_url": (links.get("oneNoteClientUrl") or {}).get("href"),
                "web_url": (links.get("oneNoteWebUrl") or {}).get("href"),
                # Internal Graph API endpoint (not user-navigable).
                "content_url": page.get("contentUrl"),
            }
        }

        CACHE.invalidate_listings()  # new page -> drop stale listing caches
        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error creating page: {str(e)}"

@mcp.tool()
async def create_page(section_id: str, title: str, content_html: str = None) -> str:
    """
    Create a new page in a OneNote section.

    Args:
        section_id: ID of the section to create the page in
        title: Title of the new page
        content_html: Optional HTML for the page body. Do NOT repeat the title here;
            it is applied automatically as the page title.

    Returns:
        JSON string with the created page information
    """
    return await _create_page_impl(section_id, title, content_html)

@mcp.tool()
async def update_page_content(page_id: str, content_html: str, target_element: str = "body",
                              action: str = "append") -> str:
    """
    Update the content of an existing OneNote page.

    Args:
        page_id: ID of the page to update
        content_html: New HTML content to add/replace
        target_element: Target element to update (default: "body"). Use "title"
            with the new title text as content_html to rename a page.
        action: How to apply the content: "append" (default), "replace",
            "prepend", or "insert". The "title" target only supports replace,
            so replace is used automatically there.

    Returns:
        Status message
    """
    try:
        valid_actions = ("append", "replace", "prepend", "insert")
        if action not in valid_actions:
            return (f"Error updating page content: unsupported action '{action}' "
                    f"(use one of: {', '.join(valid_actions)})")
        # Graph rejects APPEND on the title target (error 20141); replace is the
        # only action that works for titles.
        if target_element == "title" and action == "append":
            action = "replace"

        # OneNote PATCH API for updating page content
        patch_data = [
            {
                "target": target_element,
                "action": action,
                "content": content_html
            }
        ]

        token = await get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        response = await _http_client().patch(
            f"{GRAPH_BASE_URL}/me/onenote/pages/{page_id}/content",
            headers=headers,
            json=patch_data
        )

        if response.status_code >= 400:
            return f"Error updating page: {response.status_code} - {response.text}"

        # The page changed: drop its cached content/PNGs and every listing (incl. the
        # lastmod probe) so the next read sees the new content, not a stale cache.
        # invalidate_page walks/removes files - keep it off the event loop.
        await asyncio.to_thread(CACHE.invalidate_page, page_id)
        CACHE.invalidate_listings()

        result = {
            "status": "success",
            "message": "Page content updated successfully",
            "page_id": page_id
        }

        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error updating page content: {str(e)}"

# Page copy is asynchronous in Graph (202 + Operation-Location): poll the operation
# up to this long before handing the operation id back to the caller.
COPY_WAIT_SECONDS = float(os.getenv("ONENOTE_COPY_WAIT", "60"))
OPERATION_POLL_INTERVAL = 2.0

class GraphUnsupportedError(Exception):
    """Graph answered 501 'not implemented' - the operation doesn't exist for
    this notebook type (consumer/personal OneDrive notebooks lack copyToSection)."""

async def _start_page_copy(page_id: str, target_section_id: str) -> str:
    """Kick off a copyToSection and return the operation id to poll."""
    response = await _graph_request_raw(
        "POST",
        f"/me/onenote/pages/{page_id}/copyToSection",
        json_data={"id": target_section_id},
    )
    if response.status_code == 501:
        raise GraphUnsupportedError(
            "Microsoft Graph does not support page copy/move for personal OneDrive "
            "notebooks (error 20111, 'OData Feature not implemented'). Move or copy "
            "the page manually in the OneNote app."
        )
    if response.status_code >= 400:
        raise Exception(f"Graph API error: {response.status_code} - {response.text}")

    # The operation id is the tail of the Operation-Location header; some responses
    # also (or only) carry it in the body.
    op_location = response.headers.get("Operation-Location", "")
    op_id = op_location.rstrip("/").rsplit("/", 1)[-1] if op_location else ""
    if not op_id:
        try:
            op_id = response.json().get("id") or ""
        except Exception:
            pass
    if not op_id:
        raise Exception("copyToSection was accepted but Graph returned no operation id")
    return op_id

async def _poll_operation(operation_id: str, wait_seconds: float) -> Dict:
    """Poll a OneNote async operation until it finishes or the wait budget runs out.
    Returns the last operation resource seen (status may still be running)."""
    deadline = time.monotonic() + wait_seconds
    while True:
        op = await make_graph_request(f"/me/onenote/operations/{operation_id}")
        status = (op.get("status") or "").lower()
        if status in ("completed", "failed") or time.monotonic() >= deadline:
            return op
        await asyncio.sleep(OPERATION_POLL_INTERVAL)

async def _ink_guard(page_id: str, action: str) -> Optional[str]:
    """Return a refusal message if the page contains ink or its ink status can't be
    verified, else None. The server's hard rule is to never destroy handwritten ink,
    so this fails closed: no verification, no deletion."""
    try:
        response = await _graph_get_raw(
            f"/me/onenote/pages/{page_id}/content",
            params={"includeInkML": "true"},
        )
        if response.status_code >= 400:
            return (
                f"Could not verify the page is ink-free "
                f"({response.status_code} - {response.text[:200]}); refusing to {action}."
            )
        parsed = _parse_page_multipart(
            response.headers.get("content-type", ""), response.content
        )
        if parsed["has_ink"]:
            return (
                f"Page contains handwritten ink ({parsed['trace_count']} strokes). "
                f"This server never deletes ink pages; refusing to {action}."
            )
        return None
    except Exception as e:
        return f"Could not verify the page is ink-free ({e}); refusing to {action}."

@mcp.tool()
async def copy_page(page_id: str, target_section_id: str) -> str:
    """Copy a page into another section. The original page is left untouched, so
    this is safe for any page, including ink pages.

    Graph performs the copy asynchronously; this polls until it finishes (up to
    ONENOTE_COPY_WAIT seconds, default 60). If it is still running after that,
    the operation id is returned - pass it to check_onenote_operation to get the
    new page id once the copy completes.

    Args:
        page_id: ID of the page to copy.
        target_section_id: ID of the section to copy the page into.

    Returns:
        JSON with the new page id (or a pending operation id).
    """
    try:
        op_id = await _start_page_copy(page_id, target_section_id)
        op = await _poll_operation(op_id, COPY_WAIT_SECONDS)
        status = (op.get("status") or "").lower()

        if status == "completed":
            CACHE.invalidate_listings()  # new page -> drop stale listing caches
            return json.dumps({
                "status": "success",
                "message": "Page copied successfully",
                "new_page_id": op.get("resourceId"),
                "new_page_url": op.get("resourceLocation"),
            }, indent=2)

        if status == "failed":
            error = op.get("error") or {}
            return json.dumps({
                "status": "error",
                "error": f"Copy failed: {error.get('message', 'unknown error')}",
                "operation_id": op_id,
            }, indent=2)

        return json.dumps({
            "status": "pending",
            "operation_id": op_id,
            "message": (
                f"Copy still running after {COPY_WAIT_SECONDS:.0f}s. "
                "Call check_onenote_operation with this operation_id for the result."
            ),
        }, indent=2)

    except GraphUnsupportedError as e:
        return json.dumps({
            "status": "unsupported",
            "error": str(e),
        }, indent=2)
    except Exception as e:
        return f"Error copying page: {str(e)}"

@mcp.tool()
async def move_page(page_id: str, target_section_id: str) -> str:
    """Move a typed page to another section (copy, then delete the original once
    the copy has completed).

    REFUSES to move pages containing handwritten ink - the delete half would
    destroy the original ink, and this server never touches ink pages. For ink
    pages use copy_page, which leaves the original in place.

    Args:
        page_id: ID of the page to move.
        target_section_id: ID of the destination section.

    Returns:
        JSON with the new page id, or a refusal/pending status.
    """
    try:
        guard = await _ink_guard(page_id, "move it (the original would be deleted)")
        if guard:
            return json.dumps({
                "status": "refused",
                "error": guard,
                "hint": "Use copy_page instead; it leaves the original page untouched.",
            }, indent=2)

        op_id = await _start_page_copy(page_id, target_section_id)
        op = await _poll_operation(op_id, COPY_WAIT_SECONDS)
        status = (op.get("status") or "").lower()

        if status == "failed":
            error = op.get("error") or {}
            return json.dumps({
                "status": "error",
                "error": f"Copy failed: {error.get('message', 'unknown error')} - original page NOT deleted",
                "operation_id": op_id,
            }, indent=2)

        if status != "completed":
            return json.dumps({
                "status": "pending",
                "operation_id": op_id,
                "message": (
                    f"Copy still running after {COPY_WAIT_SECONDS:.0f}s; the original was NOT "
                    "deleted. Call check_onenote_operation with this operation_id, then "
                    "delete_page on the original once the copy has completed."
                ),
            }, indent=2)

        # Copy confirmed complete - now remove the original.
        await make_graph_request(f"/me/onenote/pages/{page_id}", method="DELETE")
        # invalidate_page walks/removes files - keep it off the event loop.
        await asyncio.to_thread(CACHE.invalidate_page, page_id)
        CACHE.invalidate_listings()

        return json.dumps({
            "status": "success",
            "message": "Page moved successfully (copied to target section, original deleted)",
            "new_page_id": op.get("resourceId"),
            "new_page_url": op.get("resourceLocation"),
        }, indent=2)

    except GraphUnsupportedError as e:
        return json.dumps({
            "status": "unsupported",
            "error": str(e),
            "hint": "The original page was not modified or deleted.",
        }, indent=2)
    except Exception as e:
        return f"Error moving page: {str(e)}"

@mcp.tool()
async def delete_page(page_id: str, confirm: bool = False) -> str:
    """Permanently delete a typed page. Guarded twice: it must be called with
    confirm=true, and it REFUSES to delete any page containing handwritten ink
    (this server's hard rule is that the user's ink is never touched).

    Args:
        page_id: ID of the page to delete.
        confirm: Must be true to actually delete. When false, returns the page
            title so the caller can confirm the right page is targeted.

    Returns:
        JSON status message.
    """
    try:
        meta = await make_graph_request(f"/me/onenote/pages/{page_id}")
        title = meta.get("title") or "(untitled)"

        if not confirm:
            return json.dumps({
                "status": "confirmation_required",
                "page_id": page_id,
                "title": title,
                "message": (
                    f"This will permanently delete '{title}'. "
                    "Call delete_page again with confirm=true to proceed."
                ),
            }, indent=2)

        guard = await _ink_guard(page_id, "delete it")
        if guard:
            return json.dumps({
                "status": "refused",
                "page_id": page_id,
                "title": title,
                "error": guard,
            }, indent=2)

        await make_graph_request(f"/me/onenote/pages/{page_id}", method="DELETE")
        # invalidate_page walks/removes files - keep it off the event loop.
        await asyncio.to_thread(CACHE.invalidate_page, page_id)
        CACHE.invalidate_listings()

        return json.dumps({
            "status": "success",
            "message": f"Page '{title}' deleted",
            "page_id": page_id,
        }, indent=2)

    except Exception as e:
        return f"Error deleting page: {str(e)}"

@mcp.tool()
async def check_onenote_operation(operation_id: str) -> str:
    """Check the status of an asynchronous OneNote operation (e.g. a page copy
    that was still pending when copy_page or move_page returned).

    Args:
        operation_id: The operation id returned by copy_page/move_page.

    Returns:
        JSON with the operation status; on completion, the new page's id.
    """
    try:
        op = await make_graph_request(f"/me/onenote/operations/{operation_id}")
        result = {
            "operation_id": operation_id,
            "status": op.get("status"),
            "resource_id": op.get("resourceId"),
            "resource_location": op.get("resourceLocation"),
        }
        if op.get("error"):
            result["error"] = op["error"]
        return json.dumps(result, indent=2)

    except Exception as e:
        return f"Error checking operation: {str(e)}"

# Sidekick write-back configuration: generated summaries/todos go into a dedicated
# section so Claude's typed output never touches the user's ink pages.
SIDEKICK_NOTEBOOK = os.getenv("ONENOTE_SIDEKICK_NOTEBOOK", "Slugbook")
SIDEKICK_SECTION = os.getenv("ONENOTE_SIDEKICK_SECTION", "Sidekick")

async def _find_notebook_id(name: str) -> Optional[str]:
    notebooks = await _graph_get_all("/me/onenote/notebooks")
    for nb in notebooks:
        if nb.get("displayName") == name:
            return nb.get("id")
    return None

async def _find_or_create_sidekick_section() -> str:
    """Return the id of the dedicated Sidekick section, creating it if needed."""
    nb_id = await _find_notebook_id(SIDEKICK_NOTEBOOK)
    if not nb_id:
        raise Exception(f"Sidekick notebook '{SIDEKICK_NOTEBOOK}' not found.")
    sections = await _graph_get_all(f"/me/onenote/notebooks/{nb_id}/sections")
    for s in sections:
        if s.get("displayName") == SIDEKICK_SECTION:
            return s.get("id")
    created = await make_graph_request(
        f"/me/onenote/notebooks/{nb_id}/sections",
        method="POST",
        data={"displayName": SIDEKICK_SECTION},
    )
    return created.get("id")

async def _create_sidekick_page_impl(title: str, content_html: str) -> str:
    """Find-or-create the Sidekick section and write a new typed page into it.
    Plain helper shared by the create_sidekick_page tool and test scripts."""
    try:
        section_id = await _find_or_create_sidekick_section()
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, indent=2)
    return await _create_page_impl(section_id, title, content_html)

@mcp.tool()
async def create_sidekick_page(title: str, content_html: str) -> str:
    """Write a NEW typed page (summary, aggregated todo list, etc.) into a dedicated
    'Sidekick' section. This is the safe write-back path: it ONLY creates new typed
    pages in a separate section and never edits or touches the user's ink pages.

    Args:
        title: Title for the new page.
        content_html: HTML body (e.g. a summary paragraph or a <ul> todo list). Do NOT
            repeat the title here; it is applied automatically as the page title.

    Returns:
        JSON with the created page info and the Sidekick section used.
    """
    return await _create_sidekick_page_impl(title, content_html)

def _html_to_text(html_str: str, limit: int = 160) -> str:
    """Crude HTML -> text for page previews: strip tags/entities, collapse whitespace."""
    if not html_str:
        return ""
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_str)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:limit]

async def _find_pages_impl(query: str = "", limit: int = 25, include_preview: bool = False) -> str:
    """List pages in the working notebook with dates, section, ink flag and a typed-text
    preview, most-recently-modified first, with an optional title substring filter.

    Raises on failure (e.g. notebook missing) so errors are never cached as listings."""
    nb_id = await _find_notebook_id(SIDEKICK_NOTEBOOK)
    if not nb_id:
        raise Exception(f"Notebook '{SIDEKICK_NOTEBOOK}' not found.")
    sections = await _graph_get_all(f"/me/onenote/notebooks/{nb_id}/sections")
    pages = []
    for s in sections:
        sp = await _graph_get_all(f"/me/onenote/sections/{s['id']}/pages?$top=100")
        for p in sp:
            plinks = p.get("links") or {}
            pages.append({
                "id": p.get("id"),
                "title": p.get("title") or "(untitled)",
                "section": s.get("displayName"),
                "created": p.get("createdDateTime"),
                "modified": p.get("lastModifiedDateTime"),
                "client_url": (plinks.get("oneNoteClientUrl") or {}).get("href"),
                "web_url": (plinks.get("oneNoteWebUrl") or {}).get("href"),
            })
    pages.sort(key=lambda x: x.get("modified") or "", reverse=True)
    if query:
        q = query.lower()
        pages = [p for p in pages if q in p["title"].lower()]
    pages = pages[:max(1, limit)]

    if include_preview:
        # One content fetch per page: run them concurrently, bounded so a large
        # listing doesn't hammer Graph (throttling) or the event loop.
        sem = asyncio.Semaphore(5)

        async def _fill_preview(p: Dict) -> None:
            async with sem:
                try:
                    r = await _graph_get_raw(
                        f"/me/onenote/pages/{p['id']}/content", params={"includeInkML": "true"})
                    if r.status_code < 400:
                        parsed = _parse_page_multipart(r.headers.get("content-type", ""), r.content)
                        p["has_ink"] = parsed["has_ink"]
                        p["ink_traces"] = parsed["trace_count"]
                        p["preview"] = _html_to_text(parsed["html"])
                    else:
                        p["preview_error"] = str(r.status_code)
                except Exception as e:
                    p["preview_error"] = str(e)

        await asyncio.gather(*(_fill_preview(p) for p in pages))

    return json.dumps(
        {"status": "success", "notebook": SIDEKICK_NOTEBOOK, "count": len(pages), "pages": pages},
        indent=2,
    )

@mcp.tool()
async def find_pages(query: str = "", limit: int = 25, include_preview: bool = False) -> str:
    """Discover pages in the notebook to find the one the user means.

    Lists pages (default notebook: Slugbook) most-recently-modified first, each with
    title, section, created/modified dates, whether it contains ink, and a short
    preview of any typed text. Use this to resolve references like "my most recent
    notes" or to disambiguate similar titles, then pass the chosen page id to
    render_page_ink / get_page_ink. Read-only.

    Args:
        query: Optional case-insensitive substring to filter page titles.
        limit: Max pages to return (default 25).
        include_preview: Default False for a fast title/date/section listing. Set True
            to also fetch an ink flag + typed-text preview per page (slower: one request
            per page) - use only when you need to disambiguate similar pages.

    Returns:
        JSON list of pages with metadata (and preview/ink info if requested).
    """
    key = f"find:{query}:{limit}:{include_preview}"
    try:
        return await CACHE.listing(
            key, None, lambda: _find_pages_impl(query=query, limit=limit, include_preview=include_preview)
        )
    except Exception as e:
        # Errors propagate as exceptions so they are never cached for the TTL.
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

def main():
    """Main entry point for the server."""
    # Log token caching configuration
    cache_status = "enabled" if TOKEN_CACHE_ENABLED else "disabled"
    logger.info(f"OneNote MCP Server starting - Token caching: {cache_status}")

    logger.info(f"Authority: {AUTHORITY}")
    if TOKEN_CACHE_ENABLED:
        logger.info(f"Token cache file: {TOKEN_CACHE_FILE}")
    # Initialize MSAL and load any cached tokens; persist the cache on exit.
    try:
        get_msal_app()
    except Exception as e:
        logger.info(f"MSAL not initialized at startup (AZURE_CLIENT_ID may be unset): {e}")
    atexit.register(_persist_cache)

    mcp.run()

if __name__ == "__main__":
    main()
