"""Blocking MSAL calls must not run on the event loop.

acquire_token_by_device_flow polls until the user completes sign-in (minutes);
acquire_token_silent / initiate_device_flow do network I/O. Run inline they freeze
every concurrent request of the HTTP server, including Docker health checks.
"""
import json
import threading
import time

import pytest

import onenote_mcp_server as srv

MAIN_THREAD = threading.get_ident()

TOKEN_RESULT = {"access_token": "tok-123", "expires_in": 3600}


class FakeMsalApp:
    def __init__(self, flow=None):
        self.thread_ids = {}
        self.silent_calls = 0
        self._flow = flow or {
            "user_code": "ABC123",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "message": "Go to https://microsoft.com/devicelogin and enter ABC123",
        }

    def get_accounts(self):
        return [{"username": "user@example.com"}]

    def acquire_token_silent(self, scopes, account=None):
        self.silent_calls += 1
        self.thread_ids["silent"] = threading.get_ident()
        return dict(TOKEN_RESULT)

    def initiate_device_flow(self, scopes=None):
        self.thread_ids["initiate"] = threading.get_ident()
        return dict(self._flow)

    def acquire_token_by_device_flow(self, flow):
        self.thread_ids["by_device_flow"] = threading.get_ident()
        return dict(TOKEN_RESULT)


@pytest.fixture()
def fake_app(monkeypatch):
    app = FakeMsalApp()
    monkeypatch.setattr(srv, "msal_app", app)
    monkeypatch.setattr(srv, "access_token", None)
    monkeypatch.setattr(srv, "token_expires_at", None)
    monkeypatch.setattr(srv, "current_flow", None)
    return app


async def test_acquire_token_silent_runs_off_event_loop(fake_app):
    assert await srv.ensure_valid_token() is True
    assert srv.access_token == "tok-123"
    assert fake_app.thread_ids["silent"] != MAIN_THREAD


async def test_fresh_token_short_circuits_msal(fake_app, monkeypatch):
    monkeypatch.setattr(srv, "access_token", "still-good")
    monkeypatch.setattr(srv, "token_expires_at", time.time() + 1000)
    assert await srv.ensure_valid_token() is True
    assert fake_app.silent_calls == 0


async def test_start_authentication_initiates_off_loop(fake_app):
    out = json.loads(await srv.start_authentication.fn())
    assert out["status"] == "authentication_required"
    assert out["user_code"] == "ABC123"
    assert fake_app.thread_ids["initiate"] != MAIN_THREAD


async def test_complete_authentication_polls_off_loop(fake_app, monkeypatch):
    async def fake_me(endpoint, method="GET", data=None):
        return {"displayName": "Test User", "mail": "user@example.com"}

    monkeypatch.setattr(srv, "make_graph_request", fake_me)
    monkeypatch.setattr(srv, "current_flow", {"user_code": "ABC123"})

    out = json.loads(await srv.complete_authentication.fn())
    assert out["status"] == "success"
    assert fake_app.thread_ids["by_device_flow"] != MAIN_THREAD
