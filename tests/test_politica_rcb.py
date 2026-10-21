"""Política de acesso por RCB (reprodução do Ingeteam i15): respostas por item, reports só dos RCBs
habilitados, IPs permitidos, ocultar/instanciar, mudança com o IED rodando e persistência."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets
from conftest import (BOOL, CTRL, IND3, ROOT, UINT, VIS, ahttp, connect, drain, next_report, on, run, start_server,
                      stop_server, write1)

from simied.mms.client import MmsClientError
from simied.mms.types import Bits, MmsType, enc_data

CID_RCBS = ROOT / "exemplos" / "SE_BAB_H06_BCM1_i15_rcbs_reais_ed1.cid"
BITS10 = MmsType("bits", 10)
BITS6 = MmsType("bits", 6)
N2 = "LLN0$BR$BRCB_ESTADOS_N2"
N3 = "LLN0$BR$BRCB_ESTADOS_N3"
SAGE = "LLN0$RP$BRCB_ESTADOS_SAGE"
MED_N2 = "LLN0$RP$URCB_MEDIDAS_N2"
MED_N3 = "LLN0$RP$URCB_MEDIDAS_N3"
DS = {N2: "DS_ESTADOS_N2", N3: "DS_ESTADOS_N3", SAGE: "DS_ESTADOS_SAGE", MED_N2: "DS_MEDIDAS_N2",
      MED_N3: "DS_MEDIDAS_N3"}
E2, E3 = 2, 3


def sage_attrs(rcb: str, rptid: str) -> list[tuple]:
    """Atributos na ordem em que o SAGE escreve num só Write (Resv só existe no URCB)."""
    out = [] if "$BR$" in rcb else [("Resv", BOOL, True)]
    return out + [("RptID", VIS, rptid), ("DatSet", VIS, f"{CTRL}/LLN0${DS[rcb]}"),
                  ("OptFlds", BITS10, Bits.from_list(10, [1, 2, 3, 4])), ("BufTm", UINT, 0),
                  ("TrgOps", BITS6, Bits.from_list(6, [1, 2, 5])), ("IntgPd", UINT, 0), ("GI", BOOL, True)]


async def write_attrs(c, rcb: str, attrs: list[tuple]) -> list:
    return await c.write([on(CTRL, f"{rcb}${a}") for a, _, _ in attrs], [enc_data(t, v) for _, t, v in attrs])


async def configure(c, rcb: str, rptid: str) -> tuple[list, int | None]:
    """Sequência do SAGE: escreve os atributos e depois o RptEna; devolve (resultados, resultado do RptEna)."""
    res = await write_attrs(c, rcb, sage_attrs(rcb, rptid))
    ena = await write1(c, CTRL, f"{rcb}$RptEna", BOOL, True)
    return res, ena


def policy(sim, iid: int, nome: str, **body):
    return sim.h.patch(f"/ieds/{iid}/rcbs/{nome}/policy", json=body)


def create(sim, **kw) -> dict:
    return sim.create(cid=CID_RCBS, **kw)


# ---------------------------------------------------------------------------- respostas por item
def test_i15_respostas_por_item_e_reports(sim):
    ied = create(sim)
    iid = ied["id"]
    ws_url = sim.base.replace("http", "ws") + "/api/v1/ws"

    async def go():
        ws = await websockets.connect(ws_url)
        c = await connect(ied["porta"])
        for rcb in (N2, N3):
            res, ena = await configure(c, rcb, "SAGE_X")
            assert res == [E2, E3, E2, E2, E2, E2, E2], rcb  # RptID, DatSet, OptFlds, BufTm, TrgOps, IntgPd, GI
            assert ena == E3, rcb
            assert await write1(c, CTRL, f"{rcb}$PurgeBuf", BOOL, True) == E2  # "demais" atributos
        res, ena = await configure(c, SAGE, "SAGE_ID_1_1")
        assert res == [None, None, E3, None, None, None, None, E2]  # Resv ... GI
        assert ena is None
        for rcb in (MED_N2, MED_N3):
            res, ena = await configure(c, rcb, "SAGE_MED")
            assert res == [None, None, E3, None, None, None, None, E2], rcb
            assert ena is None, rcb
        await drain(c)
        # todos os RCBs pedidos; só os habilitados (SAGE e medidas) enviam, e a MOLA só está no SAGE e em N2/N3
        await ahttp(sim.set, iid, IND3, valor=True)
        rpts = await drain(c, 0.6)
        assert [r["RptID"] for r in rpts] == ["SAGE_ID_1_1"]
        # evento rcb.write_denied no WebSocket
        seen = []
        end = time.time() + 3
        while time.time() < end and not seen:
            batch = json.loads(await asyncio.wait_for(ws.recv(), 3))
            seen = [m for m in batch if m["evento"] == "rcb.write_denied" and m["ied"] == iid]
        assert seen and {"rcb", "atributo", "codigo", "ip", "ts"} <= set(seen[0]["dados"])
        st = {r["nome"]: r for r in (await ahttp(sim.h.get, f"/ieds/{iid}/rcbs")).json()["rcbs"]}
        assert st["BRCB_ESTADOS_SAGE"]["cliente"].startswith("127.0.0.1:")  # quem habilitou
        await c.close()
        await ws.close()
    run(go())
    rows = sim.h.get(f"/ieds/{iid}/rcb-denied").json()["recusas"]
    n2 = [r for r in rows if r["rcb"].endswith(N2)]
    assert {(r["atributo"], r["codigo"]) for r in n2} >= {("RptID", 2), ("DatSet", 3), ("GI", 2), ("RptEna", 3)}
    assert all(r["ip"] == "127.0.0.1" and r["ts"] > 0 for r in n2)
    rcbs = {r["nome"]: r for r in sim.h.get(f"/ieds/{iid}/rcbs").json()["rcbs"]}
    assert rcbs["BRCB_ESTADOS_N2"]["estado_politica"] == "ocupado"
    assert rcbs["BRCB_ESTADOS_N2"]["recusadas"] == 9 and not rcbs["BRCB_ESTADOS_N2"]["RptEna"]
    rep = sim.reports(iid, 1)
    assert [r["rptid"] for r in rep if not r["descartado"]] == ["SAGE_ID_1_1"]


def test_mudar_politica_com_ied_rodando_e_derrubar(sim):
    ied = create(sim)
    iid = ied["id"]

    async def go():
        c = await connect(ied["porta"])
        await configure(c, SAGE, "SAGE_ID")
        assert await write1(c, CTRL, f"{N2}$RptEna", BOOL, True) == E3
        r = await ahttp(policy, sim, iid, "BRCB_ESTADOS_N2", politica="livre")
        assert r.status_code == 200 and r.json()["politica"] == "livre" and r.json()["origem"]["politica"] == "ied"
        assert await write1(c, CTRL, f"{N2}$RptEna", BOOL, True) is None  # próxima escrita já segue a política nova
        await drain(c)
        await ahttp(sim.set, iid, IND3, valor=True)
        rpts = await drain(c, 0.6)
        assert sorted(r["RptID"] for r in rpts) == ["CTRL/LLN0.BR.BRCB_ESTADOS_N2", "SAGE_ID"]
        # derrubar a habilitação: N2 deixa de enviar na hora
        r = await ahttp(sim.h.post, f"/ieds/{iid}/rcbs/BRCB_ESTADOS_N2/disable-client")
        assert r.status_code == 200 and r.json()["derrubados"][0]["cliente"].startswith("127.0.0.1:")
        await ahttp(sim.set, iid, IND3, valor=False)
        rpts = await drain(c, 0.6)
        assert [r["RptID"] for r in rpts] == ["SAGE_ID"]
        # PATCH com derrubar=true: volta a ocupado e derruba o SAGE
        r = await ahttp(policy, sim, iid, "BRCB_ESTADOS_SAGE", politica="ocupado", derrubar=True)
        assert r.status_code == 200 and not r.json()["instancias"][0]["habilitado"]
        await ahttp(sim.set, iid, IND3, valor=True)
        assert await next_report(c, 0.5) is None
        assert await write1(c, CTRL, f"{SAGE}$RptEna", BOOL, True) == E3
        await c.close()
    run(go())


# ---------------------------------------------------------------------------- IPs permitidos
def test_ips_permitidos(sim):
    ied = create(sim)
    iid = ied["id"]

    async def go():
        c = await connect(ied["porta"])
        assert (await ahttp(policy, sim, iid, "BRCB_ESTADOS_N2", ips=["10.9.9.9"])).status_code == 200
        assert await write1(c, CTRL, f"{N2}$RptID", VIS, "X") == E2  # fora da lista: política (ocupado)
        assert await write1(c, CTRL, f"{N2}$RptEna", BOOL, True) == E3
        assert (await ahttp(policy, sim, iid, "BRCB_ESTADOS_N2", ips=["127.0.0.1"])).status_code == 200
        assert await write1(c, CTRL, f"{N2}$RptID", VIS, "X") is None  # dentro: livre
        assert await write1(c, CTRL, f"{N2}$RptEna", BOOL, True) is None
        # RCB livre com lista de IPs: quem está fora é negado (senão a lista não teria efeito)
        assert (await ahttp(policy, sim, iid, "URCB_MEDIDAS_N2", ips=["10.9.9.9"])).status_code == 200
        assert await write1(c, CTRL, f"{MED_N2}$RptID", VIS, "X") == E3
        await c.close()
    run(go())


# ---------------------------------------------------------------------------- ocultar e instanciar
def test_ocultar_e_instanciar(sim):
    ied = create(sim)
    iid = ied["id"]

    async def go():
        c = await connect(ied["porta"])
        names = await c.get_name_list(0, CTRL)
        assert N2 in names and SAGE in names and not any(n.startswith(SAGE + "0") for n in names)
        assert (await ahttp(policy, sim, iid, "BRCB_ESTADOS_N2", ocultar=True)).status_code == 200
        names = await c.get_name_list(0, CTRL)
        assert not any(n == N2 or n.startswith(N2 + "$") for n in names)
        assert "LLN0$BR$BRCB_ESTADOS_N3" in names and "LLN0$BR" in names
        with pytest.raises(MmsClientError):
            await c.get_var_attr(on(CTRL, N2))
        assert b"BRCB_ESTADOS_N2" not in await c.get_var_attr(on(CTRL, "LLN0"))
        assert b"BRCB_ESTADOS_N3" in await c.get_var_attr(on(CTRL, "LLN0"))
        assert (await c.read([on(CTRL, f"{N2}$RptID")]))[0][1] == "CTRL/LLN0.BR.BRCB_ESTADOS_N2"  # segue no modelo
        await c.close()
    run(go())
    r = policy(sim, iid, "BRCB_ESTADOS_SAGE", instanciar=True)
    assert r.status_code == 409 and r.json()["codigo"] == "confirmar"  # muda o modelo: reinicia o IED
    r = policy(sim, iid, "BRCB_ESTADOS_SAGE", instanciar=True, confirmar=True)
    assert r.status_code == 200 and [i["nome"] for i in r.json()["instancias"]] == \
        [f"BRCB_ESTADOS_SAGE{i:02d}" for i in range(1, 7)]

    async def go2():
        c = await connect(ied["porta"])
        names = await c.get_name_list(0, CTRL)
        assert [f"{SAGE}{i:02d}" for i in range(1, 7)] == [n for n in names if n.startswith(SAGE) and "$" not in n[len(SAGE):]]
        assert SAGE not in names
        assert not any(n.startswith(N2) for n in names)  # 'ocultar' sobrevive à recarga do modelo
        # a política vale para todas as instâncias
        assert await write1(c, CTRL, f"{SAGE}03$GI", BOOL, True) == E2
        await c.close()
    run(go2())


def test_validacao_da_api(sim):
    iid = create(sim, start=False)["id"]
    for body, trecho in [({"politica": "xpto"}, "política 'xpto' inválida"),
                         ({"atributos": {"Foo": "ok"}}, "atributo de RCB desconhecido"),
                         ({"atributos": {"GI": "erro9"}}, "resposta 'erro9' inválida"),
                         ({"ips": ["abc"]}, "IP inválido"), ({"ocultar": "sim"}, "'ocultar' deve ser"),
                         ({"cor": "azul"}, "campos desconhecidos")]:
        r = policy(sim, iid, "BRCB_ESTADOS_N2", **body)
        assert r.status_code == 422 and trecho in r.json()["erro"], (body, r.text)
    r = policy(sim, iid, "NAO_EXISTE", politica="livre")
    assert r.status_code == 404 and "NAO_EXISTE" in r.json()["erro"]
    assert sim.h.post(f"/ieds/{iid}/rcbs/NAO_EXISTE/disable-client").status_code == 404
    g = sim.h.get(f"/ieds/{iid}/rcbs/BRCB_ESTADOS_SAGE/policy").json()
    assert g["politica"] == "livre" and g["atributos"] == {"DatSet": "erro3", "GI": "erro2"}
    assert g["instanciar"] is False and g["origem"]["atributos"] == "perfil"
    # a sobrescrita do IED pode voltar ao perfil
    policy(sim, iid, "BRCB_ESTADOS_N2", politica="negado")
    g = policy(sim, iid, "BRCB_ESTADOS_N2", restaurar_perfil=True).json()
    assert g["politica"] == "ocupado" and g["origem"]["politica"] == "perfil"


def test_politica_em_lote(sim):
    ied = create(sim)
    iid = ied["id"]
    bulk = lambda **b: sim.h.patch(f"/ieds/{iid}/rcbs/policy", json=b)  # noqa: E731
    # tudo validado antes: um nome inexistente não altera nenhum RCB
    r = bulk(rcbs=["BRCB_ESTADOS_N2", "NAO_EXISTE"], politica="livre")
    assert r.status_code == 404 and "NAO_EXISTE" in r.json()["erro"] and "nada foi alterado" in r.json()["erro"]
    assert sim.h.get(f"/ieds/{iid}/rcbs/BRCB_ESTADOS_N2/policy").json()["politica"] == "ocupado"
    assert bulk(rcbs=["BRCB_ESTADOS_N2"], politica="xpto").status_code == 422
    assert bulk(rcbs=["BRCB_ESTADOS_N2"], instanciar=True).status_code == 422
    assert bulk(rcbs=[], politica="livre").status_code == 422
    assert bulk(rcbs=["BRCB_ESTADOS_N2"]).status_code == 422

    async def go():
        c = await connect(ied["porta"])
        r = await ahttp(bulk, rcbs=["BRCB_ESTADOS_N2", "BRCB_ESTADOS_N3"], politica="livre")
        assert r.status_code == 200 and [x["politica"] for x in r.json()["rcbs"]] == ["livre", "livre"]
        for rcb in (N2, N3):
            assert await write1(c, CTRL, f"{rcb}$RptEna", BOOL, True) is None
        # de volta a ocupado em lote, derrubando quem estava habilitado
        r = await ahttp(bulk, rcbs=["BRCB_ESTADOS_N2", "BRCB_ESTADOS_N3"], politica="ocupado", derrubar=True)
        assert r.status_code == 200
        assert not any(i["habilitado"] for x in r.json()["rcbs"] for i in x["instancias"])
        for rcb in (N2, N3):
            assert await write1(c, CTRL, f"{rcb}$RptEna", BOOL, True) == E3
        await c.close()
    run(go())


def test_perfil_generico_mantem_comportamento_atual(sim):
    ied = create(sim, perfil="generico-ed1")

    async def go():
        c = await connect(ied["porta"])
        assert await write1(c, CTRL, f"{N2}$RptEna", BOOL, True) is None  # sem política: livre
        await c.close()
    run(go())


# ---------------------------------------------------------------------------- persistência
def test_politicas_sobrevivem_ao_reinicio(tmp_path):
    proc, s = start_server(tmp_path)
    try:
        iid = s.create(cid=CID_RCBS, start=False)["id"]
        r = s.h.patch(f"/ieds/{iid}/rcbs/BRCB_ESTADOS_N3/policy",
                      json={"politica": "negado", "atributos": {"RptID": "ok"}, "ips": ["10.1.1.1"], "ocultar": True})
        assert r.status_code == 200, r.text
    finally:
        stop_server(proc)
    proc, s = start_server(tmp_path)
    try:
        g = s.h.get(f"/ieds/{iid}/rcbs/BRCB_ESTADOS_N3/policy").json()
        assert (g["politica"], g["atributos"], g["ips"], g["ocultar"]) == ("negado", {"RptID": "ok"}, ["10.1.1.1"], True)
        st = {r["nome"]: r for r in s.h.get(f"/ieds/{iid}/rcbs").json()["rcbs"]}
        assert st["BRCB_ESTADOS_N3"]["estado_politica"] == "negado"
        assert st["BRCB_ESTADOS_N2"]["estado_politica"] == "ocupado"  # do perfil
    finally:
        stop_server(proc)
