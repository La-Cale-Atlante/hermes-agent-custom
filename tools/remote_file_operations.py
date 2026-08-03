"""``RemoteHttpFileOperations`` — backend fichier qui parle à la Soute (satellite fichier Axis).

Fork de la couche fichier d'Hermès (D-22, contrat C-SOUTE) : au lieu du filesystem local, chaque
op fichier fait un get/push HTTP vers la Soute, autorisé par un GRANT workspace signé par C4
(présenté en header ``Authorization: Grant <jwt>``). Activé par ``HERMES_FILE_BACKEND=remote_http``.

Principe (C-SOUTE §5bis) :
- byte I/O (read raw / write / delete / move / list / search) → API Soute ;
- logique riche RÉUTILISÉE d'Hermès, agent-side, HORS LLM : pagination (``normalize_read_pagination``
  + gutter ``N|``), fuzzy (``fuzzy_match``), V4A (``patch_parser``, marche via ``self``), EOL/BOM ;
- confinement porté par la Soute (périmètre du grant), pas ici ;
- LSP/lint-shell désactivés en remote (pas de FS local à analyser) — le gate syntaxe in-proc
  (JSON/YAML/TOML) reste actif à l'écriture ; ``search`` délègue à la Soute (fidélité réduite vs rg).
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import posixpath
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from tools.file_operations import (
    LINTERS_INPROC,
    _FAIL_CLOSED_INPROC_EXTS,
    FileOperations,
    PatchResult,
    ReadResult,
    SearchMatch,
    SearchResult,
    WriteResult,
    _detect_line_ending,
    _normalize_line_endings,
    _strip_bom,
    normalize_read_pagination,
    normalize_search_pagination,
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}

# Transport HTTP : (method, url, body|None, headers) -> (status:int, data:bytes). Injectable en test.
Transport = Callable[[str, str, Optional[bytes], dict], tuple]


def _b64url_json(segment: str) -> dict:
    pad = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + pad).decode("utf-8"))


def _prefix_from_grant(grant: str) -> str:
    """Extrait le claim ``prefix`` du grant (payload JWT, SANS vérif — la Soute vérifie)."""
    try:
        return str(_b64url_json(grant.split(".")[1]).get("prefix", "")).strip("/")
    except Exception:
        return ""


def _urllib_transport(method: str, url: str, body: Optional[bytes], headers: dict) -> tuple:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        return 0, str(exc).encode("utf-8")


class RemoteHttpFileOperations(FileOperations):
    def __init__(self, base_url: str, grant: str, prefix: Optional[str] = None,
                 transport: Optional[Transport] = None) -> None:
        self._base = base_url.rstrip("/")
        self._grant = grant
        self._prefix = (prefix if prefix is not None else _prefix_from_grant(grant)).strip("/")
        self._transport = transport or _urllib_transport
        self.cwd = "/"  # compat cache _get_file_ops (record_session_cwd)

    @classmethod
    def from_env(cls) -> "RemoteHttpFileOperations":
        base = os.environ.get("HERMES_SOUTE_URL", "")
        grant = os.environ.get("HERMES_WORKSPACE_GRANT", "")
        if not base or not grant:
            raise ValueError("HERMES_SOUTE_URL et HERMES_WORKSPACE_GRANT requis pour remote_http")
        return cls(base, grant, os.environ.get("HERMES_WORKSPACE_PREFIX") or None)

    # ---------------- plomberie HTTP ----------------

    def _logical(self, path: str) -> str:
        """Chemin logique Soute = <prefix workspace> + <chemin relatif de l'outil>."""
        p = (path or "").lstrip("/")
        p = posixpath.normpath(p) if p else p
        pfx = f"{self._prefix}/" if self._prefix else ""
        if pfx and (p == self._prefix or p.startswith(pfx)):
            return p
        return f"{pfx}{p}"

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"Authorization": f"Grant {self._grant}"}
        if extra:
            h.update(extra)
        return h

    def _call(self, method: str, logical: str, *, params: Optional[dict] = None,
              body: Optional[bytes] = None, headers: Optional[dict] = None) -> tuple:
        url = f"{self._base}/v1/files/{urllib.parse.quote(logical)}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._transport(method, url, body, self._headers(headers))

    # ---------------- lecture ----------------

    def read_file_raw(self, path: str) -> ReadResult:
        status, data = self._call("GET", self._logical(path), params={"raw": "true"})
        if status == 404:
            return ReadResult(error=f"File not found: {path}")
        if status != 200:
            return ReadResult(error=f"Failed to read file {path}: HTTP {status}")
        if self._is_image(path):
            return ReadResult(is_image=True, is_binary=True, file_size=len(data))
        if b"\x00" in data[:1000]:
            return ReadResult(is_binary=True, file_size=len(data),
                              error="Binary file — cannot display as text.")
        content, _ = _strip_bom(data.decode("utf-8", errors="replace"))
        return ReadResult(content=content, file_size=len(data))

    def read_file(self, path: str, offset: int = 1, limit: int = 2000) -> ReadResult:
        offset, limit = normalize_read_pagination(offset, limit)
        raw = self.read_file_raw(path)
        if raw.error or raw.is_binary or raw.is_image:
            return raw
        lines = raw.content.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]
        total_lines = len(lines)
        end_line = offset + limit - 1
        page = lines[offset - 1:end_line]
        truncated = total_lines > end_line
        hint = None
        if truncated:
            hint = (f"Use offset={end_line + 1} to continue reading "
                    f"(showing {offset}-{end_line} of {total_lines} lines)")
        return ReadResult(
            content=self._add_line_numbers("\n".join(page), offset),
            total_lines=total_lines, file_size=raw.file_size, truncated=truncated, hint=hint,
        )

    # ---------------- écriture ----------------

    def write_file(self, path: str, content: str) -> WriteResult:
        # Gate syntaxe fail-closed in-proc (JSON/YAML/TOML) — même doctrine que ShellFileOperations,
        # exécutable sans FS local. LSP/lint-shell : non applicables en remote.
        ext = os.path.splitext(path)[1].lower()
        gate = LINTERS_INPROC.get(ext) if ext in _FAIL_CLOSED_INPROC_EXTS else None
        if gate is not None:
            ok, err = gate(content)
            if not ok and err != "__SKIP__":
                return WriteResult(error=f"Refused write to {path}: content does not parse ({err})")
        data = content.encode("utf-8")
        status, resp = self._call("PUT", self._logical(path), body=data,
                                  headers={"Content-Type": "application/octet-stream"})
        if status not in (200, 201):
            return WriteResult(error=f"Failed to write {path}: HTTP {status} {resp[:200]!r}")
        return WriteResult(bytes_written=len(data))

    def patch_replace(self, path: str, old_string: str, new_string: str,
                      replace_all: bool = False) -> PatchResult:
        raw = self.read_file_raw(path)
        if raw.error:
            return PatchResult(error=raw.error)
        content = raw.content
        from tools.fuzzy_match import fuzzy_find_and_replace
        new_content, match_count, _strategy, error = fuzzy_find_and_replace(
            content, old_string, new_string, replace_all)
        if error or match_count == 0:
            from tools.fuzzy_match import is_already_applied
            if is_already_applied(content, old_string, new_string):
                return PatchResult(success=True, no_change=True, note=(
                    f"File already contains the target text — edit appears already applied to "
                    f"{path}. No write performed; do not re-send this patch."))
            err_msg = error or f"Could not find match for old_string in {path}"
            try:
                from tools.fuzzy_match import format_no_match_hint
                err_msg += format_no_match_hint(err_msg, match_count, old_string, content)
            except Exception:
                pass
            return PatchResult(error=err_msg)
        ending = _detect_line_ending(content)
        if ending:
            new_content = _normalize_line_endings(new_content, ending)
        w = self.write_file(path, new_content)
        if w.error:
            return PatchResult(error=f"Failed to write changes: {w.error}")
        diff = "".join(difflib.unified_diff(
            content.splitlines(keepends=True), new_content.splitlines(keepends=True),
            fromfile=path, tofile=path))
        return PatchResult(success=True, diff=diff, files_modified=[path])

    def patch_v4a(self, patch_content: str) -> PatchResult:
        from tools.patch_parser import apply_v4a_operations, parse_v4a_patch
        operations, parse_error = parse_v4a_patch(patch_content)
        if parse_error:
            return PatchResult(error=f"Failed to parse patch: {parse_error}")
        # apply_v4a_operations opère via SELF (read_file_raw/write_file/delete_file/move_file) → HTTP.
        return apply_v4a_operations(operations, self)

    # ---------------- delete / move ----------------

    def delete_file(self, path: str) -> WriteResult:
        status, resp = self._call("DELETE", self._logical(path))
        if status == 404:
            return WriteResult()  # déjà absent = idempotent
        if status not in (200, 204):
            return WriteResult(error=f"Failed to delete {path}: HTTP {status}")
        return WriteResult()

    def move_file(self, src: str, dst: str) -> WriteResult:
        body = json.dumps({"op": "move", "dest": self._logical(dst)}).encode("utf-8")
        status, resp = self._call("POST", self._logical(src), body=body,
                                  headers={"Content-Type": "application/json"})
        if status != 200:
            return WriteResult(error=f"Failed to move {src} -> {dst}: HTTP {status}")
        return WriteResult()

    # ---------------- search (délégué à la Soute, fidélité réduite vs rg) ----------------

    def search(self, pattern: str, path: str = ".", target: str = "content",
               file_glob: Optional[str] = None, limit: int = 50, offset: int = 0,
               output_mode: str = "content", context: int = 0) -> SearchResult:
        limit, offset = normalize_search_pagination(limit, offset)
        prefix = self._logical("" if path in (".", "") else path)
        url = f"{self._base}/v1/files:search"
        q = urllib.parse.urlencode({"q": pattern, "prefix": prefix, "limit": limit, "offset": offset})
        status, data = self._transport("GET", f"{url}?{q}", None, self._headers())
        if status != 200:
            return SearchResult(error=f"search failed: HTTP {status}")
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            return SearchResult(error="search: réponse illisible")
        matches = [SearchMatch(path=m["path"], line_number=m.get("line", 0),
                               content=m.get("text", "")) for m in payload.get("matches", [])]
        if output_mode == "files_with_matches":
            files = sorted({m.path for m in matches})
            return SearchResult(files=files, total_count=len(files),
                                truncated=bool(payload.get("truncated")))
        return SearchResult(matches=matches, total_count=len(matches),
                            truncated=bool(payload.get("truncated")))

    # ---------------- helpers ----------------

    def _is_image(self, path: str) -> bool:
        return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS

    def _add_line_numbers(self, content: str, start_line: int = 1) -> str:
        """Gutter compact ``N|CONTENT`` — identique à ShellFileOperations._add_line_numbers."""
        try:
            from tools.tool_output_limits import get_max_line_length
            max_line_length = get_max_line_length()
        except Exception:
            max_line_length = 2000
        out = []
        for i, line in enumerate(content.split("\n"), start=start_line):
            if len(line) > max_line_length:
                line = line[:max_line_length] + "... [truncated]"
            out.append(f"{i}|{line}")
        return "\n".join(out)


__all__ = ["RemoteHttpFileOperations"]
