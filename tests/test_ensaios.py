"""Ensaios da seção 13 automatizáveis sem o SAGE físico (servidor real em loopback).

T1, T2, T4, T5, T6, T8, T9 e T10 rodam aqui com o cliente MMS do projeto fazendo o papel do SAGE.
T3 e T7 dependem do SAGE físico e ficam para o ensaio em campo.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from conftest import (BOOL, CID_I15, CID_REF, CTRL, I15, IND3, UINT, VIS, ahttp, connect, drain, enable,
                      free_port, next_report, on, run, write1)

from simied.mms.client import MmsClientError

URCB_SAGE = "LLN0$RP$urcbSAGE"
BRCB_EST = "LLN0$BR$brcbEstados"
URCB_EST = "LLN0$RP$urcbEstados"


def _val(rpt, k=0):
    """(stVal, q, t) do k-ésimo membro incluído."""
    st, q, t = rpt["valores"][k][1]
    return st[1], q[1], t[1]


# ---------------------------------------------------------------------------- T1
def test_t1_associa_le_datasets_e_pontos(sim):
    ied = sim.create()
    assert ied["sage_hosts"].startswith("127.0.0.1\t" + I15)  # RN-NET-07

    async def go():
        c = await connect(ied["porta"])
        fab, modelo, _ = await c.identify()
        assert (fab, modelo) == ("Ingeteam", "INGEPAC EF")
        assert await c.get_name_list(9) == [CTRL, I15 + "MED"]
        nvls = await c.get_name_list(2, CTRL)
        assert "LLN0$DS_ESTADOS_SAGE" in nvls and "LLN0$DS_ESTADOS_N2" in nvls
        membros = await c.get_nvl_attr(on(CTRL, "LLN0$DS_ESTADOS_N2"))
        assert membros[:3] == [f"{CTRL}/sAutGGIO2$ST$Ind{i}" for i in (1, 2, 3)]  # ordem do CID
        vals = await c.read_nvl(on(CTRL, "LLN0$DS_ESTADOS_N2"))
        assert len(vals) == len(membros)
        st, q, t = vals[2][1]
        assert q[1].encode_value() == b"\x03\x00\x00"
        assert t[1].secs > 1_700_000_000, "t inicial não pode ser 1970"
        # ponto inexistente -> object-non-existent (código 10)
        r = await c.read([on(CTRL, "sAutGGIO2$ST$Ind99")])
        assert r[0] == ("error", 10)
        await c.close()
    run(go())


def test_ip_do_cid_e_conflito_de_porta(sim):  # RN-NET-03, RN-NET-04
    up = sim.upload(CID_I15)
    r = sim.h.post("/ieds", json={"token": up["token"], "ip": "10.237.68.177", "nome": "ip_cid"})
    assert r.status_code == 400 and "RN-NET-03" in r.json()["erro"]
    a = sim.create()
    b = sim.create(port=a["porta"], start=False)
    r = sim.h.post(f"/ieds/{b['id']}/start")
    assert r.status_code == 409 and "RN-NET-04" in r.json()["erro"]


# ---------------------------------------------------------------------------- T2
def test_t2_mola_gera_report_no_formato_esperado(sim):
    ied = sim.create()
    iid = ied["id"]

    async def go():
        c = await connect(ied["porta"])
        await enable(c, URCB_SAGE + "01", "SAGE_ID_1_1")
        await drain(c)
        t0 = time.time()
        r = await ahttp(sim.set, iid, IND3, valor=True)
        assert r.status_code == 200
        rpt = await next_report(c)
        lat = time.time() - t0
        assert rpt is not None
        assert lat < 0.5  # critério: até 100 ms com BufTm=0 (inclui o HTTP do teste)
        assert rpt["RptID"] == "SAGE_ID_1_1"  # RN-RCB-11
        assert rpt["DatSet"] == f"{CTRL}/LLN0$DS_ESTADOS_SAGE"
        assert rpt["ConfRev"] == 1
        assert rpt["incluidos"] == [0]  # RN-RCB-12: só o membro alterado
        assert rpt["ReasonCode"] == ["010000"]  # dchg
        st, q, t = _val(rpt)
        assert st is True and abs(t.to_float() - time.time()) < 5 and t.tq == 0x8B
        raw = c.raw_reports[-1]
        assert raw[0] == 0xA3  # unconfirmed-PDU
        assert b"\x84\x03\x03\x00\x00" in raw and b"\x91\x08" in raw  # q e utc-time como no relé
        # RN-DIG-03: mesmo valor não gera report
        await ahttp(sim.set, iid, IND3, valor=True)
        assert await next_report(c, 0.5) is None
        # qchg e dchg+qchg num único report (RN-DIG-02)
        await ahttp(sim.set, iid, IND3, qualidade={"validity": "invalid"})
        assert (await next_report(c))["ReasonCode"] == ["001000"]
        await ahttp(sim.set, iid, IND3, valor=False, qualidade={"validity": "good"})
        rpt = await next_report(c)
        assert rpt["ReasonCode"] == ["011000"]
        assert await next_report(c, 0.3) is None
        await c.close()
    run(go())
    hist = sim.h.get(f"/ieds/{iid}/history").json()["mudancas"]
    assert hist[0]["origem"] == "manual" and hist[0]["reports"]  # RN-DIG-10
    rep = sim.reports(iid, 3)
    assert rep[0]["pdu"] and rep[0]["rptid"] == "SAGE_ID_1_1"  # critério: PDU no histórico


def test_rcb_atributos_conf_e_gi(sim):  # RN-RCB-05, RN-RCB-06, RN-RCB-07
    ied = sim.create()

    async def go():
        a = await connect(ied["porta"])
        b = await connect(ied["porta"])
        assert await write1(a, CTRL, URCB_SAGE + "01$DatSet", VIS, f"{CTRL}/LLN0$DS_ESTADOS_N2") == 3
        await enable(a, URCB_SAGE + "01", "X")
        assert await write1(b, CTRL, URCB_SAGE + "01$RptID", VIS, "Y") == 2  # habilitado por outro
        assert await write1(a, CTRL, URCB_SAGE + "01$IntgPd", UINT, 1000) == 2  # habilitado
        assert await write1(a, CTRL, URCB_SAGE + "01$GI", BOOL, True) is None
        rpt = await next_report(a)
        assert rpt["incluidos"] == list(range(len(rpt["incluidos"]))) and len(rpt["incluidos"]) == 2
        assert set(rpt["ReasonCode"]) == {"000001"}  # GI
        await a.close()
        await b.close()
    run(go())


def test_brcb_guarda_e_reenvia(sim):  # RN-RCB-02
    ied = sim.create()
    iid = ied["id"]

    async def go():
        c = await connect(ied["porta"])
        await enable(c, BRCB_EST + "01", "BR1")
        await drain(c)
        await c.close()
        await asyncio.sleep(0.3)
        for v in (True, False, True):
            await ahttp(sim.set, iid, IND3, valor=v)
        c = await connect(ied["porta"])
        await enable(c, BRCB_EST + "01")
        got = await drain(c, 0.5)
        assert [_val(r)[0] for r in got] == [True, False, True]
        assert all(r["EntryID"] != "00" * 8 for r in got)
        await c.close()
    run(go())



def test_brcb_entryid_cresce_depois_de_recarregar(sim):
    """O SAGE escreve o último EntryID recebido antes do RptEna e descarta reports com EntryID menor: depois
    de recarregar o modelo (reinício do simulador, reimportação), a numeração não pode voltar ao começo."""
    ied = sim.create(start=False)
    iid = ied["id"]
    assert sim.h.post(f"/ieds/{iid}/start").status_code == 200

    async def ultimo_eid():
        c = await connect(ied["porta"])
        await enable(c, BRCB_EST + "01", "BR1")
        await drain(c)
        await ahttp(sim.set, iid, IND3, valor=not sim.point(iid, IND3)["valor"])
        got = await drain(c, 0.5)
        await c.close()
        assert got and all(r["EntryID"] != "00" * 8 for r in got)
        return int(got[-1]["EntryID"], 16)

    antes = run(ultimo_eid())
    up = sim.upload(CID_I15)
    assert sim.h.post(f"/ieds/{iid}/reimport", json={"token": up["token"], "confirmar": True}).status_code == 200
    assert run(ultimo_eid()) > antes


# ---------------------------------------------------------------------------- T4
def test_t4_tres_scadas_em_paralelo(sim):
    ied = sim.create()
    iid = ied["id"]

    async def go():
        cs = [await connect(ied["porta"]) for _ in range(3)]
        for i, c in enumerate(cs, 1):
            await enable(c, f"{URCB_SAGE}{i:02d}", f"SAGE_ID_{i}_1")
        with pytest.raises(MmsClientError):  # 4ª associação: limite de 3 (RN-NET-06)
            await connect(ied["porta"], timeout=2)
        for c in cs:
            await drain(c)
        await ahttp(sim.set, iid, IND3, valor=True)
        rpts = [await next_report(c) for c in cs]
        assert [r["RptID"] for r in rpts] == ["SAGE_ID_1_1", "SAGE_ID_2_1", "SAGE_ID_3_1"]
        assert len({tuple(r["valores"][0][1][2][1].encode_value()) for r in rpts}) == 1  # mesmo t
        sess = (await ahttp(sim.h.get, f"/ieds/{iid}/sessions")).json()["sessoes"]
        assert len(sess) == 3  # RN-NET-11: a tela mostra os clientes associados
        for c in cs:
            await c.close()
    run(go())


# ---------------------------------------------------------------------------- T5
def test_t5_relogio_atrasado_3h(sim):
    ied = sim.create()
    iid = ied["id"]
    r = sim.h.put(f"/ieds/{iid}/clock", json={"modo": "deslocamento", "horas": 3, "negativo": True})
    assert r.json()["diferenca_ied_s"] == -10800

    async def go():
        c = await connect(ied["porta"])
        await enable(c, URCB_SAGE + "01", "T5")
        await drain(c)
        await ahttp(sim.set, iid, IND3, valor=True)
        _, _, t = _val(await next_report(c))
        assert abs(t.to_float() - (time.time() - 10800)) < 5
        await ahttp(sim.h.put, f"/ieds/{iid}/clock", json={"modo": "sistema"})
        await ahttp(sim.set, iid, IND3, valor=False)
        _, _, t = _val(await next_report(c))
        assert abs(t.to_float() - time.time()) < 5
        await c.close()
    run(go())


# ---------------------------------------------------------------------------- T6
def test_t6_ponto_em_dois_datasets(sim):
    ied = sim.create()
    iid = ied["id"]
    p = sim.point(iid, IND3)
    assert sorted(d["ds"].split("$")[-1] for d in p["datasets"]) == ["DS_ESTADOS_N2", "DS_ESTADOS_SAGE"]

    async def go():
        c = await connect(ied["porta"])
        await enable(c, URCB_EST + "01", "N2")
        await enable(c, URCB_SAGE + "01", "SAGE")
        await drain(c)
        await ahttp(sim.set, iid, IND3, valor=True)
        got = {r["RptID"]: r for r in await drain(c)}
        assert set(got) == {"N2", "SAGE"}
        assert got["N2"]["incluidos"] == [2] and got["SAGE"]["incluidos"] == [0]
        await c.close()
    run(go())


# ---------------------------------------------------------------------------- analógicos
def test_analogicos_faixa_e_zona_morta(sim):  # RN-ANA-03, RN-ANA-05
    ied = sim.create()
    iid = ied["id"]
    r = sim.set(iid, "MMXU1$MX$Hz", valor=70)
    assert r.status_code == 422 and r.json()["codigo"] == "fora_da_faixa"
    assert sim.set(iid, "MMXU1$MX$Hz", valor=70, confirmar_fora_faixa=True).status_code == 200
    assert sim.set(iid, "MMXU1$MX$Hz", valor=60).status_code == 200
    sim.set(iid, "MMXU1$MX$Hz", valor=60.01)  # db=100 -> 0,1 % de 20 Hz = 0,02 Hz
    hist = sim.h.get(f"/ieds/{iid}/history").json()["mudancas"]
    assert "zona morta" in hist[0]["origem"]
    assert sim.point(iid, "MMXU1$MX$Hz")["valor"] == pytest.approx(60.01)
    r = sim.set(iid, "MMXU1$MX$A$phsA", valor={"mag": 120.5, "ang": -30})
    assert r.json()["valor"] == {"mag": pytest.approx(120.5), "ang": pytest.approx(-30)}


# ---------------------------------------------------------------------------- T8
def test_t8_espelhamento_de_outro_ied(sim):
    """O IED A faz o papel do relé real; B espelha o Ind3 de A (RN-DIG-04 a RN-DIG-07)."""
    a = sim.create()
    b = sim.create()
    pb = sim.point(b["id"], IND3)
    link = {"ip": "127.0.0.1", "porta": a["porta"], "intervalo_ms": 200, "replica_t": True}
    r = sim.h.patch(f"/ieds/{b['id']}/points/{pb['id']}", json={"modo": "espelhado", "vinculo": link})
    assert r.status_code == 200, r.text
    assert sim.set(b["id"], IND3, valor=True).status_code == 409  # RN-UI-02: chave bloqueada

    def wait(pred, timeout=4.0):
        end = time.time() + timeout
        while time.time() < end:
            p = sim.point(b["id"], IND3)
            if pred(p):
                return p
            time.sleep(0.1)
        raise AssertionError(f"espelho não chegou: {p}")

    sim.set(a["id"], IND3, valor=True)
    ta = sim.point(a["id"], IND3)["t"]
    pb = wait(lambda p: p["valor"] is True)
    assert pb["t"] == ta  # RN-DIG-06: replica o t remoto
    # RN-DIG-07: perda de comunicação -> qualidade inválida, mantém o valor
    sim.h.post(f"/ieds/{a['id']}/stop")
    pb = wait(lambda p: p["qualidade"]["validity"] == "invalid", 10)
    assert pb["valor"] is True
    # forçado ignora o relé e liberar volta ao modo anterior
    sim.h.patch(f"/ieds/{b['id']}/points/{pb['id']}", json={"modo": "forcado", "valor": False})
    assert sim.point(b["id"], IND3)["modo"] == "forcado"
    sim.h.patch(f"/ieds/{b['id']}/points/{pb['id']}", json={"liberar": True})
    assert sim.point(b["id"], IND3)["modo"] == "espelhado"


# ---------------------------------------------------------------------------- T9
def test_t9_falhas(sim):
    ied = sim.create()
    iid, port = ied["id"], ied["porta"]

    async def go():
        c = await connect(port, timeout=1.5)
        await enable(c, URCB_SAGE + "01", "T9")
        await drain(c)
        # RN-FLT-02: descartar todos
        await ahttp(sim.faults, iid, descarte={"ativo": True, "um_a_cada": 1})
        await ahttp(sim.set, iid, IND3, valor=True)
        assert await next_report(c, 0.5) is None
        await ahttp(sim.faults, iid, descarte=False, confrev={"ativo": True, "valor": 999})
        # RN-FLT-04: ConfRev diferente
        await ahttp(sim.set, iid, IND3, valor=False)
        assert (await next_report(c))["ConfRev"] == 999
        # RN-FLT-03: duplicar
        await ahttp(sim.faults, iid, confrev=False, duplicar={"ativo": True, "modo": "duplicar"})
        await ahttp(sim.set, iid, IND3, valor=True)
        assert len(await drain(c)) == 2
        # RN-FLT-01: atraso
        await ahttp(sim.faults, iid, duplicar=False, atraso={"ativo": True, "ms": 400})
        t0 = time.time()
        await ahttp(sim.set, iid, IND3, valor=False)
        assert await next_report(c, 2) is not None and time.time() - t0 >= 0.38
        # RN-FLT-07: Read sem resposta com TCP aberto
        await ahttp(sim.faults, iid, atraso=False, silenciar_read=True)
        with pytest.raises(asyncio.TimeoutError):
            await c.read([on(CTRL, IND3)])
        await ahttp(sim.faults, iid, silenciar_read=False)
        await c.close()
        # RN-FLT-05: recusar e fechar após N s
        await ahttp(sim.faults, iid, recusar_assoc=True)
        with pytest.raises(MmsClientError):
            await connect(port, timeout=2)
        await ahttp(sim.faults, iid, recusar_assoc=False, fechar_apos={"ativo": True, "segundos": 1})
        c = await connect(port)
        await asyncio.sleep(1.5)
        assert not c.connected
        await ahttp(sim.faults, iid, fechar_apos=False)
        # RN-FLT-06: erro fixo na escrita de atributo de RCB
        await ahttp(sim.faults, iid, erro_escrita={"ativo": True, "atributo": "RptEna", "codigo": 3})
        c = await connect(port)
        assert await write1(c, CTRL, URCB_SAGE + "02$RptEna", BOOL, True) == 3
        await c.close()
    run(go())
    rows = sim.reports(iid)
    assert any(r["descartado"] and any("RN-FLT-02" in n for n in r["notas"]) for r in rows)
    assert sim.h.get(f"/ieds/{iid}").json()["falhas_ativas"][0]["regra"] == "RN-FLT-06"


def test_t9_report_unico(sim):  # RN-FLT-08
    ied = sim.create()
    iid = ied["id"]
    sim.faults(iid, report_unico=True)

    async def go():
        cs = [await connect(ied["porta"]) for _ in range(2)]
        for i, c in enumerate(cs, 1):
            await enable(c, f"{URCB_SAGE}{i:02d}", f"C{i}")
            await drain(c)
        await ahttp(sim.set, iid, IND3, valor=True)
        assert await next_report(cs[0]) is not None
        assert await next_report(cs[1], 0.5) is None
        for c in cs:
            await c.close()
    run(go())


# ---------------------------------------------------------------------------- T10
def test_t10_dois_ieds_independentes(sim):
    a = sim.create()
    b = sim.create(cid=CID_REF, perfil="referencia-UCDQSA1", port=free_port())

    async def go():
        ca = await connect(a["porta"])
        cb = await connect(b["porta"])
        assert (await cb.get_name_list(9)) == ["SE_BAB__UCDQSA1LD0"]
        await ahttp(sim.h.post, f"/ieds/{a['id']}/stop")
        await asyncio.sleep(0.3)
        assert not ca.connected
        r = await cb.read([on("SE_BAB__UCDQSA1LD0", "GGIO1$ST$Ind1$stVal")])
        assert r[0] == ("bool", False)
        await cb.close()
    run(go())


# ---------------------------------------------------------------------------- persistência e parada rápida
def test_parada_rapida_restaura_pontos(sim):  # RN-SEG-05
    ied = sim.create()
    iid = ied["id"]
    sim.set(iid, IND3, valor=True)
    r = sim.h.post("/panic").json()
    assert any(x["ied"] == ied["nome"] for x in r["parados"])
    assert sim.point(iid, IND3)["valor"] is False
    assert sim.h.get(f"/ieds/{iid}").json()["estado"] == "parado"


def test_cenario_aplicado_em_sequencia(sim):  # seção 10, cenários
    ied = sim.create()
    iid = ied["id"]
    sc = sim.h.post("/scenarios", json={"nome": "mola", "passos": [
        {"ponto": IND3, "valor": True}, {"ponto": "MMXU1$MX$Hz", "valor": 59.9},
        {"ponto": IND3, "valor": False, "atraso_ms": 300}]}).json()
    r = sim.h.post(f"/scenarios/{sc['id']}/apply", params={"ied": iid}).json()
    assert r == {"passos": 3, "grupos": 2}
    time.sleep(0.1)
    assert sim.point(iid, IND3)["valor"] is True
    time.sleep(0.5)
    assert sim.point(iid, IND3)["valor"] is False
    assert sim.point(iid, "MMXU1$MX$Hz")["valor"] == pytest.approx(59.9)


# ---------------------------------------------------------------------------- desempenho (seção 14)
def test_carga_10_ieds_3_clientes_100_mudancas_por_segundo(sim):
    """10 IEDs × 3 clientes, 100 mudanças/s no total por 3 s, sem perda de report."""
    ieds = [sim.create() for _ in range(10)]
    pids = {i["id"]: sim.point(i["id"], IND3)["id"] for i in ieds}
    n_por_ied = 30

    async def go():
        clients = {}
        for i in ieds:
            cs = [await connect(i["porta"]) for _ in range(3)]
            for k, c in enumerate(cs, 1):
                await enable(c, f"{URCB_SAGE}{k:02d}", f"C{k}")
                await drain(c, 0.05)
            clients[i["id"]] = cs
        t0 = time.time()
        for n in range(n_por_ied):
            for i in ieds:
                await ahttp(sim.h.patch, f"/ieds/{i['id']}/points/{pids[i['id']]}", json={"valor": n % 2 == 0})
            await asyncio.sleep(max(0.0, t0 + (n + 1) * 0.1 - time.time()))
        dur = time.time() - t0
        await asyncio.sleep(0.5)
        for cs in clients.values():
            for c in cs:
                assert c.reports.qsize() == n_por_ied
                await c.close()
        return dur
    dur = run(go())
    assert 10 * n_por_ied / dur >= 90
