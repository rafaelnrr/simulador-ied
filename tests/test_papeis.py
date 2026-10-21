"""Permissões por papel na API: leitura consulta, operacao opera, administrador também configura."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from simied.api import create_app
from simied.manager import Manager

CONFIG = [("POST", "/ieds", {}), ("PATCH", "/ieds/1", {}), ("DELETE", "/ieds/1", None),
          ("POST", "/ieds/1/duplicate", {}), ("POST", "/ieds/1/rcbs", {}), ("DELETE", "/ieds/1/datasets/LD/LLN0/ds", None),
          ("PUT", "/profiles/x", {}), ("DELETE", "/profiles/x", None), ("POST", "/net/alias", {}),
          ("POST", "/cid-editor/ied-1/ops", {}), ("POST", "/cid-editor/ied-1/apply", {}), ("POST", "/cid-editor/ied-1/undo", {}),
          ("POST", "/cid-editor/files", {}), ("DELETE", "/cid-editor/ied-1", None)]
OPERACAO = [("PATCH", "/ieds/1/points/1", {}), ("POST", "/ieds/1/points:batch", {}), ("PUT", "/ieds/1/faults", {}),
            ("PUT", "/ieds/1/clock", {}), ("POST", "/ieds/1/start", {}), ("POST", "/scenarios/1/apply?ied=1", {}),
            ("PATCH", "/ieds/1/rcbs/BRCB_ESTADOS_N2/policy", {}), ("PATCH", "/ieds/1/rcbs/policy", {}), ("POST", "/ieds/1/rcbs/BRCB_ESTADOS_N2/disable-client", {})]


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    mgr = Manager(tmp_path_factory.mktemp("papeis"), "192.0.2.1")  # fora do loopback: autenticação obrigatória
    for papel in ("leitura", "operacao", "administrador"):
        mgr.auth.add_user(papel, "senha-" + papel, papel)
    c = TestClient(create_app(mgr), raise_server_exceptions=False)  # sem lifespan: só o middleware importa aqui
    tok = {p: c.post("/api/v1/login", json={"usuario": p, "senha": "senha-" + p}).json()["token"]
           for p in ("leitura", "operacao", "administrador")}
    return c, tok


def _call(cli, papel, method, path, body):
    c, tok = cli
    return c.request(method, "/api/v1" + path, json=body, headers={"Authorization": "Bearer " + tok[papel]})


@pytest.mark.parametrize("method,path,body", CONFIG)
def test_configuracao_so_admin(cli, method, path, body):
    assert _call(cli, "operacao", method, path, body).status_code == 403
    assert _call(cli, "leitura", method, path, body).status_code == 403
    assert _call(cli, "administrador", method, path, body).status_code != 403


@pytest.mark.parametrize("method,path,body", OPERACAO)
def test_operacao_permitida(cli, method, path, body):
    assert _call(cli, "operacao", method, path, body).status_code != 403
    assert _call(cli, "administrador", method, path, body).status_code != 403
    assert _call(cli, "leitura", method, path, body).status_code == 403


def test_eu(cli):
    assert _call(cli, "administrador", "GET", "/eu", None).json() == {"usuario": "administrador", "papel": "administrador"}


def test_editor_de_cid_consulta_para_todos(cli):
    for papel in ("leitura", "operacao", "administrador"):
        assert _call(cli, papel, "GET", "/cid-editor", None).status_code == 200
        assert _call(cli, papel, "GET", "/cid-editor/ied-1", None).status_code == 404  # IED inexistente, mas não 403
