"""axis-github — un tool GitHub authentifié par un token COURT minté par la plateforme Axis.

Le token n'est jamais dans le sandbox : à chaque appel, le tool demande à la plateforme (service
connecteurs C4, endpoint interne `POST /internal/connectors/github/token`) un **installation token
GitHub court, scopé au repo** (least-privilege), puis frappe l'API REST GitHub avec. Le modèle ne
voit jamais de credential. Axis détient l'auth.

Gating (additif) : le tool n'est disponible que si la plateforme a provisionné le connecteur pour ce
run — `AXIS_CONNECTORS_URL` + `AXIS_CONNECTORS_INTERNAL_KEY` présents (posés par le Timonier). Absent
⇒ le tool ne s'enregistre pas (aucun autre tool n'est touché).

Variables d'env (posées par le Timonier depuis le payload de mission) :
  AXIS_CONNECTORS_URL          base du service connecteurs (ex. http://c4.axis-platform.svc:8400)
  AXIS_CONNECTORS_INTERNAL_KEY clé interne (header X-Internal-Key)
  AXIS_TENANT_ID / AXIS_USER_ID identité au nom de laquelle on mint
  AXIS_GITHUB_API_BASE         optionnel — défaut https://api.github.com (GitHub Enterprise sinon)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("axis.github")

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
_RESPONSE_MAX_CHARS = 8000        # borne l'injection de contexte (réponses API volumineuses)
_TOKEN_REFRESH_MARGIN_S = 60      # re-mint si le token expire dans moins de ça

# Cache process-local des tokens mintés, par scope (repository ou "*"). Évite de re-frapper la
# plateforme/GitHub à chaque appel (un token d'installation vit ~1 h). {scope: (token, expiry_epoch)}
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _connectors_url() -> str:
    return (os.environ.get("AXIS_CONNECTORS_URL") or "").rstrip("/")


def _internal_key() -> str:
    return os.environ.get("AXIS_CONNECTORS_INTERNAL_KEY") or ""


def check_github_requirements() -> bool:
    """Le tool n'existe que si la plateforme a provisionné le connecteur (URL + clé interne)."""
    return bool(_connectors_url() and _internal_key())


def _api_base() -> str:
    return (os.environ.get("AXIS_GITHUB_API_BASE") or "https://api.github.com").rstrip("/")


def _expiry_epoch(expires_at: str) -> float:
    """ISO-8601 GitHub (`2026-01-01T00:00:00Z`) → epoch ; repli 50 min si non parsable."""
    try:
        dt = datetime.fromisoformat((expires_at or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return time.time() + 50 * 60


def _mint_token(repository: Optional[str]) -> str:
    """Installation token court scopé (tenant, user[, repo]) via le service connecteurs Axis.

    Cache par scope. Lève `RuntimeError` (message présentable) si la plateforme refuse."""
    scope = repository or "*"
    cached = _TOKEN_CACHE.get(scope)
    now = time.time()
    if cached and cached[1] - _TOKEN_REFRESH_MARGIN_S > now:
        return cached[0]

    url = f"{_connectors_url()}/internal/connectors/github/token"
    payload: dict = {"tenant_id": os.environ.get("AXIS_TENANT_ID") or "",
                     "user_id": os.environ.get("AXIS_USER_ID") or ""}
    if repository:
        payload["repositories"] = [repository]
    try:
        r = httpx.post(url, headers={"X-Internal-Key": _internal_key()},
                       json=payload, timeout=_MINT_TIMEOUT)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"service connecteurs injoignable : {exc}") from exc
    if r.status_code != 200:
        detail = _safe_detail(r)
        raise RuntimeError(f"mint refusé (HTTP {r.status_code}) : {detail}")
    data = r.json()
    token = data.get("token")
    if not token:
        raise RuntimeError("réponse de mint sans token")
    _TOKEN_CACHE[scope] = (token, _expiry_epoch(data.get("expires_at", "")))
    return token


def _safe_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("message") or body.get("error") or body)[:200]
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:200]


GITHUB_API_SCHEMA = {
    "name": "github_api",
    "description": (
        "Call the GitHub REST API as the connected GitHub account, authenticated by Axis "
        "(a short-lived, repo-scoped token is minted for you — you never handle a credential). "
        "Use it to read or act on GitHub: issues, pull requests, comments, repository contents, "
        "reviews, etc. You compose the REST call. Examples: list issues → method='GET', "
        "path='/repos/OWNER/REPO/issues'; open an issue → method='POST', "
        "path='/repos/OWNER/REPO/issues', body={\"title\":\"...\",\"body\":\"...\"}; comment on a "
        "PR → method='POST', path='/repos/OWNER/REPO/issues/NUMBER/comments', body={\"body\":\"...\"}. "
        "Always pass 'repository' as 'OWNER/REPO' so the token is scoped to just that repo."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "GitHub REST API path, starting with '/'. Ex: '/repos/OWNER/REPO/issues'.",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"],
                "description": "HTTP method (default GET).",
            },
            "repository": {
                "type": "string",
                "description": (
                    "'OWNER/REPO' the call targets. Scopes the minted token to this repo only "
                    "(least-privilege). Strongly recommended for every call."
                ),
            },
            "body": {
                "type": "object",
                "description": "JSON body for POST/PATCH/PUT (object). Omit for GET/DELETE.",
            },
        },
        "required": ["path"],
    },
}


def handle_github_api(args: dict, **_kw) -> str:
    """Frappe l'API GitHub avec un token minté à la volée. Ne lève jamais (contrat tool → string)."""
    if not check_github_requirements():
        return tool_error("connecteur github non provisionné pour ce run (AXIS_CONNECTORS_URL absent).")
    path = str((args or {}).get("path") or "").strip()
    if not path.startswith("/"):
        return tool_error("path requis, doit commencer par '/' (ex: '/repos/OWNER/REPO/issues').")
    method = str((args or {}).get("method") or "GET").upper()
    if method not in _ALLOWED_METHODS:
        return tool_error(f"method invalide : {method} (attendu : {', '.join(sorted(_ALLOWED_METHODS))}).")
    repository = (args or {}).get("repository") or None
    body = (args or {}).get("body")

    try:
        token = _mint_token(repository)
    except RuntimeError as exc:
        return tool_error(f"github : {exc}")

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{_api_base()}{path}"
    try:
        r = httpx.request(method, url, headers=headers,
                          json=body if body not in (None, "") else None, timeout=_API_TIMEOUT)
    except httpx.HTTPError as exc:
        return tool_error(f"github injoignable : {exc}")

    text = r.text or ""
    truncated = text[:_RESPONSE_MAX_CHARS]
    note = "" if len(text) <= _RESPONSE_MAX_CHARS else f" (tronqué à {_RESPONSE_MAX_CHARS} c.)"
    ok = 200 <= r.status_code < 300
    summary = f"HTTP {r.status_code} {method} {path}{note}"
    if ok:
        return tool_result(summary, body=truncated)
    return tool_error(f"{summary} — {_safe_detail(r)}", body=truncated)


__all__ = ["GITHUB_API_SCHEMA", "handle_github_api", "check_github_requirements"]
