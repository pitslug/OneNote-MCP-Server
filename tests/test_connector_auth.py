"""Connector OAuth (OIDC proxy) wiring: dual static-bearer/OAuth auth and
build_http_app mode selection."""
import pytest

import onenote_mcp_server as srv
import server_entry
from server_entry import _static_access_token, DualAuthOIDCProxy, build_http_app


# ---- static token acceptance --------------------------------------------------

def test_static_access_token_accepts_matching_token():
    tok = _static_access_token("sekrit", "sekrit")
    assert tok is not None
    assert tok.client_id == "onenote-static-bearer"


def test_static_access_token_carries_required_scopes():
    """The scope-enforcement middleware must not reject the static bearer when
    the OIDC proxy declares required_scopes."""
    tok = _static_access_token("sekrit", "sekrit", scopes=["openid", "profile"])
    assert tok is not None
    assert tok.scopes == ["openid", "profile"]


def test_static_access_token_rejects_mismatch_and_unset():
    assert _static_access_token("wrong", "sekrit") is None
    assert _static_access_token("anything", None) is None
    assert _static_access_token("", "sekrit") is None


async def test_dual_proxy_prefers_static_then_falls_back(monkeypatch):
    """The static ONENOTE_API_TOKEN keeps working when OAuth is enabled; anything
    else is handed to the OIDC proxy's own verification."""
    sentinel = object()

    async def fake_oidc_load(self, token):
        return sentinel

    monkeypatch.setattr(
        DualAuthOIDCProxy.__mro__[1], "load_access_token", fake_oidc_load
    )

    proxy = DualAuthOIDCProxy.__new__(DualAuthOIDCProxy)  # skip network-touching init
    proxy._static_token = "sekrit"
    proxy.required_scopes = ["openid", "profile", "email"]

    static = await proxy.load_access_token("sekrit")
    assert static is not None and static.client_id == "onenote-static-bearer"
    assert static.scopes == ["openid", "profile", "email"]

    other = await proxy.load_access_token("some-oauth-jwt")
    assert other is sentinel


# ---- build_http_app mode selection ---------------------------------------------

@pytest.fixture()
def restore_mcp_auth():
    before = srv.mcp.auth
    yield
    srv.mcp.auth = before


def test_build_http_app_without_oidc_keeps_bearer_gate(monkeypatch, restore_mcp_auth):
    """No OIDC env -> exactly today's behavior: Gateway holds the bearer gate."""
    monkeypatch.setenv("ONENOTE_API_TOKEN", "sekrit")
    monkeypatch.delenv("ONENOTE_OIDC_CONFIG_URL", raising=False)

    gateway, _inner = build_http_app()
    assert gateway._token == "sekrit"


def test_build_http_app_with_oidc_moves_gate_inward(monkeypatch, restore_mcp_auth):
    """OIDC env -> FastMCP auth enforces (static token via DualAuthOIDCProxy);
    the Gateway must NOT 401 first, or the OAuth routes become unreachable."""
    captured = {}

    from fastmcp.server.auth import StaticTokenVerifier

    class StubProxy(StaticTokenVerifier):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(tokens={})

    monkeypatch.setattr(server_entry, "DualAuthOIDCProxy", StubProxy)
    monkeypatch.setenv("ONENOTE_API_TOKEN", "sekrit")
    monkeypatch.setenv("ONENOTE_OIDC_CONFIG_URL",
                       "https://id.example.net/.well-known/openid-configuration")
    monkeypatch.setenv("ONENOTE_OIDC_CLIENT_ID", "onenote-mcp")
    monkeypatch.setenv("ONENOTE_OIDC_CLIENT_SECRET", "oidc-secret")
    monkeypatch.setenv("ONENOTE_PUBLIC_BASE_URL", "https://mcp.example.net")

    gateway, _inner = build_http_app()

    assert gateway._token is None, "bearer gate must move inward when OAuth is on"
    assert isinstance(srv.mcp.auth, StubProxy)
    assert captured["config_url"] == "https://id.example.net/.well-known/openid-configuration"
    assert captured["client_id"] == "onenote-mcp"
    assert captured["client_secret"] == "oidc-secret"
    assert captured["base_url"] == "https://mcp.example.net"
    assert captured["static_token"] == "sekrit"
    # Pocket-ID rejects an authorize request without scopes ("Scope is required"),
    # so the proxy must declare them (advertised via .well-known and forwarded).
    assert captured["required_scopes"] == ["openid", "profile", "email"]


def test_build_http_app_oidc_scopes_overridable(monkeypatch, restore_mcp_auth):
    captured = {}

    from fastmcp.server.auth import StaticTokenVerifier

    class StubProxy(StaticTokenVerifier):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(tokens={})

    monkeypatch.setattr(server_entry, "DualAuthOIDCProxy", StubProxy)
    monkeypatch.setenv("ONENOTE_OIDC_CONFIG_URL",
                       "https://id.example.net/.well-known/openid-configuration")
    monkeypatch.setenv("ONENOTE_OIDC_CLIENT_ID", "onenote-mcp")
    monkeypatch.setenv("ONENOTE_OIDC_CLIENT_SECRET", "oidc-secret")
    monkeypatch.setenv("ONENOTE_PUBLIC_BASE_URL", "https://mcp.example.net")
    monkeypatch.setenv("ONENOTE_OIDC_SCOPES", "openid groups")

    build_http_app()
    assert captured["required_scopes"] == ["openid", "groups"]


def test_build_http_app_half_configured_oidc_refuses_to_boot(monkeypatch, restore_mcp_auth):
    """A config URL without credentials must fail loudly, not boot ungated."""
    monkeypatch.setenv("ONENOTE_OIDC_CONFIG_URL",
                       "https://id.example.net/.well-known/openid-configuration")
    monkeypatch.delenv("ONENOTE_OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("ONENOTE_OIDC_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("ONENOTE_PUBLIC_BASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="ONENOTE_OIDC"):
        build_http_app()
