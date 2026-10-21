"""Perfis de IED (seção 9, RN-PRF-01 a RN-PRF-04).

Perfil = arquivo JSON versionado. Os fornecidos ficam em ./profiles (somente leitura);
os criados pelo usuário ficam em <dados>/profiles.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent / "profiles"

DEFAULT = {
    "id": "",
    "nome": "",
    "versao": 1,
    "descricao": "",
    "identificacao": {"fabricante": "SimIED", "modelo": "Simulador IEC 61850", "revisao": "1.0"},
    "edicao": "cid",
    "servicos": {
        "defineNamedVariableList": "cid",
        "deleteNamedVariableList": "cid",
        "getNamedVariableListAttributes": True,
        "identify": True,
        "status": True,
    },
    "rcb": {
        "posicao": "cid",
        "atributos": "cid",
        "indexacao": "cid",
        "trgops_permitidos": ["dchg", "qchg", "dupd", "period", "gi"],
        "max_datasets": "cid",
        "max_rcbs": "cid",
        "buffer_max": 1000,
        "erro_reservado": 2,
        "erro_conf": 3,
        "erro_habilitado": 2,
        "reasoncode_bits": "edicao",
        "segmentacao": True,
        "reserva_implicita": True,
    },
    "tempo": {"qualidade_t": "0x8b"},
    "qualidade": {"inicial": {"validity": "good"}},
    "limites": {"max_associacoes": 3, "max_pdu": 65000, "max_serv_outstanding": 10, "nesting": 10},
}

RCB_ATTRS = ("RptID", "DatSet", "OptFlds", "BufTm", "TrgOps", "IntgPd")
SETTING_KEY = {"RptID": "rptID", "DatSet": "datSet", "OptFlds": "optFields", "BufTm": "bufTime",
               "TrgOps": "trgOps", "IntgPd": "intgPd"}


def deep_merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class ProfileStore:
    def __init__(self, data_dir: Path):
        self.user_dir = data_dir / "profiles"
        self.user_dir.mkdir(parents=True, exist_ok=True)

    def _load_dir(self, d: Path, builtin: bool) -> dict:
        out = {}
        if not d.exists():
            return out
        for f in sorted(d.glob("*.json")):
            try:
                p = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            p.setdefault("id", f.stem)
            p["fornecido"] = builtin
            out[p["id"]] = p
        return out

    def all(self) -> dict:
        out = self._load_dir(BASE_DIR, True)
        out.update(self._load_dir(self.user_dir, False))
        return out

    def get(self, pid: str) -> dict | None:
        return self.all().get(pid)

    def save(self, prof: dict) -> dict:
        pid = prof.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", pid or ""):
            raise ValueError("id do perfil deve conter só letras, números, ponto, hífen e sublinhado")
        cur = self.all().get(pid)
        if cur and cur.get("fornecido"):
            raise ValueError("perfis fornecidos não podem ser alterados; crie uma cópia")
        pol = (prof.get("rcb") or {}).get("politicas")
        if pol is not None:
            from .rcbpolicy import validate_all
            validate_all(pol, "rcb.politicas")
        prof = {k: v for k, v in prof.items() if k != "fornecido"}
        prof["versao"] = int((cur or {}).get("versao", 0)) + 1
        (self.user_dir / f"{pid}.json").write_text(json.dumps(prof, indent=2, ensure_ascii=False),
                                                    encoding="utf-8")
        return prof

    def copy(self, src: str, new_id: str, nome: str | None = None) -> dict:
        p = self.get(src)
        if p is None:
            raise KeyError(src)
        p = copy.deepcopy(p)
        p["id"] = new_id
        p["nome"] = nome or f"Cópia de {p.get('nome', src)}"
        p["copiado_de"] = src
        p["versao"] = 0
        return self.save(p)

    def delete(self, pid: str):
        f = self.user_dir / f"{pid}.json"
        if not f.exists():
            raise KeyError(pid)
        f.unlink()


def resolve(profile: dict, overrides: dict | None, services: dict, edition: int) -> dict:
    """Perfil efetivo: padrão + perfil + sobrescritas do IED, com 'cid' resolvido pelo Services (RN-CID-10)."""
    p = deep_merge(DEFAULT, profile or {})
    p = deep_merge(p, overrides or {})
    if p.get("edicao") in ("cid", None):
        p["edicao"] = edition
    sv = p["servicos"]
    dyn = "DynDataSet" in services
    for k in ("defineNamedVariableList", "deleteNamedVariableList"):
        if sv.get(k) == "cid":
            sv[k] = dyn
    r = p["rcb"]
    rs = services.get("ReportSettings", {})
    if r.get("atributos") == "cid":
        if rs:
            r["atributos"] = {a: rs.get(SETTING_KEY[a], "Fix") for a in RCB_ATTRS}
        else:
            r["atributos"] = {a: "Dyn" for a in RCB_ATTRS}
    else:
        r["atributos"] = {a: r["atributos"].get(a, "Dyn") for a in RCB_ATTRS}
    if r.get("max_datasets") == "cid":
        r["max_datasets"] = int(services.get("ConfDataSet", {}).get("max", 0) or 0) or None
    if r.get("max_rcbs") == "cid":
        r["max_rcbs"] = int(services.get("ConfReportControl", {}).get("max", 0) or 0) or None
    if r.get("reasoncode_bits") == "edicao":
        r["reasoncode_bits"] = 7 if p["edicao"] >= 2 else 6
    if r.get("indexacao") == "cid":
        r["indexacao"] = "cid" if p["edicao"] >= 2 else "se_max_maior_1"
    p["max_dyn_datasets"] = int(services.get("DynDataSet", {}).get("max", 0) or 0) if dyn else 0
    return p


def services_list(p: dict) -> list[str]:
    s = ["status", "getNameList", "identify", "read", "write", "getVariableAccessAttributes",
         "informationReport", "conclude"]
    sv = p["servicos"]
    if sv.get("getNamedVariableListAttributes", True):
        s.append("getNamedVariableListAttributes")
    if sv.get("defineNamedVariableList"):
        s.append("defineNamedVariableList")
    if sv.get("deleteNamedVariableList"):
        s.append("deleteNamedVariableList")
    return s
