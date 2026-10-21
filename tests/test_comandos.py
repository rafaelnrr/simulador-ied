"""Serviço de controle (Oper, SBOw, Cancel, LastApplError, CommandTermination), registro de comandos e lógicas."""
from __future__ import annotations

import asyncio
import time

from conftest import CID_I15, CTRL, connect, on, run

from simied.mms.types import Bits, MmsType, UtcTime, enc_data

# CSWI1: sbo-with-enhanced-security (SBOw/Oper/Cancel); CSWI2: direct-with-normal-security (só Oper)
TIPOS = """
    <LNodeType id="CSWI_SBO" lnClass="CSWI"><DO name="Mod" type="INC_Mod"/><DO name="Beh" type="INS_Beh"/><DO name="Pos" type="DPC_SBO"/></LNodeType>
    <LNodeType id="CSWI_DIR" lnClass="CSWI"><DO name="Mod" type="INC_Mod"/><DO name="Beh" type="INS_Beh"/><DO name="Pos" type="DPC_DIR"/></LNodeType>
    <DOType id="DPC_SBO" cdc="DPC">
      <DA name="origin" fc="ST" bType="Struct" type="Originator"/><DA name="ctlNum" fc="ST" bType="INT8U"/>
      <DA name="stVal" fc="ST" bType="Dbpos" dchg="true"><Val>off</Val></DA>
      <DA name="q" fc="ST" bType="Quality" qchg="true"/><DA name="t" fc="ST" bType="Timestamp"/>
      <DA name="stSeld" fc="ST" bType="BOOLEAN" dchg="true"/>
      <DA name="SBOw" fc="CO" bType="Struct" type="Oper_DPC"/><DA name="Oper" fc="CO" bType="Struct" type="Oper_DPC"/>
      <DA name="Cancel" fc="CO" bType="Struct" type="Cancel_DPC"/>
      <DA name="ctlModel" fc="CF" bType="Enum" type="ctlModel"><Val>sbo-with-enhanced-security</Val></DA>
      <DA name="sboTimeout" fc="CF" bType="INT32U"><Val>2000</Val></DA>
      <DA name="operTimeout" fc="CF" bType="INT32U"><Val>1000</Val></DA>
    </DOType>
    <DOType id="DPC_DIR" cdc="DPC">
      <DA name="origin" fc="ST" bType="Struct" type="Originator"/><DA name="ctlNum" fc="ST" bType="INT8U"/>
      <DA name="stVal" fc="ST" bType="Dbpos" dchg="true"><Val>off</Val></DA>
      <DA name="q" fc="ST" bType="Quality" qchg="true"/><DA name="t" fc="ST" bType="Timestamp"/>
      <DA name="Oper" fc="CO" bType="Struct" type="Oper_DPC"/>
      <DA name="ctlModel" fc="CF" bType="Enum" type="ctlModel"><Val>direct-with-normal-security</Val></DA>
    </DOType>
    <DAType id="Originator"><BDA name="orCat" bType="Enum" type="orCategory"/><BDA name="orIdent" bType="Octet64"/></DAType>
    <DAType id="Oper_DPC"><BDA name="ctlVal" bType="BOOLEAN"/><BDA name="origin" bType="Struct" type="Originator"/>
      <BDA name="ctlNum" bType="INT8U"/><BDA name="T" bType="Timestamp"/><BDA name="Test" bType="BOOLEAN"/>
      <BDA name="Check" bType="Check"/></DAType>
    <DAType id="Cancel_DPC"><BDA name="ctlVal" bType="BOOLEAN"/><BDA name="origin" bType="Struct" type="Originator"/>
      <BDA name="ctlNum" bType="INT8U"/><BDA name="T" bType="Timestamp"/><BDA name="Test" bType="BOOLEAN"/></DAType>
    <EnumType id="orCategory"><EnumVal ord="0">not-supported</EnumVal><EnumVal ord="1">bay-control</EnumVal><EnumVal ord="2">station-control</EnumVal><EnumVal ord="3">remote-control</EnumVal></EnumType>
"""
LNS = ('<LN lnClass="CSWI" inst="1" lnType="CSWI_SBO" desc="Comando 52-H06"/>'
       '<LN lnClass="CSWI" inst="2" lnType="CSWI_DIR" desc="Comando direto"/>')

