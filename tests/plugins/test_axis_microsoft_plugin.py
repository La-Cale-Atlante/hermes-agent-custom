"""axis-microsoft plugin — tool additif Microsoft Graph authentifié par la plateforme Axis.

Sans réseau : on patche `httpx.post` (mint) et `httpx.request` (API Graph). On vérifie le gating
additif, le mint (Bearer), la réutilisation du token en cache, et la surface d'erreur."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import plugins.axis_microsoft as plugin
from plugins.axis_microsoft import tools


def _resp(status: int, json_data=None, text=None):
    return SimpleNamespace(
        status_code=status,
        json=lambda: (json_data if json_data is not None else {}),
        text=(text if text is not None else json.dumps(json_data or {})),
    )


def _provision(monkeypatch):
    monkeypatch.setenv("AXIS_CONNECTORS_URL", "http://c4.local:8400")
    monkeypatch.setenv("AXIS_CONNECTORS_INTERNAL_KEY", "k")
    monkeypatch.setenv("AXIS_TENANT_ID", "acme")
    monkeypatch.setenv("AXIS_USER_ID", "u1")
    tools._TOKEN_CACHE.clear()


def test_check_requirements_gates_on_provisioning(monkeypatch):
    monkeypatch.delenv("AXIS_CONNECTORS_URL", raising=False)
    monkeypatch.delenv("AXIS_CONNECTORS_INTERNAL_KEY", raising=False)
    assert tools.check_microsoft_requirements() is False
    _provision(monkeypatch)
    assert tools.check_microsoft_requirements() is True


def test_register_registers_microsoft_graph_only():
    calls = []

    class _Ctx:
        def register_tool(self, **kw):
            calls.append(kw["name"])

    plugin.register(_Ctx())
    assert calls == ["microsoft_graph"]


def test_handler_refuses_when_not_provisioned(monkeypatch):
    monkeypatch.delenv("AXIS_CONNECTORS_URL", raising=False)
    assert "non provisionné" in tools.handle_microsoft_graph({"path": "/me/messages"})


def test_handler_validates_path(monkeypatch):
    _provision(monkeypatch)
    assert "path requis" in tools.handle_microsoft_graph({"path": "me/messages"})


def test_handler_mints_bearer_then_calls_graph_and_caches(monkeypatch):
    _provision(monkeypatch)
    mint_calls, api_calls = [], []

    def _fake_post(url, headers=None, json=None, timeout=None):
        mint_calls.append({"url": url, "headers": headers, "json": json})
        return _resp(200, {"token": "eyJACC", "expires_in": 3600})

    def _fake_request(method, url, headers=None, json=None, timeout=None):
        api_calls.append({"method": method, "url": url, "headers": headers})
        return _resp(200, {"value": []}, text='{"value": []}')

    with patch.object(tools.httpx, "post", _fake_post), \
         patch.object(tools.httpx, "request", _fake_request):
        out1 = tools.handle_microsoft_graph({"path": "/me/messages?$top=5"})
        out2 = tools.handle_microsoft_graph({"path": "/me/events"})

    assert mint_calls[0]["url"].endswith("/internal/connectors/microsoft/token")
    assert mint_calls[0]["json"] == {"tenant_id": "acme", "user_id": "u1"}
    assert len(mint_calls) == 1                                   # token réutilisé (cache)
    assert api_calls[0]["headers"]["Authorization"] == "Bearer eyJACC"
    assert api_calls[0]["url"] == "https://graph.microsoft.com/v1.0/me/messages?$top=5"
    assert "HTTP 200" in out1 and "HTTP 200" in out2


def test_handler_surfaces_api_error(monkeypatch):
    _provision(monkeypatch)

    def _fake_post(url, headers=None, json=None, timeout=None):
        return _resp(200, {"token": "t", "expires_in": 3600})

    def _fake_request(method, url, headers=None, json=None, timeout=None):
        return _resp(403, {"error": {"message": "Forbidden"}}, text='{"error":{"message":"Forbidden"}}')

    with patch.object(tools.httpx, "post", _fake_post), \
         patch.object(tools.httpx, "request", _fake_request):
        out = tools.handle_microsoft_graph({"path": "/me/messages"})
    assert "HTTP 403" in out


def test_handler_surfaces_mint_refusal(monkeypatch):
    _provision(monkeypatch)

    def _fake_post(url, headers=None, json=None, timeout=None):
        return _resp(409, {"message": "microsoft non connecté"}, text='{"message":"microsoft non connecté"}')

    with patch.object(tools.httpx, "post", _fake_post):
        out = tools.handle_microsoft_graph({"path": "/me/messages"})
    assert "mint refusé" in out and "409" in out
