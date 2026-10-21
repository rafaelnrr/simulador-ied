"""Testes de unidade: SCL, catálogo, perfis, codificação de tempo e qualidade (seções 4, 8 e 9)."""
from __future__ import annotations

from conftest import CID_I15, CID_REF, I15

from simied.clock import IedClock, q_from_dict, q_to_dict
from simied.model import Builder, build_points
from simied.mms.types import Bits, UtcTime
from simied.profiles import ProfileStore, resolve
from simied.scl import parse_scl


def _catalog(cid):
    scl = parse_scl(cid.read_bytes())
    ied = scl.ieds[0]
    b = Builder(scl, ied)
    return scl, ied, build_points(b.build(), b)


def test_edicao_detectada_do_xmlns():  # RN-CID-01
    assert parse_scl(CID_I15.read_bytes()).edition == 1
    assert parse_scl(CID_REF.read_bytes()).edition == 2


def test_catalogo_i15():  # RN-CID-03, RN-CID-04, RN-CID-05
    _, ied, pts = _catalog(CID_I15)
    assert ied.name == I15
    by = {p.path: p for p in pts}
    mola = by[f"{I15}CTRL/sAutGGIO2$ST$Ind3"]
    assert (mola.cdc, mola.kind, mola.desc) == ("SPS", "dig", "MOLA")
    assert by[f"{I15}MED/MMXU1$MX$Hz"].cdc == "MV"
    assert by[f"{I15}MED/MMXU1$MX$A$phsA"].cdc == "CMV"
    assert by[f"{I15}MED/MMTR1$ST$TotWh"].cdc == "BCR"
    assert {p.cdc for p in pts} >= {"SPS", "INS", "MV", "CMV", "BCR", "ACT", "ACD"}


def test_reimportar_mesmo_cid_nao_muda_catalogo():  # critério de aceite
    a = [p.path for p in _catalog(CID_I15)[2]]
    b = [p.path for p in _catalog(CID_I15)[2]]
    assert a == b


def test_perfil_i15(tmp_path):  # seção 9, RN-CID-10
    store = ProfileStore(tmp_path)
    scl = parse_scl(CID_I15.read_bytes())
    p = resolve(store.get("ingeteam-ef-i15"), None, scl.ieds[0].services, scl.edition)
    r = p["rcb"]
    assert r["posicao"] == "LLN0"
    assert r["atributos"]["DatSet"] == "Conf" and r["atributos"]["RptID"] == "Dyn"
    assert (r["max_datasets"], r["max_rcbs"]) == (16, 32)
    assert p["servicos"]["defineNamedVariableList"] is False
    assert p["edicao"] == 1


def test_perfis_fornecidos(tmp_path):  # RN-PRF-02
    ids = set(ProfileStore(tmp_path).all())
    assert {"generico-ed1", "generico-ed2", "ingeteam-ef-i15", "referencia-UCDQSA1"} <= ids


def test_copia_de_perfil(tmp_path):  # RN-PRF-03
    store = ProfileStore(tmp_path)
    p = store.copy("ingeteam-ef-i15", "meu-i15")
    assert p["copiado_de"] == "ingeteam-ef-i15"
    assert store.get("meu-i15")["fornecido"] is False


def test_utc_time_no_fio():  # RN-TMP-01
    t = UtcTime.from_float(1_000_000_000.5, 0x8B)
    b = t.encode_value()
    assert len(b) == 8
    assert b[:4] == (1_000_000_000).to_bytes(4, "big")
    assert int.from_bytes(b[4:7], "big") == 1 << 23  # 0,5 s = 2^23 / 2^24
    assert b[7] == 0x8B


def test_relogio_deslocamento_e_falha():  # RN-TMP-02, RN-TMP-04, RN-TMP-05
    c = IedClock({"modo": "deslocamento", "horas": 3, "negativo": True})
    assert c.offset_s == -10800
    assert c.tq_byte() == 0x8B
    c.update({"falha": True})
    assert c.tq_byte() & 0x60 == 0x60
    c = IedClock({"modo": "congelado", "congelado_em": "2026-10-09T12:00:00Z"})
    assert abs(c.now() - 1791547200) < 1


def test_qualidade_boa_no_fio():  # seção 8: 84 03 03 00 00
    q = Bits(13, 0)
    assert q.encode_value() == bytes([0x03, 0x00, 0x00])
    q = q_from_dict({"validity": "invalid"}, q)
    assert q_to_dict(q)["validity"] == "invalid"
    q = q_from_dict({"validity": "questionable", "detailQual": ["oldData"], "source": "substituted"}, q)
    d = q_to_dict(q)
    assert (d["validity"], d["detailQual"], d["source"]) == ("questionable", ["oldData"], "substituted")


def test_mascara_de_inclusao():  # seção 7: bit mais significativo = índice 0
    b = Bits.from_list(10, [0, 9])
    assert b.encode_value() == bytes([6, 0b10000000, 0b01000000])
