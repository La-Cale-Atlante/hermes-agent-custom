"""Fork satellite fichier (D-22) : RemoteHttpFileOperations parle à la Soute.

Transport in-memory qui mime le contrat C-SOUTE (GET raw / PUT / DELETE / POST move / search) —
aucun réseau. Prouve : mapping du préfixe workspace, read paginé (gutter N|), patch fuzzy,
patch V4A (via self), delete/move, search, gate syntaxe, et le seam _get_file_ops.
"""

from __future__ import annotations

import base64
import json
import os
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from tools.remote_file_operations import RemoteHttpFileOperations

PREFIX = "acme/alice/workspace/m-1"


def _grant(prefix: str = PREFIX + "/") -> str:
    """Grant factice : seul le claim `prefix` du payload est lu (la Soute vérifie la signature)."""
    payload = base64.urlsafe_b64encode(json.dumps({"prefix": prefix}).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


class FakeSoute:
    """Backend in-memory conforme au contrat C-SOUTE (chemins logiques → octets)."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def __call__(self, method: str, url: str, body, headers):
        assert headers.get("Authorization", "").startswith("Grant "), "grant manquant"
        parts = urlsplit(url)
        params = parse_qs(parts.query)
        if parts.path == "/v1/files:search":
            q = params["q"][0]
            prefix = params.get("prefix", [""])[0]
            matches = []
            for p, data in sorted(self.files.items()):
                if prefix and not p.startswith(prefix):
                    continue
                for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                    if q in line:
                        matches.append({"path": p, "line": i, "text": line})
            return 200, json.dumps({"matches": matches, "truncated": False}).encode()

        logical = unquote(parts.path[len("/v1/files/"):])
        if method == "GET":
            if logical not in self.files:
                return 404, b'{"code":"not_found"}'
            return 200, self.files[logical]           # raw=true → octets exacts
        if method == "PUT":
            self.files[logical] = body or b""
            return 200, b'{"sha256":"x","size":0}'
        if method == "DELETE":
            existed = self.files.pop(logical, None) is not None
            return (204, b"") if existed else (404, b"")
        if method == "POST":
            payload = json.loads(body)
            if payload.get("op") == "move":
                if logical not in self.files:
                    return 404, b""
                self.files[payload["dest"]] = self.files.pop(logical)
                return 200, b"{}"
        return 400, b""


@pytest.fixture
def soute():
    return FakeSoute()


@pytest.fixture
def ops(soute):
    return RemoteHttpFileOperations("http://soute.test", _grant(), transport=soute)


# ---------------- mapping du préfixe ----------------

def test_prefixe_workspace_applique(ops, soute):
    ops.write_file("report.md", "x")
    assert f"{PREFIX}/report.md" in soute.files          # relatif → préfixé
    ops.write_file(f"{PREFIX}/deja.md", "y")
    assert f"{PREFIX}/deja.md" in soute.files             # déjà préfixé → pas de double


# ---------------- read ----------------

def test_write_puis_read_raw_octets_exacts(ops):
    ops.write_file("f.md", "hello\nworld\n")
    r = ops.read_file_raw("f.md")
    assert r.error is None and r.content == "hello\nworld\n"


def test_read_file_pagination_et_gutter(ops):
    ops.write_file("f.md", "l1\nl2\nl3\n")
    r = ops.read_file("f.md")
    assert r.content == "1|l1\n2|l2\n3|l3" and r.total_lines == 3 and not r.truncated
    r2 = ops.read_file("f.md", offset=2, limit=1)
    assert r2.content == "2|l2" and r2.truncated and "offset=3" in r2.hint


def test_read_file_introuvable(ops):
    assert "File not found" in (ops.read_file("absent.md").error or "")


# ---------------- patch_replace (fuzzy réutilisé) ----------------

def test_patch_replace_ok(ops):
    ops.write_file("f.md", "bonjour monde")
    res = ops.patch_replace("f.md", "monde", "Axis")
    assert res.success and "f.md" in res.files_modified
    assert ops.read_file_raw("f.md").content == "bonjour Axis"


def test_patch_replace_absent(ops):
    ops.write_file("f.md", "abc")
    assert ops.patch_replace("f.md", "zzz", "y").error


def test_patch_replace_old_absent_gere_proprement(ops):
    ops.write_file("f.md", "bonjour Axis")
    res = ops.patch_replace("f.md", "monde", "Axis")  # 'monde' absent
    # géré : soit no-op success (déjà appliqué), soit erreur — jamais un succès-avec-écriture bidon
    assert (res.no_change and res.success) or (res.error and not res.success)
    assert ops.read_file_raw("f.md").content == "bonjour Axis"  # fichier intact


# ---------------- patch_v4a (via self → HTTP) ----------------

def test_patch_v4a_add_file_via_self(ops, soute):
    patch = "*** Begin Patch\n*** Add File: created.md\n+ligne A\n+ligne B\n*** End Patch\n"
    res = ops.patch_v4a(patch)
    assert res.success, res.error
    assert f"{PREFIX}/created.md" in soute.files


# ---------------- delete / move ----------------

def test_delete_idempotent(ops, soute):
    ops.write_file("f.md", "x")
    assert ops.delete_file("f.md").error is None
    assert f"{PREFIX}/f.md" not in soute.files
    assert ops.delete_file("f.md").error is None       # 2e delete = idempotent


def test_move(ops, soute):
    ops.write_file("src.md", "data")
    assert ops.move_file("src.md", "dst.md").error is None
    assert f"{PREFIX}/src.md" not in soute.files
    assert soute.files[f"{PREFIX}/dst.md"] == b"data"


# ---------------- search (délégué) ----------------

def test_search_contenu(ops):
    ops.write_file("a.md", "le chat dort\nle chien court")
    res = ops.search("chat")
    assert len(res.matches) == 1 and res.matches[0].line_number == 1
    assert res.matches[0].content == "le chat dort"


# ---------------- gate syntaxe fail-closed ----------------

def test_write_json_invalide_refuse(ops, soute):
    r = ops.write_file("bad.json", "{ invalide")
    assert r.error and "parse" in r.error
    assert f"{PREFIX}/bad.json" not in soute.files       # rien écrit (fail-closed)


def test_write_json_valide_ok(ops):
    assert ops.write_file("ok.json", '{"a": 1}').error is None


# ---------------- seam _get_file_ops ----------------

def test_seam_bascule_backend(monkeypatch):
    monkeypatch.setenv("HERMES_FILE_BACKEND", "remote_http")
    monkeypatch.setenv("HERMES_SOUTE_URL", "http://soute.test")
    monkeypatch.setenv("HERMES_WORKSPACE_GRANT", _grant())
    import tools.file_tools as ft
    ft.clear_file_ops_cache()
    ops = ft._get_file_ops("task-xyz")
    assert isinstance(ops, RemoteHttpFileOperations)
    ft.clear_file_ops_cache()
