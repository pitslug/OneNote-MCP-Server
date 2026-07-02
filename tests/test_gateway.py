"""Gateway tests: bearer gate behavior and /auth device-flow single-flight."""
import asyncio
import threading

import pytest

import onenote_mcp_server as srv
from server_entry import Gateway


class Collector:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return self.messages[0]["status"]

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.messages[1:]).decode()


class DummyInner:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})


def _scope(path, auth=None):
    headers = [(b"authorization", auth.encode())] if auth else []
    return {"type": "http", "path": path, "headers": headers}


async def _recv():
    return {"type": "http.request"}


# ---- bearer gate (characterization) -----------------------------------------

async def test_healthz_bypasses_gate():
    gw = Gateway(DummyInner(), token="sekrit", auth_route=False)
    send = Collector()
    await gw(_scope("/healthz"), _recv, send)
    assert send.status == 200


async def test_mcp_requires_bearer():
    gw = Gateway(DummyInner(), token="sekrit", auth_route=False)
    send = Collector()
    await gw(_scope("/mcp"), _recv, send)
    assert send.status == 401


async def test_mcp_with_valid_bearer_passes_through():
    inner = DummyInner()
    gw = Gateway(inner, token="sekrit", auth_route=False)
    send = Collector()
    await gw(_scope("/mcp", auth="Bearer sekrit"), _recv, send)
    assert send.status == 200
    assert inner.calls == 1


# ---- /auth single-flight -----------------------------------------------------

class BlockingMsalApp:
    """Device flow that stays pending until release() is called."""

    def __init__(self):
        self.initiate_calls = 0
        self._release = threading.Event()

    def initiate_device_flow(self, scopes=None):
        self.initiate_calls += 1
        return {"user_code": "XYZ789", "verification_uri": "https://microsoft.com/devicelogin"}

    def acquire_token_by_device_flow(self, flow):
        self._release.wait(timeout=10)
        return {"error_description": "test flow aborted"}

    def release(self):
        self._release.set()


async def test_auth_route_single_flight(monkeypatch):
    """Repeated /auth hits must not stack up blocked device flows (DoS vector)."""
    app = BlockingMsalApp()

    async def not_authed():
        return False

    monkeypatch.setattr(srv, "ensure_valid_token", not_authed)
    monkeypatch.setattr(srv, "get_msal_app", lambda: app)

    gw = Gateway(DummyInner(), token="sekrit", auth_route=True)
    try:
        first = Collector()
        await gw(_scope("/auth"), _recv, first)
        assert first.status == 200
        assert "XYZ789" in first.body

        second = Collector()
        await gw(_scope("/auth"), _recv, second)
        assert app.initiate_calls == 1, "second /auth hit started another device flow"
        assert "in progress" in second.body.lower()
    finally:
        app.release()
        await asyncio.sleep(0.1)  # let the background completion task finish


async def test_auth_route_allows_new_flow_after_completion(monkeypatch):
    app = BlockingMsalApp()

    async def not_authed():
        return False

    monkeypatch.setattr(srv, "ensure_valid_token", not_authed)
    monkeypatch.setattr(srv, "get_msal_app", lambda: app)

    gw = Gateway(DummyInner(), token=None, auth_route=True)
    await gw(_scope("/auth"), _recv, Collector())
    app.release()
    await asyncio.sleep(0.2)  # background task completes, pending flag clears

    again = Collector()
    await gw(_scope("/auth"), _recv, again)
    assert app.initiate_calls == 2, "flow slot never freed after completion"
