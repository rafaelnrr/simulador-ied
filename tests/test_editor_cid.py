"""Editor de CID: visões, operações, desfazer, validação, aplicação no IED, arquivo avulso e persistência."""
from __future__ import annotations

from conftest import CID_I15, CID_REF, I15, IND3, free_port, start_server, stop_server

from simied.cidedit import Doc
from simied.scl import parse_scl

LN0 = f"m/{I15}/CTRL/LLN0"
GGIO = f"m/{I15}/CTRL/sAutGGIO2"


def ed(sim, path: str, method: str = "GET", **kw):
    return sim.h.request(method, "/cid-editor" + path, **kw)


def op(sim, sid: str, **body):
    r = ed(sim, f"/{sid}/ops", "POST", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def node(sim, sid: str, no: str) -> dict:
    r = ed(sim, f"/{sid}/node", params={"no": no})
    assert r.status_code == 200, r.text
    return r.json()


def kids(sim, sid: str, no: str = "", vista: str = "modelo") -> list:
    r = ed(sim, f"/{sid}/children", params={"no": no, "vista": vista})
    assert r.status_code == 200, r.text
    return r.json()["filhos"]


def test_regravacao_preserva_o_modelo():
    for f in (CID_I15, CID_REF):
        raw = f.read_bytes()
        out = Doc(raw).serialize()
        a, b = parse_scl(raw), parse_scl(out)
        assert [(i.name, [(ld.inst, [(ln.name, len(ln.datasets), len(ln.rcbs), ln.inits) for ln in ld.lns])
                          for ld in i.lds]) for i in a.ieds] == \
               [(i.name, [(ld.inst, [(ln.name, len(ln.datasets), len(ln.rcbs), ln.inits) for ln in ld.lns])
                          for ld in i.lds]) for i in b.ieds]
        assert b" />" not in out
        assert Doc(out).serialize() == out  # estável


def test_visoes_e_detalhes(sim):
    iid = sim.create(start=False)["id"]
    sid = f"ied-{iid}"
    s = ed(sim, f"/{sid}").json()
    assert s["alterado"] is False and s["origem"] == "ied" and s["ied"]["id"] == iid
    assert s["contagem"]["LDevice"] == 2 and s["contagem"]["DataSet"] >= 1
    assert [x["rotulo"] for x in kids(sim, sid)] == [I15]
    assert [x["rotulo"] for x in kids(sim, sid, f"m/{I15}")] == ["CTRL", "MED"]
    ln0 = kids(sim, sid, LN0)
    nomes = [x["rotulo"] for x in ln0]
    assert "Mod" in nomes and any(x["tipo"] == "ds" for x in ln0) and any(x["tipo"] == "rcb" for x in ln0)
    mod = {x["rotulo"]: x for x in kids(sim, sid, LN0 + "/Mod")}
    assert {"t": "CF", "c": "info"} in mod["ctlModel"]["marcas"]
    d = node(sim, sid, LN0 + "/Mod/ctlModel")
    assert (d["fc"], d["bType"], d["valor"], d["valor_origem"]) == ("CF", "Enum", "status-only", "tipo")
    assert "sbo-with-enhanced-security" in d["opcoes"]
    assert d["mms"] == f"{I15}CTRL/LLN0$CF$Mod$ctlModel"
    ind3 = node(sim, sid, GGIO + "/Ind3")
    assert ind3["cdc"] == "SPS" and ind3["desc"] == "MOLA"
    assert any(x["dataset_nome"].endswith("DS_ESTADOS_SAGE") for x in ind3["em_datasets"])
    # tipos: grupos, usos e tipo sem uso não aparece como erro
    grupos = {x["rotulo"]: x for x in kids(sim, sid, vista="tipos")}
    assert set(grupos) == {"LNodeType", "DOType", "DAType", "EnumType"}
    dot = kids(sim, sid, "t/DOType", "tipos")[0]
    dd = node(sim, sid, dot["id"])
    assert dd["tag"] == "DOType" and dd["usado_total"] >= 1
    # xml: raiz e localizar um DAI no modelo
    assert kids(sim, sid, vista="xml")[0]["tag"] == "SCL"
    busca = ed(sim, f"/{sid}/search", params={"q": "ctlModel", "vista": "modelo"}).json()
    assert busca["total"] >= 1 and all("ctlModel" in r["rotulo"] for r in busca["resultados"])
    loc = ed(sim, f"/{sid}/locate", params={"no": dot["id"], "vista": "auto"}).json()
    assert loc["vista"] == "tipos" and loc["caminho"][0] == "t/DOType"
    v = ed(sim, f"/{sid}/validate").json()
    assert v["erros"] == [] and v["carga"]["ieds"][0]["nome"] == I15


def test_editar_desfazer_e_aplicar_no_ied_rodando(sim):
    ied = sim.create()
    iid = ied["id"]
    sid = f"ied-{iid}"
    sim.set(iid, IND3, valor=True)
    r = op(sim, sid, op="valor", no=LN0 + "/Mod/ctlModel", valor="direct-with-normal-security")
    assert r["alterado"] and r["sessao"]["alterado"] and r["sessao"]["desfazer"] == 1
    assert node(sim, sid, LN0 + "/Mod/ctlModel")["valor_origem"] == "instância"
    op(sim, sid, op="descricao", no=GGIO + "/Ind3", desc="MOLA DESCARREGADA")
    bad = ed(sim, f"/{sid}/ops", "POST", json={"op": "valor", "no": LN0 + "/Mod/ctlModel", "valor": "xpto"})
    assert bad.status_code == 422 and "EnumType" in bad.json()["erro"]
    diff = ed(sim, f"/{sid}/diff").json()
    assert diff["adicionadas"] >= 2 and any("MOLA DESCARREGADA" in x for x in diff["linhas"])
    # desfazer e refazer
    u = ed(sim, f"/{sid}/undo", "POST").json()
    assert node(sim, sid, GGIO + "/Ind3")["desc"] == "MOLA" and u["sessao"]["refazer"] == 1
    ed(sim, f"/{sid}/redo", "POST")
    assert node(sim, sid, GGIO + "/Ind3")["desc"] == "MOLA DESCARREGADA"
    # FCDA nova no DataSet SAGE pela visão modelo
    ds = next(x for x in kids(sim, sid, LN0) if x["rotulo"] == "DS_ESTADOS_SAGE")
    op(sim, sid, op="fcda", no=GGIO + "/Ind5", dataset=ds["id"], fc="ST")
    dup = ed(sim, f"/{sid}/ops", "POST", json={"op": "fcda", "no": GGIO + "/Ind5", "dataset": ds["id"], "fc": "ST"})
    assert dup.status_code == 409
    assert node(sim, sid, ds["id"])["fcdas"][-1]["texto"] == "CTRL/sAutGGIO2.Ind5 [ST]"
    # aplicar com o IED rodando pede confirmação
    r = ed(sim, f"/{sid}/apply", "POST", json={})
    assert r.status_code == 409 and r.json()["codigo"] == "confirmar"
    r = ed(sim, f"/{sid}/apply", "POST", json={"confirmar": True})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["removidos"] == [] and res["sessao"]["alterado"] is False
    assert sim.h.get(f"/ieds/{iid}").json()["estado"] == "rodando"
    assert sim.point(iid, IND3)["valor"] is True  # estado mantido na reimportação
    sage = next(d for d in sim.h.get(f"/ieds/{iid}/datasets").json()["datasets"] if d["nome"] == "DS_ESTADOS_SAGE")
    assert sage["membros"][-1]["ref"].endswith("sAutGGIO2$ST$Ind5")
    cid = sim.h.get(f"/ieds/{iid}/export-cid").text
    assert "MOLA DESCARREGADA" in cid and "direct-with-normal-security</Val>" in cid
    assert all(d["sessao"] != sid for d in ed(sim, "").json()["rascunhos"])  # aplicado: sem rascunho pendente


def test_operacoes_xml(sim):
    iid = sim.create(start=False)["id"]
    sid = f"ied-{iid}"
    ln0x = node(sim, sid, LN0)["xml_no"]
    r = op(sim, sid, op="adicionar", pai=ln0x, tag="DataSet", atributos={"name": "DS_NOVO", "desc": "teste"})
    ds = r["no"]
    d = node(sim, sid, ds)
    assert d["tag"] == "DataSet" and d["fcdas"] == []
    # o DataSet novo fica junto dos outros (ordem do esquema), antes dos ReportControl
    tags = [x["tag"] for x in kids(sim, sid, ln0x, "xml")]
    assert tags.index("ReportControl") > max(i for i, t in enumerate(tags) if t == "DataSet")
    op(sim, sid, op="adicionar", pai=ds, xml='<FCDA ldInst="CTRL" prefix="" lnClass="XCBR" lnInst="1" doName="Pos" fc="ST"/>')
    op(sim, sid, op="adicionar", pai=ds, xml='<FCDA ldInst="CTRL" prefix="" lnClass="XCBR" lnInst="1" doName="Nada" fc="ST"/>')
    f = node(sim, sid, ds)["fcdas"]
    assert [x["ok"] for x in f] == [True, False]
    v = ed(sim, f"/{sid}/validate").json()
    assert any("Nada" in e["msg"] and e["no"] == f[1]["no"] for e in v["erros"])
    op(sim, sid, op="mover", no=f[1]["no"], direcao=-1)
    assert [x["ok"] for x in node(sim, sid, ds)["fcdas"]] == [False, True]
    op(sim, sid, op="excluir", no=node(sim, sid, ds)["fcdas"][0]["no"])
    assert [x["ok"] for x in node(sim, sid, ds)["fcdas"]] == [True]
    op(sim, sid, op="atributos", no=ds, atributos={"desc": None, "name": "DS_RENOMEADO"})
    d = node(sim, sid, ds)
    assert d["atributos"] == [["name", "DS_RENOMEADO"]]
    # elemento fora do esquema pede confirmação; XML inválido e tipo em uso
    r = ed(sim, f"/{sid}/ops", "POST", json={"op": "adicionar", "pai": ds, "tag": "DOI", "atributos": {"name": "x"}})
    assert r.status_code == 409 and r.json()["codigo"] == "confirmar"
    r = ed(sim, f"/{sid}/ops", "POST", json={"op": "substituir", "no": ds, "xml": "<DataSet name='x'>"})
    assert r.status_code == 422 and "XML inválido" in r.json()["erro"]
    dot = kids(sim, sid, "t/DOType", "tipos")[0]["id"]
    r = ed(sim, f"/{sid}/ops", "POST", json={"op": "excluir", "no": dot})
    assert r.status_code == 409
    r = ed(sim, f"/{sid}/ops", "POST", json={"op": "xpto"})
    assert r.status_code == 422
    # descartar volta ao CID do IED
    assert ed(sim, f"/{sid}", "DELETE").status_code == 200
    assert ed(sim, f"/{sid}").json()["alterado"] is False


def test_arquivo_avulso_vira_ied(sim):
    r = sim.h.post("/cid-editor/files", files={"arquivo": (CID_REF.name, CID_REF.read_bytes())})
    assert r.status_code == 200, r.text
    s = r.json()
    sid = s["sessao"]
    assert s["origem"] == "arquivo" and sid.startswith("arq-")
    ied_scl = s["ieds_scl"][0]
    ied_el = node(sim, sid, f"m/{ied_scl}")["xml_no"]
    op(sim, sid, op="atributos", no=ied_el, atributos={"desc": "editado no editor"})
    assert ed(sim, f"/{sid}/apply", "POST", json={}).status_code == 422  # arquivo avulso não tem IED
    up = ed(sim, f"/{sid}/upload-token", "POST").json()
    body = {"token": up["token"], "perfil": "generico-ed2", "ip": "127.0.0.1", "porta": free_port(), "nome": "do_editor",
            "aceitar_erros": True}
    r = sim.h.post("/ieds", json=body)
    assert r.status_code == 200, r.text
    assert "editado no editor" in sim.h.get(f"/ieds/{r.json()['id']}/export-cid").text
    assert sim.h.get(f"/cid-editor/{sid}/download").content.startswith(b"<?xml")
    assert ed(sim, f"/{sid}", "DELETE").status_code == 200
    assert ed(sim, f"/{sid}").status_code == 404
    assert sim.h.post("/cid-editor/files", files={"arquivo": ("x.cid", b"<nada/>")}).status_code == 422


def test_rascunho_sobrevive_ao_reinicio(tmp_path):
    proc, s = start_server(tmp_path)
    try:
        iid = s.create(start=False)["id"]
        op(s, f"ied-{iid}", op="descricao", no=GGIO + "/Ind4", desc="SF6 BAIXO")
    finally:
        stop_server(proc)
    proc, s = start_server(tmp_path)
    try:
        sid = f"ied-{iid}"
        info = ed(s, f"/{sid}").json()
        assert info["alterado"] is True and info["historico"][-1]["acao"].endswith("SF6 BAIXO")
        assert node(s, sid, GGIO + "/Ind4")["desc"] == "SF6 BAIXO"
        assert "SF6 BAIXO" not in s.h.get(f"/ieds/{iid}/export-cid").text  # só muda o IED ao aplicar
    finally:
        stop_server(proc)


def test_visualizador_exportar_comparar_e_limpar(sim):
    iid = sim.create(start=False)["id"]
    sid = f"ied-{iid}"
    ov = ed(sim, f"/{sid}/overview").json()
    assert ov["ieds"][0]["nome"] == I15 and ov["comunicacao"]
    rcb = {c["nome"]: c for c in ov["controles"] if c["tipo"] == "ReportControl"}
    assert rcb and all(c["dataset"] for c in rcb.values())
    sage = next(d for d in ov["datasets"] if d["nome"] == "DS_ESTADOS_SAGE")
    assert sage["membros"][0]["mms"].endswith("sAutGGIO2$ST$Ind3") and sage["membros"][0]["desc"] == "MOLA"
    for tipo in ("datasets", "controles", "comunicacao", "pontos"):
        r = ed(sim, f"/{sid}/export", params={"tipo": tipo})
        assert r.status_code == 200 and r.text.startswith("﻿") and ";" in r.text.splitlines()[0], tipo
    assert "sAutGGIO2$ST$Ind3$stVal" in ed(sim, f"/{sid}/export", params={"tipo": "pontos"}).text
    assert ed(sim, f"/{sid}/export", params={"tipo": "x"}).status_code == 422
    # comparação: rascunho × original e IED × IED com outro nome
    op(sim, sid, op="descricao", no=GGIO + "/Ind3", desc="MOLA NOVA")
    cmp = ed(sim, f"/{sid}/compare", params={"com": "base"}).json()
    assert cmp["total"] == 1 and cmp["diferencas"][0]["categoria"] == "Instância (DOI)"
    assert "desc: MOLA → MOLA NOVA" in cmp["diferencas"][0]["detalhes"]
    outro = f"ied-{sim.create(start=False)['id']}"
    cmp = ed(sim, f"/{sid}/compare", params={"com": outro}).json()
    assert cmp["sem_nome"] is False and cmp["total"] == 1, cmp["resumo"]
    op(sim, outro, op="atributos", no=node(sim, outro, f"m/{I15}")["xml_no"], atributos={"name": "OUTRO_IED"})
    cmp = ed(sim, f"/{sid}/compare", params={"com": outro}).json()
    assert cmp["sem_nome"] is True and cmp["ieds"] == [["OUTRO_IED"], [I15]]
    assert any(d["chave"] == "<IED>CTRL/sAutGGIO2.Ind3" for d in cmp["diferencas"])
    assert not any(d["categoria"] == "LN" for d in cmp["diferencas"])
    # limpeza de tipos sem uso
    dtt = node(sim, sid, kids(sim, sid, "t/DOType", "tipos")[0]["id"])["caminho"][-1]["id"]
    op(sim, sid, op="adicionar", pai=dtt, tag="DOType", atributos={"id": "SOBRA_T", "cdc": "SPS"})
    r = op(sim, sid, op="limpar")
    assert "1 DOType" in r["mensagem"]
    assert not any(x["rotulo"] == "SOBRA_T" for x in kids(sim, sid, "t/DOType", "tipos"))


def test_sugestao_opmsk(sim):
    iid = sim.create(start=False)["id"]
    r = sim.h.get("/opmsk/sugestao", params={"ied": iid}).json()
    b = {x["bit"]: x for x in r["bits"]}
    assert b[5]["ligar"] is True and "LLN0" in b[5]["motivo"]
    assert b[4]["ligar"] is (not r["dynamic"])


def test_formulario_do_report_control(sim):
    iid = sim.create(start=False)["id"]
    sid = f"ied-{iid}"
    rcb = next(x for x in kids(sim, sid, LN0) if x["tipo"] == "rcb")
    no = rcb["id"]
    d = node(sim, sid, no)
    assert d["classe"] == "xml" and d["tag"] == "ReportControl"
    r = d["rcb"]
    assert set(r["trgops"]) == {"dchg", "qchg", "dupd", "period", "gi"}
    assert "seqNum" in r["optfields"] and r["datasets"] and r["membros"]["total"] > 0
    assert r["membros"]["dchg"] > 0  # os membros do DataSet declaram dchg no tipo

    res = op(sim, sid, op="rcb", no=no, atributos={"intgPd": "2000", "desc": "teste"},
             trgops={"period": True, "dupd": False}, optfields={"entryID": True},
             rptenabled={"max": "6"},
             clientlns=[{"iedName": "SAGE", "ldInst": "LD0", "lnClass": "ITCI", "lnInst": "1"}])
    assert res["alterado"] and "intgPd" in res["mensagem"]
    r = node(sim, sid, no)["rcb"]
    assert r["atributos"]["intgPd"] == "2000" and r["trgops"]["period"]["valor"]
    assert r["optfields"]["entryID"]["valor"] and r["rptenabled"]["max"] == "6"
    assert r["clientlns"][0]["iedName"] == "SAGE" and r["clientlns"][0]["lnClass"] == "ITCI"
    assert not any(a["campo"] == "intgPd" and a["nivel"] == "aviso" for a in r["avisos"])

    # análise: period ligado sem intgPd e nenhum gatilho
    op(sim, sid, op="rcb", no=no, atributos={"intgPd": "0"})
    assert any(a["campo"] == "intgPd" for a in node(sim, sid, no)["rcb"]["avisos"])
    op(sim, sid, op="rcb", no=no, trgops={k: False for k in ("dchg", "qchg", "dupd", "period", "gi")})
    assert any(a["campo"] == "trgops" and a["nivel"] == "erro" for a in node(sim, sid, no)["rcb"]["avisos"])

    # validações
    for body, st in (({"atributos": {"intgPd": "-1"}}, 422), ({"atributos": {"name": "1x"}}, 422),
                     ({"atributos": {"datSet": "NAO_EXISTE"}}, 409), ({"rptenabled": {"max": "100"}}, 422),
                     ({"rptenabled": {"max": "1"}, "clientlns": [{"iedName": "A", "lnClass": "ITCI"}] * 2}, 422)):
        rr = ed(sim, f"/{sid}/ops", "POST", json={"op": "rcb", "no": no, **body})
        assert rr.status_code == st, (body, rr.text)

    # o XML continua válido para o simulador
    raw = ed(sim, f"/{sid}/download").content
    rc = [r for i in parse_scl(raw).ieds for ld in i.lds for ln in ld.lns for r in ln.rcbs
          if r.name == node(sim, sid, no)["rcb"]["atributos"]["name"]][0]
    assert rc.max == 6 and rc.optflds["entryID"] and not rc.trgops["gi"]
    op(sim, sid, op="rcb", no=no, clientlns=[])
    assert node(sim, sid, no)["rcb"]["clientlns"] == []


def test_valor_mudado_no_cid_vence_o_estado_salvo(sim):
    iid = sim.create(start=False)["id"]
    sid = f"ied-{iid}"
    ind4 = IND3.replace("Ind3", "Ind4")
    sim.set(iid, IND3, valor=True)
    sim.set(iid, ind4, valor=True)
    sim.set(iid, ind4, valor=False)
    op(sim, sid, op="valor", no=GGIO + "/Ind4/stVal", valor="true")
    r = ed(sim, f"/{sid}/apply", "POST", json={"confirmar": True})
    assert r.status_code == 200, r.text
    assert sim.point(iid, ind4)["valor"] is True  # o CID mudou o valor: vale o CID
    assert sim.point(iid, IND3)["valor"] is True  # o CID não mexeu: estado mantido
    # depois de aplicado, um ajuste manual volta a ser mantido nas próximas cargas
    sim.set(iid, ind4, valor=False)
    rs = sim.h.post(f"/ieds/{iid}/start")
    assert rs.status_code == 200, rs.text
    assert sim.point(iid, ind4)["valor"] is False
