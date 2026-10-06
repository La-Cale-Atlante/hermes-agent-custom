"""axis-github plugin — tool additif GitHub authentifié par la plateforme Axis.

Sans réseau : on patche `httpx.post` (mint) et `httpx.request` (API GitHub). On vérifie le gating
additif (check_fn), le mint scopé au repo, la réutilisation du token en cache, et la surface
d'erreur (non provisionné / path invalide / refus API)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import plugins.axis_github as plugin
from plugins.axis_github import tools


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


# ---- gating additif ----

def test_check_requirements_gates_on_provisioning(monkeypatch):
    monkeypatch.delenv("AXIS_CONNECTORS_URL", raising=False)
    monkeypatch.delenv("AXIS_CONNECTORS_INTERNAL_KEY", raising=False)
    assert tools.check_github_requirements() is False        # non provisionné → tool absent
    _provision(monkeypatch)
    assert tools.check_github_requirements() is True


def test_register_registers_github_api_only():
    calls = []

    class _Ctx:
        def register_tool(self, **kw):
            calls.append(kw["name"])

    plugin.register(_Ctx())
    assert calls == ["github_api"]                            # additif : un seul tool, rien d'autre touché


# ---- handler : validation & surface d'erreur ----

def test_handler_refuses_when_not_provisioned(monkeypatch):
    monkeypatch.delenv("AXIS_CONNECTORS_URL", raising=False)
    out = tools.handle_github_api({"path": "/repos/o/r/issues"})
    assert "non provisionné" in out


def test_handler_validates_path(monkeypatch):
    _provision(monkeypatch)
    out = tools.handle_github_api({"path": "repos/o/r"})      # pas de '/' initial
    assert "path requis" in out


# ---- happy path : mint scopé + appel API + cache ----

def test_handler_mints_scoped_then_calls_api_and_caches(monkeypatch):
    _provision(monkeypatch)
    mint_calls, api_calls = [], []

    def _fake_post(url, headers=None, json=None, timeout=None):
        mint_calls.append({"url": url, "headers": headers, "json": json})
        return _resp(200, {"token": "ghs_abc", "expires_at": "2099-01-01T00:00:00Z"})

    def _fake_request(method, url, headers=None, json=None, timeout=None):
        api_calls.append({"method": method, "url": url, "headers": headers, "json": json})
        status = 201 if method == "POST" else 200
        return _resp(status, {"number": 42}, text='{"number": 42}')

    with patch.object(tools.httpx, "post", _fake_post), \
         patch.object(tools.httpx, "request", _fake_request):
        out1 = tools.handle_github_api(
            {"method": "POST", "path": "/repos/o/r/issues", "repository": "o/r",
             "body": {"title": "x"}})
        out2 = tools.handle_github_api(
            {"method": "GET", "path": "/repos/o/r/issues", "repository": "o/r"})

    # mint scopé au repo demandé (least-privilege)
    assert mint_calls[0]["json"]["repositories"] == ["o/r"]
    assert mint_calls[0]["headers"]["X-Internal-Key"] == "k"
    # token réutilisé pour le 2e appel (même scope) → un SEUL mint
    assert len(mint_calls) == 1
    # API frappée avec le token minté, sur la bonne URL
    assert api_calls[0]["headers"]["Authorization"] == "token ghs_abc"
    assert api_calls[0]["url"] == "https://api.github.com/repos/o/r/issues"
    assert api_calls[0]["method"] == "POST" and api_calls[1]["method"] == "GET"
    assert "HTTP 201" in out1 and "HTTP 200" in out2


def test_handler_surfaces_api_error(monkeypatch):
    _provision(monkeypatch)

    def _fake_post(url, headers=None, json=None, timeout=None):
        return _resp(200, {"token": "t", "expires_at": "2099-01-01T00:00:00Z"})

    def _fake_request(method, url, headers=None, json=None, timeout=None):
        return _resp(404, {"message": "Not Found"}, text='{"message":"Not Found"}')

    with patch.object(tools.httpx, "post", _fake_post), \
         patch.object(tools.httpx, "request", _fake_request):
        out = tools.handle_github_api({"path": "/repos/o/missing", "repository": "o/missing"})
    assert "HTTP 404" in out and "Not Found" in out


def test_handler_surfaces_mint_refusal(monkeypatch):
    _provision(monkeypatch)

    def _fake_post(url, headers=None, json=None, timeout=None):
        return _resp(409, {"message": "github non connecté"}, text='{"message":"github non connecté"}')

    with patch.object(tools.httpx, "post", _fake_post):
        out = tools.handle_github_api({"path": "/repos/o/r/issues", "repository": "o/r"})
    assert "mint refusé" in out and "409" in out
