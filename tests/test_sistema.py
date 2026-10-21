"""Painel de configuração: preferências, usuários locais, troca de senha, informações e backup."""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from simied.api import create_app
from simied.manager import Manager


@pytest.fixture()
def cli(tmp_path):
    mgr = Manager(tmp_path, "192.0.2.1")  # fora do loopback: login obrigatório
    mgr.auth.add_user("chefe", "senha-chefe", "administrador")
    mgr.auth.add_user("oper", "senha-oper", "operacao")
    c = TestClient(create_app(mgr), raise_server_exceptions=False)

    def login(u, p):
        r = c.post("/api/v1/login", json={"usuario": u, "senha": p})
        return {"Authorization": "Bearer " + r.json()["token"]} if r.status_code == 200 else None
    return c, login, mgr


def test_preferencias(cli):
    c, login, mgr = cli
    adm, op = login("chefe", "senha-chefe"), login("oper", "senha-oper")
    r = c.get("/api/v1/settings", headers=op).json()
    assert r["novo_ied"] == {"perfil": "auto", "porta": 102, "max_associacoes": 3} and r["sage"]["opmsk_padrao"] == "10C830"
    assert c.put("/api/v1/settings", json={"sage": {"opmsk_padrao": "1"}}, headers=op).status_code == 403
    r = c.put("/api/v1/settings", json={"sage": {"opmsk_padrao": "0x30"}, "novo_ied": {"perfil": "ingeteam-ef-i15", "porta": 1102}},
              headers=adm)
    assert r.status_code == 200, r.text
    assert r.json()["sage"]["opmsk_padrao"] == "30" and r.json()["novo_ied"]["porta"] == 1102
    for body, txt in [({"sage": {"opmsk_padrao": "1000000"}}, "24 bits"), ({"novo_ied": {"porta": 0}}, "entre"),
                      ({"novo_ied": {"perfil": "xpto"}}, "não existe"), ({"xx": {}}, "seção"), ({"sage": {"y": 1}}, "campo")]:
        r = c.put("/api/v1/settings", json=body, headers=adm)
        assert r.status_code == 422 and txt in r.json()["erro"], (body, r.text)
    assert (mgr.data_dir / "config.json").exists()
    assert c.get("/api/v1/settings", headers=adm).json()["novo_ied"]["perfil"] == "ingeteam-ef-i15"


def test_usuarios(cli):
    c, login, mgr = cli
    adm, op = login("chefe", "senha-chefe"), login("oper", "senha-oper")
    assert c.get("/api/v1/users", headers=op).status_code == 403
    assert c.get("/api/v1/system", headers=op).status_code == 403
    r = c.get("/api/v1/users", headers=adm).json()
    assert {u["usuario"] for u in r["usuarios"]} == {"chefe", "oper"} and r["eu"] == "chefe"
    assert c.post("/api/v1/users", json={"usuario": "novo", "senha": "curta", "papel": "leitura"}, headers=adm).status_code == 422
    assert c.post("/api/v1/users", json={"usuario": "a b", "senha": "senha-longa"}, headers=adm).status_code == 422
    assert c.post("/api/v1/users", json={"usuario": "novo", "senha": "senha-longa", "papel": "leitura"}, headers=adm).status_code == 200
    assert c.post("/api/v1/users", json={"usuario": "novo", "senha": "senha-longa"}, headers=adm).status_code == 409
    leitor = login("novo", "senha-longa")
    assert c.get("/api/v1/eu", headers=leitor).json()["papel"] == "leitura"
    # leitura pode trocar a própria senha
    assert c.post("/api/v1/me/password", json={"atual": "errada", "nova": "outra-senha"}, headers=leitor).status_code == 403
    assert c.post("/api/v1/me/password", json={"atual": "senha-longa", "nova": "outra-senha"}, headers=leitor).status_code == 200
    assert login("novo", "senha-longa") is None and login("novo", "outra-senha")
    # mudar o papel encerra os logins daquele usuário
    assert c.patch("/api/v1/users/oper", json={"papel": "leitura"}, headers=adm).status_code == 200
    assert c.get("/api/v1/eu", headers=op).status_code == 401
    # último administrador é protegido; ninguém se remove
    assert c.patch("/api/v1/users/chefe", json={"papel": "operacao"}, headers=adm).status_code == 409
    assert c.delete("/api/v1/users/chefe", headers=adm).status_code == 409
    assert c.delete("/api/v1/users/novo", headers=adm).status_code == 200
    assert login("novo", "outra-senha") is None
    assert c.delete("/api/v1/users/nao-existe", headers=adm).status_code == 404


def test_sistema_e_backup(cli):
    c, login, mgr = cli
    adm = login("chefe", "senha-chefe")
    i = c.get("/api/v1/system", headers=adm).json()
    assert i["autenticacao"] is True and i["ldap"] is None and i["ieds"] == 0 and i["disco"]["banco"] > 0
    c.put("/api/v1/settings", json={"sage": {"opmsk_padrao": "30"}}, headers=adm)
    r = c.get("/api/v1/backup", headers=adm)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert {"simied.db", "config.json", "LEIAME.txt"} <= set(z.namelist())
    assert c.get("/api/v1/backup", headers=login("oper", "senha-oper")).status_code == 403


def test_opmsk_bits(cli):
    c, login, mgr = cli
    b = c.get("/api/v1/opmsk/bits", headers=login("oper", "senha-oper")).json()["bits"]
    assert len(b) == 24 and b[5]["valor"] == "20" and b[23]["valor"] == "800000" and "LLN0" in b[5]["efeito"]


def test_logout_invalida_o_token(cli):
    c, login, mgr = cli
    mgr.auth.add_user("leitor", "senha-leitor", "leitura")
    for u, pw in (("chefe", "senha-chefe"), ("leitor", "senha-leitor")):
        h = login(u, pw)
        assert c.get("/api/v1/eu", headers=h).status_code == 200
        assert c.post("/api/v1/logout", headers=h).status_code == 200
        assert c.get("/api/v1/eu", headers=h).status_code == 401
