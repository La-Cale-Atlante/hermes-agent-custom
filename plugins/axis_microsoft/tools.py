"""axis-microsoft — un tool Microsoft Graph authentifié par un access token COURT minté par Axis.

Le token n'est jamais dans le sandbox : à chaque appel, le tool demande à la plateforme (service
connecteurs C4, `POST /internal/connectors/microsoft/token`) un **access token délégué court** (issu
du refresh token consenti par l'utilisateur, rotaté côté C4), puis frappe l'API Microsoft Graph avec.
Le modèle ne voit jamais de credential. Axis détient l'auth (client_secret + refresh token restent en
plateforme).

Gating (additif) : le tool n'est disponible que si la plateforme a provisionné le connecteur pour ce
run — `AXIS_CONNECTORS_URL` + `AXIS_CONNECTORS_INTERNAL_KEY` (posés par le Timonier). Absent ⇒ le tool
ne s'enregistre pas (aucun autre tool touché).

Env (posées par le Timonier depuis le payload de mission) :
  AXIS_CONNECTORS_URL / AXIS_CONNECTORS_INTERNAL_KEY  service connecteurs + clé interne
  AXIS_TENANT_ID / AXIS_USER_ID                       identité au nom de laquelle on mint
  AXIS_MSGRAPH_API_BASE                                optionnel — défaut https://graph.microsoft.com/v1.0
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx

logger = logging.getLogger("axis.microsoft")

try:  # surface d'erreur standard du harness ; repli JSON si import indispo
    from tools.registry import tool_error, tool_result
except Exception:  # pragma: no cover - plugin chargé hors harness (tests isolés)
    def tool_error(message: str, **extra) -> str:
        return json.dumps({"error": message, **extra}, ensure_ascii=False)

    def tool_result(data=None, **kwargs) -> str:
        payload = {"result": data} if data is not None else {}
        payload.update(kwargs)
        return json.dumps(payload, ensure_ascii=False)

_ALLOWED_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}
_MINT_TIMEOUT = 10.0
_API_TIMEOUT = 20.0
_RESPONSE_MAX_CHARS = 8000
_TOKEN_REFRESH_MARGIN_S = 60

# Cache process-local de l'access token (un seul scope : le compte de l'utilisateur). {("*"): (token, exp)}
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _connectors_url() -> str:
    return (os.environ.get("AXIS_CONNECTORS_URL") or "").rstrip("/")


def _internal_key() -> str:
    return os.environ.get("AXIS_CONNECTORS_INTERNAL_KEY") or ""


def check_microsoft_requirements() -> bool:
    """Le tool n'existe que si la plateforme a provisionné le connecteur (URL + clé interne)."""
    return bool(_connectors_url() and _internal_key())


def _api_base() -> str:
    return (os.environ.get("AXIS_MSGRAPH_API_BASE") or "https://graph.microsoft.com/v1.0").rstrip("/")


def _mint_token() -> str:
    """Access token Microsoft Graph court (tenant, user) via le service connecteurs Axis.

    Cache (le token vit ~1 h). Lève `RuntimeError` (message présentable) si la plateforme refuse."""
    cached = _TOKEN_CACHE.get("*")
    now = time.time()
    if cached and cached[1] - _TOKEN_REFRESH_MARGIN_S > now:
        return cached[0]

    url = f"{_connectors_url()}/internal/connectors/microsoft/token"
    payload = {"tenant_id": os.environ.get("AXIS_TENANT_ID") or "",
               "user_id": os.environ.get("AXIS_USER_ID") or ""}
    try:
        r = httpx.post(url, headers={"X-Internal-Key": _internal_key()},
                       json=payload, timeout=_MINT_TIMEOUT)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"service connecteurs injoignable : {exc}") from exc
    if r.status_code != 200:
        raise RuntimeError(f"mint refusé (HTTP {r.status_code}) : {_safe_detail(r)}")
    data = r.json()
    token = data.get("token")
    if not token:
        raise RuntimeError("réponse de mint sans token")
    expires_in = int(data.get("expires_in") or 3000)
    _TOKEN_CACHE["*"] = (token, now + expires_in)
    return token


def _safe_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("message") or body.get("error") or body)[:200]
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:200]


MICROSOFT_GRAPH_SCHEMA = {
    "name": "microsoft_graph",
    "description": (
        "Call the Microsoft Graph REST API as the connected Microsoft 365 account, authenticated by "
        "Axis (a short-lived delegated token is minted for you — you never handle a credential). "
        "Use it to read the user's Microsoft 365: emails, chats (Teams), calendar events, profile, "
        "etc. You compose the REST call. Examples: recent emails → method='GET', "
        "path='/me/messages?$top=10'; calendar → method='GET', path='/me/events'; Teams chats → "
        "method='GET', path='/me/chats'. Paths are relative to the Graph API base "
        "(https://graph.microsoft.com/v1.0)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Graph API path, starting with '/'. Ex: '/me/messages?$top=10'.",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"],
                "description": "HTTP method (default GET).",
            },
            "body": {
                "type": "object",
                "description": "JSON body for POST/PATCH/PUT (object). Omit for GET/DELETE.",
            },
        },
        "required": ["path"],
    },
}


def handle_microsoft_graph(args: dict, **_kw) -> str:
    """Frappe l'API Graph avec un token minté à la volée. Ne lève jamais (contrat tool → string)."""
    if not check_microsoft_requirements():
        return tool_error("connecteur microsoft non provisionné pour ce run (AXIS_CONNECTORS_URL absent).")
    path = str((args or {}).get("path") or "").strip()
    if not path.startswith("/"):
        return tool_error("path requis, doit commencer par '/' (ex: '/me/messages?$top=10').")
    method = str((args or {}).get("method") or "GET").upper()
    if method not in _ALLOWED_METHODS:
        return tool_error(f"method invalide : {method} (attendu : {', '.join(sorted(_ALLOWED_METHODS))}).")
    body = (args or {}).get("body")

    try:
        token = _mint_token()
    except RuntimeError as exc:
        return tool_error(f"microsoft : {exc}")

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url = f"{_api_base()}{path}"
    try:
        r = httpx.request(method, url, headers=headers,
                          json=body if body not in (None, "") else None, timeout=_API_TIMEOUT)
    except httpx.HTTPError as exc:
        return tool_error(f"microsoft graph injoignable : {exc}")

    text = r.text or ""
    truncated = text[:_RESPONSE_MAX_CHARS]
    note = "" if len(text) <= _RESPONSE_MAX_CHARS else f" (tronqué à {_RESPONSE_MAX_CHARS} c.)"
    summary = f"HTTP {r.status_code} {method} {path}{note}"
    if 200 <= r.status_code < 300:
        return tool_result(summary, body=truncated)
    return tool_error(f"{summary} — {_safe_detail(r)}", body=truncated)


__all__ = ["MICROSOFT_GRAPH_SCHEMA", "handle_microsoft_graph", "check_microsoft_requirements"]