ORIG = MmsType("struct", comps=[("orCat", MmsType("int", 8)), ("orIdent", MmsType("octet", -64))])
OPER = MmsType("struct", comps=[("ctlVal", MmsType("bool")), ("origin", ORIG), ("ctlNum", MmsType("uint", 8)),
                                ("T", MmsType("utc")), ("Test", MmsType("bool")), ("Check", MmsType("bits", 2))])
CANCEL = MmsType("struct", comps=OPER.comps[:5])


def cid_comandos(tmp) -> object:
    s = CID_I15.read_text(encoding="utf-8")
    s = s.replace("</DataTypeTemplates>", TIPOS + "</DataTypeTemplates>", 1)
    s = s.replace('<LN lnClass="XCBR" inst="1"', LNS + '<LN lnClass="XCBR" inst="1"', 1)
    p = tmp / "comandos.cid"
    p.write_text(s, encoding="utf-8")
    return p


def oper(val: bool, num: int = 1, interlock: bool = False, tipo=OPER):
    v = [val, [3, b"SAGE-COS"], num, UtcTime.from_float(time.time()), False]
    if tipo is OPER:
        v.append(Bits(2, 1 if interlock else 0))
    return enc_data(tipo, v)


async def reports(c, quiet=0.4) -> list:
    out = []
    while True:
        try:
            out.append(await asyncio.wait_for(c.reports.get(), quiet))
        except asyncio.TimeoutError:
            return out


def test_comandos_mms_e_registro(sim, tmp_path):
    ied = sim.create(cid_comandos(tmp_path))
    iid, port = ied["id"], ied["porta"]
    ctl = {c["ref"].split("/")[-1]: c for c in sim.h.get(f"/ieds/{iid}/controles").json()["controles"]}
    assert ctl["CSWI1.Pos"]["ctlModel"] == "sbo-with-enhanced-security"
    assert ctl["CSWI1.Pos"]["servicos"] == ["SBOw", "Oper", "Cancel"]
    assert ctl["CSWI2.Pos"]["ctlModel"] == "direct-with-normal-security"

    async def go():
        c = await connect(port)
        try:
            # direto: fecha, eco no estado
            assert (await c.write([on(CTRL, "CSWI2$CO$Pos$Oper")], [oper(True)]))[0] is None
            await asyncio.sleep(0.2)
            assert sim.point(iid, "CSWI2$ST$Pos")["valor"] == 2
            # de novo: já está fechado -> recusa com LastApplError (Position-reached)
            assert (await c.write([on(CTRL, "CSWI2$CO$Pos$Oper")], [oper(True, 2)]))[0] == 3
            rs = [r for r in await reports(c) if r.get("vars") == ["LastApplError"]]
            assert rs and rs[0]["itens"][0][1][4][1] == 5  # AddCause = Position-reached
            # SBO reforçado: Oper sem seleção é recusado
            assert (await c.write([on(CTRL, "CSWI1$CO$Pos$Oper")], [oper(True)]))[0] == 3
            await reports(c, 0.2)
            assert (await c.write([on(CTRL, "CSWI1$CO$Pos$SBOw")], [oper(True, 7)]))[0] is None
            assert sim.h.get(f"/ieds/{iid}/controles").json()["controles"][0]["selecionado_por"]
            assert (await c.write([on(CTRL, "CSWI1$CO$Pos$Oper")], [oper(True, 7)]))[0] is None
            rs = await reports(c, 0.6)
            term = [r for r in rs if r.get("vars") == [f"{CTRL}/CSWI1$CO$Pos$Oper"]]
            assert term, rs  # CommandTermination positiva
            assert sim.point(iid, "CSWI1$ST$Pos")["valor"] == 2
            # seleção e cancelamento
            assert (await c.write([on(CTRL, "CSWI1$CO$Pos$SBOw")], [oper(False, 8)]))[0] is None
            assert (await c.write([on(CTRL, "CSWI1$CO$Pos$Cancel")], [oper(False, 8, tipo=CANCEL)]))[0] is None
            assert (await c.write([on(CTRL, "CSWI1$CO$Pos$Oper")], [oper(False, 8)]))[0] == 3
        finally:
            await c.close()
    run(go())
    cmds = sim.h.get(f"/ieds/{iid}/comandos").json()["comandos"]
    oks = [x for x in cmds if x["servico"] == "Operate" and x["resultado"] == "aceito"]
    assert len(oks) == 2
    d = next(x for x in oks if "CSWI2" in x["ref"])
    assert d["valor_txt"].startswith("CLOSE") and d["origem"]["orCat_txt"] == "remote-control"
    assert d["origem"]["orIdent"] == "SAGE-COS" and d["mms"] == f"{CTRL}/CSWI2$CO$Pos$Oper"
    assert any(x.get("terminacao") == "positiva" for x in cmds)
    assert any(x.get("causa") == "Position-reached" for x in cmds)
    assert any(x.get("causa") == "Object-not-selected" for x in cmds)
    assert any(x["servico"] == "Cancel" and x["resultado"] == "aceito" for x in cmds)


