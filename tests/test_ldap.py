"""Login via LDAP (RN-UI-06) contra o servidor simulado do ldap3: mesmo layout do diretório da rede TOCOT."""
from __future__ import annotations

import ldap3
import pytest

from simied.auth import Auth, LdapConfig
from simied.storage import Storage

BASE = "dc=redetocot,dc=local"


@pytest.fixture
def auth(tmp_path, monkeypatch):
    srv = ldap3.Server("fake")
    users = {"ana": "senha-ana-1", "bia": "senha-bia-1", "caio": "senha-caio-1", "rui": "senha-rui-1"}
    grupos = {"simied_admin": ["rui"], "simied_operacao": ["ana", "rui"], "simied_leitura": ["ana", "bia"],
              "outros": ["caio"]}
    real = ldap3.Connection

    def fake_conn(_server, user=None, password=None, **kw):
        c = real(srv, user=user, password=password, client_strategy=ldap3.MOCK_SYNC)
        for u, pw in users.items():
            c.strategy.add_entry(f"uid={u},ou=Usuarios,{BASE}",
                                 {"uid": u, "userPassword": pw, "objectClass": ["inetOrgPerson", "posixAccount"]})
        for g, membros in grupos.items():
            c.strategy.add_entry(f"cn={g},ou=Grupos,{BASE}", {"cn": g, "memberUid": membros, "objectClass": "posixGroup"})
        return c

    monkeypatch.setattr(ldap3, "Connection", fake_conn)
    st = Storage(tmp_path / "t.db")
    yield Auth(st, True, LdapConfig("ldap://fake", BASE, ["simied_operacao"], ["simied_leitura"],
                                grupos_admin=["simied_admin"]))
    st.close()


def test_papel_pelo_grupo(auth):
    assert auth.login("rui", "senha-rui-1")[1] == "administrador"  # admin prevalece sobre operacao
    assert auth.login("ana", "senha-ana-1")[1] == "operacao"
    assert auth.login("bia", "senha-bia-1")[1] == "leitura"


def test_recusas(auth):
    assert auth.login("ana", "errada") is None
    assert auth.login("ana", "") is None               # sem bind anônimo
    assert auth.login("caio", "senha-caio-1") is None  # fora dos grupos do simulador
    assert auth.login("ninguem", "x") is None
    assert auth.login("*", "x") is None                # login inválido nem chega ao filtro


def test_token_valido(auth):
    tok, _ = auth.login("bia", "senha-bia-1")
    assert auth.check(tok) == ("bia", "leitura")


def test_local_tem_prioridade(auth):
    auth.add_user("ana", "senha-local-1", "leitura")
    assert auth.login("ana", "senha-local-1")[1] == "leitura"
    assert auth.login("ana", "senha-ana-1") is None