def test_logica_disjuntor_com_intertravamento(sim, tmp_path):
    ied = sim.create(cid_comandos(tmp_path))
    iid, port = ied["id"], ied["porta"]
    cswi, xcbr, mola = f"{CTRL}/CSWI2$Pos", f"{CTRL}/XCBR1$ST$Pos", f"{CTRL}/sAutGGIO2$ST$Ind3"
    lg = {"nome": "Disjuntor 52-H06", "blocos": [
        {"id": "c", "tipo": "comando", "p": {"controle": cswi}},
        {"id": "x", "tipo": "saida", "p": {"ponto": xcbr, "tempo_ms": 100}},
        {"id": "s", "tipo": "saida", "p": {"ponto": f"{CTRL}/CSWI2$ST$Pos", "tempo_ms": 100}},
        {"id": "m", "tipo": "entrada", "p": {"ponto": mola}},
        {"id": "b", "tipo": "bloqueio", "p": {"controle": cswi, "direcao": "fechar"}},
    ], "fios": [
        {"de": "c", "saida": "on", "para": "x", "entrada": "on"}, {"de": "c", "saida": "off", "para": "x", "entrada": "off"},
        {"de": "c", "saida": "on", "para": "s", "entrada": "on"}, {"de": "c", "saida": "off", "para": "s", "entrada": "off"},
        {"de": "m", "saida": "q", "para": "b", "entrada": "a"},
    ]}
    r = sim.h.post(f"/ieds/{iid}/logicas", json=lg)
    assert r.status_code == 200, r.text
    assert r.json()["avisos"] == []
    assert sim.h.get(f"/ieds/{iid}/controles").json()["controles"][1]["execucao"] == "lógica"

    async def go():
        c = await connect(port)
        try:
            sim.set(iid, "sAutGGIO2$ST$Ind3", valor=True)  # mola descarregada: bloqueia fechar
            await asyncio.sleep(0.15)
            assert (await c.write([on(CTRL, "CSWI2$CO$Pos$Oper")], [oper(True)]))[0] == 3
            rs = [r for r in await reports(c) if r.get("vars") == ["LastApplError"]]
            assert rs and rs[0]["itens"][0][1][4][1] == 10  # Blocked-by-interlocking
            sim.set(iid, "sAutGGIO2$ST$Ind3", valor=False)
            await asyncio.sleep(0.15)
            assert (await c.write([on(CTRL, "CSWI2$CO$Pos$Oper")], [oper(True, 2)]))[0] is None
            await asyncio.sleep(0.05)
            assert sim.point(iid, "XCBR1$ST$Pos")["valor"] == 0  # em manobra
            await asyncio.sleep(0.3)
            assert sim.point(iid, "XCBR1$ST$Pos")["valor"] == 2
            assert sim.point(iid, "CSWI2$ST$Pos")["valor"] == 2
        finally:
            await c.close()
    run(go())
    est = sim.h.get(f"/ieds/{iid}/logicas/estado").json()
    assert est["varreduras"] > 0 and not est["logicas"]["L1"]["erros"]
    cmds = sim.h.get(f"/ieds/{iid}/comandos").json()["comandos"]
    assert any(x.get("causa") == "Blocked-by-interlocking" and "Disjuntor 52-H06" in x["causa_txt"] for x in cmds)
    # comando pela tela passa pelas mesmas verificações
    r = sim.h.post(f"/ieds/{iid}/controles/comando", json={"chave": cswi, "valor": True}).json()
    assert r["resultado"] == "recusado" and r["causa"] == "Position-reached"


def test_reimportar_cid_preserva_logicas(sim, tmp_path):
    ied = sim.create(cid_comandos(tmp_path), start=False)
    iid = ied["id"]
    cswi, xcbr = f"{CTRL}/CSWI2$Pos", f"{CTRL}/XCBR1$ST$Pos"
    lg = {"nome": "Eco 52", "blocos": [
        {"id": "c", "tipo": "comando", "p": {"controle": cswi}},
        {"id": "x", "tipo": "saida", "p": {"ponto": xcbr}},
        {"id": "s", "tipo": "saida", "p": {"ponto": f"{CTRL}/CSWI2$ST$Pos"}},
    ], "fios": [
        {"de": "c", "saida": "on", "para": "x", "entrada": "on"}, {"de": "c", "saida": "off", "para": "x", "entrada": "off"},
        {"de": "c", "saida": "on", "para": "s", "entrada": "on"}, {"de": "c", "saida": "off", "para": "s", "entrada": "off"},
    ]}
    assert sim.h.post(f"/ieds/{iid}/logicas", json=lg).status_code == 200
    assert sim.h.patch(f"/ieds/{iid}/controles/config", json={cswi: {"tempo_ms": 50}}).status_code == 200

    # mesmo relé, CID novo com os mesmos endereços: nada a remapear e a lógica continua lá
    up = sim.upload(cid_comandos(tmp_path))
    prev = sim.h.post(f"/ieds/{iid}/reimport", json={"token": up["token"], "simular": True}).json()
    assert prev["referencias"] == [] and prev["logicas"] == 1
    r = sim.h.post(f"/ieds/{iid}/reimport", json={"token": up["token"]}).json()
    assert r["orfas"] == [] and len(sim.h.get(f"/ieds/{iid}/logicas").json()["logicas"]) == 1

    # CID novo em que o CSWI2 virou CSWI3: a prévia aponta o que quebra e sugere o equivalente
    p = tmp_path / "v2.cid"
    p.write_text(cid_comandos(tmp_path).read_text(encoding="utf-8").replace(
        'lnClass="CSWI" inst="2"', 'lnClass="CSWI" inst="3"'), encoding="utf-8")
    up = sim.upload(p)
    prev = sim.h.post(f"/ieds/{iid}/reimport", json={"token": up["token"], "simular": True}).json()
    sug = {x["ref"]: x["sugestao"] for x in prev["referencias"]}
    assert sug == {cswi: f"{CTRL}/CSWI3$Pos", f"{CTRL}/CSWI2$ST$Pos": f"{CTRL}/CSWI3$ST$Pos"}
    assert len(sim.h.get(f"/ieds/{iid}/logicas").json()["logicas"][0]["blocos"]) == 3  # prévia não mexe
    r = sim.h.post(f"/ieds/{iid}/reimport", json={"token": up["token"], "mapa": sug}).json()
    assert r["orfas"] == [] and r["remapeados"] == 2
    blocos = {b["id"]: b["p"] for b in sim.h.get(f"/ieds/{iid}/logicas").json()["logicas"][0]["blocos"]}
    assert blocos["c"]["controle"] == f"{CTRL}/CSWI3$Pos" and blocos["s"]["ponto"] == f"{CTRL}/CSWI3$ST$Pos"
    ctl = {c["chave"]: c for c in sim.h.get(f"/ieds/{iid}/controles").json()["controles"]}
    assert ctl[f"{CTRL}/CSWI3$Pos"]["config"]["tempo_ms"] == 50 and ctl[f"{CTRL}/CSWI3$Pos"]["execucao"] == "lógica"


def test_dica_kconv_do_sage(sim, tmp_path):
    """SBOw num objeto direto (como SAGE com KCONV 'SBOw TERM' e CID com ctlModel 3) explica o KCONV certo."""
    ied = sim.create(cid_comandos(tmp_path))
    iid, port = ied["id"], ied["porta"]
    ctl = {c["ref"].split("/")[-1]: c for c in sim.h.get(f"/ieds/{iid}/controles").json()["controles"]}
    c1, c2 = ctl["CSWI1.Pos"], ctl["CSWI2.Pos"]
    assert c1["sage"]["kconv"] == "SBOw TERM" and c1["sage"]["cgf"] == "CSWI1$CO$Pos"
    assert c1["operTimeout_ms"] == 1000 and c1["operTimeout_origem"] == "CID"
    assert c1["sboTimeout_ms"] == 2000 and c1["ctlModel_origem"] == "CID"
    assert c2["sage"]["kconv"] == "" and c2["operTimeout_origem"].startswith("simulador")

    async def go():
        c = await connect(port)
        try:
            assert (await c.write([on(CTRL, "CSWI2$CO$Pos$SBOw")], [oper(True)]))[0] is not None
        finally:
            await c.close()
    run(go())
    cmds = sim.h.get(f"/ieds/{iid}/comandos").json()["comandos"]
    r = next(x for x in cmds if "CSWI2" in x["ref"] and x["resultado"] == "recusado")
    assert "KCONV vazio" in r["dica_sage"] and "direct-with-normal-security" in r["dica_sage"]


def test_bloco_comando_so_trip_e_filtro_por_objeto(sim, tmp_path):
    """Comando que só manda Trip: o bloco tem só a saída off; o registro filtra pelos objetos da lógica."""
    ied = sim.create(cid_comandos(tmp_path))
    iid, port = ied["id"], ied["porta"]
    cswi = f"{CTRL}/CSWI2$Pos"
    lg = {"nome": "Só trip", "blocos": [
        {"id": "c", "tipo": "comando", "p": {"controle": cswi, "sinais": "off"}},
        {"id": "s", "tipo": "saida", "p": {"ponto": f"{CTRL}/XCBR1$ST$Pos"}},
    ], "fios": [
        {"de": "c", "saida": "on", "para": "s", "entrada": "on"},  # porta que não existe mais: cai
        {"de": "c", "saida": "off", "para": "s", "entrada": "off"},
    ]}
    r = sim.h.post(f"/ieds/{iid}/logicas", json=lg)
    assert r.status_code == 200, r.text
    assert [f["saida"] for f in r.json()["logica"]["fios"]] == ["off"]
    # CSWI2 começa aberto: sem isto o Trip seria recusado como Position-reached
    sim.h.patch(f"/ieds/{iid}/controles/config", json={cswi: {"rejeitar_mesma_posicao": False}})

    async def go():
        c = await connect(port)
        try:
            await asyncio.sleep(0.2)
            assert (await c.write([on(CTRL, "CSWI2$CO$Pos$Oper")], [oper(False)]))[0] is None
            assert (await c.write([on(CTRL, "CSWI1$CO$Pos$Oper")], [oper(True)]))[0] is not None
            await asyncio.sleep(0.3)
        finally:
            await c.close()
    run(go())
    cmds = sim.h.get(f"/ieds/{iid}/comandos", params={"ref": f"{CTRL}/CSWI2.Pos"}).json()["comandos"]
    assert cmds and all(x["ref"] == f"{CTRL}/CSWI2.Pos" for x in cmds)
    assert cmds[0]["valor_txt"].startswith("TRIP") and cmds[0]["execucao"] == "lógica"


def test_logica_sem_saida_no_proprio_objeto_mantem_eco_do_estado(sim, tmp_path):
    """Lógica que só escreve o XCBR: o CSWI acompanha o comando; sem isso o Close seguinte vinha Position-reached."""
    ied = sim.create(cid_comandos(tmp_path))
    iid, port = ied["id"], ied["porta"]
    cswi = f"{CTRL}/CSWI2$Pos"
    lg = {"nome": "Só XCBR", "blocos": [
        {"id": "c", "tipo": "comando", "p": {"controle": cswi}},
        {"id": "x", "tipo": "saida", "p": {"ponto": f"{CTRL}/XCBR1$ST$Pos"}},
    ], "fios": [
        {"de": "c", "saida": "on", "para": "x", "entrada": "on"}, {"de": "c", "saida": "off", "para": "x", "entrada": "off"},
    ]}
    assert sim.h.post(f"/ieds/{iid}/logicas", json=lg).status_code == 200

    async def go():
        c = await connect(port)
        try:
            await asyncio.sleep(0.2)
            for n, (val, pos) in enumerate([(True, 2), (False, 1), (True, 2)], 1):
                assert (await c.write([on(CTRL, "CSWI2$CO$Pos$Oper")], [oper(val, n)]))[0] is None, val
                await asyncio.sleep(0.2)
                assert sim.point(iid, "CSWI2$ST$Pos")["valor"] == pos
                assert sim.point(iid, "XCBR1$ST$Pos")["valor"] == pos
        finally:
            await c.close()
    run(go())
    cmds = sim.h.get(f"/ieds/{iid}/comandos", params={"ref": f"{CTRL}/CSWI2.Pos"}).json()["comandos"]
    assert len(cmds) == 3 and all(x["resultado"] == "aceito" and x["execucao"] == "lógica" for x in cmds)


def test_logica_confere_posicao_no_ponto_comandado(sim, tmp_path):
    """CSWI fechado e XCBR aberto (estado que ficou de antes): o Close vai, porque a posição é a do XCBR."""
    ied = sim.create(cid_comandos(tmp_path))
    iid, port = ied["id"], ied["porta"]
    cswi = f"{CTRL}/CSWI2$Pos"
    lg = {"nome": "Só XCBR", "blocos": [
        {"id": "c", "tipo": "comando", "p": {"controle": cswi}},
        {"id": "x", "tipo": "saida", "p": {"ponto": f"{CTRL}/XCBR1$ST$Pos"}},
    ], "fios": [
        {"de": "c", "saida": "on", "para": "x", "entrada": "on"}, {"de": "c", "saida": "off", "para": "x", "entrada": "off"},
    ]}
    assert sim.h.post(f"/ieds/{iid}/logicas", json=lg).status_code == 200
    sim.set(iid, "CSWI2$ST$Pos", valor=2)
    sim.set(iid, "XCBR1$ST$Pos", valor=1)

    async def go():
        c = await connect(port)
        try:
            await asyncio.sleep(0.2)
            assert (await c.write([on(CTRL, "CSWI2$CO$Pos$Oper")], [oper(True, 1)]))[0] is None
            await asyncio.sleep(0.2)
            assert sim.point(iid, "XCBR1$ST$Pos")["valor"] == 2
            assert (await c.write([on(CTRL, "CSWI2$CO$Pos$Oper")], [oper(True, 2)]))[0] is not None  # já fechado
            assert (await c.write([on(CTRL, "CSWI2$CO$Pos$Oper")], [oper(False, 3)]))[0] is None
            await asyncio.sleep(0.2)
            assert sim.point(iid, "XCBR1$ST$Pos")["valor"] == 1
        finally:
            await c.close()
    run(go())
    cmds = sim.h.get(f"/ieds/{iid}/comandos", params={"ref": f"{CTRL}/CSWI2.Pos"}).json()["comandos"]
    recusa = next(x for x in cmds if x["resultado"] == "recusado")
    assert recusa["causa"] == "Position-reached" and "XCBR1" in recusa["causa_txt"]


def test_logica_terminacao_espera_ponto_comandado(sim, tmp_path):
    """Segurança reforçada com lógica: a CommandTermination sai quando o XCBR (com manobra) chega à posição."""
    ied = sim.create(cid_comandos(tmp_path))
    iid, port = ied["id"], ied["porta"]
    lg = {"nome": "52 reforçado", "blocos": [
        {"id": "c", "tipo": "comando", "p": {"controle": f"{CTRL}/CSWI1$Pos"}},
        {"id": "x", "tipo": "saida", "p": {"ponto": f"{CTRL}/XCBR1$ST$Pos", "tempo_ms": 300}},
    ], "fios": [
        {"de": "c", "saida": "on", "para": "x", "entrada": "on"}, {"de": "c", "saida": "off", "para": "x", "entrada": "off"},
    ]}
    assert sim.h.post(f"/ieds/{iid}/logicas", json=lg).status_code == 200
    sim.set(iid, "XCBR1$ST$Pos", valor=1)

    async def go():
        c = await connect(port)
        try:
            await asyncio.sleep(0.2)
            assert (await c.write([on(CTRL, "CSWI1$CO$Pos$SBOw")], [oper(True, 1)]))[0] is None
            assert (await c.write([on(CTRL, "CSWI1$CO$Pos$Oper")], [oper(True, 1)]))[0] is None
            rs = await reports(c, 0.8)
            assert [r for r in rs if r.get("vars") == [f"{CTRL}/CSWI1$CO$Pos$Oper"]], rs  # positiva
            assert sim.point(iid, "XCBR1$ST$Pos")["valor"] == 2
        finally:
            await c.close()
    run(go())
    cmds = sim.h.get(f"/ieds/{iid}/comandos", params={"ref": f"{CTRL}/CSWI1.Pos"}).json()["comandos"]
    assert any(x.get("terminacao") == "positiva" for x in cmds)
