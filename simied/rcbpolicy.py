"""Política de acesso por RCB: como o servidor responde às escritas MMS de cada cliente.

Reproduz IEDs que recusam o uso de alguns RCBs (por exemplo, o Ingeteam i15, em que o SAGE só consegue
usar o RCB dele). A resposta é sempre por item do Write; o Write inteiro nunca é recusado.

Configuração por nome-base do RCB (o nome do ReportControl no CID, sem sufixo de instância):
    politica    livre | ocupado | negado | somente_leitura
    atributos   {atributo: ok | erro2 | erro3}, prevalece sobre a política
    ips         IPs de cliente tratados como 'livre' (vazio = a política vale para todos)
    ocultar     o RCB some do GetNameList e do GetVariableAccessAttributes
    instanciar  True = sufixo 01..max, False = sem sufixo, None = regra de indexação do perfil
O perfil define o padrão (rcb.politicas) e o IED sobrescreve chave a chave (cfg.rcb_politicas).
"""
from __future__ import annotations

import ipaddress

from .mms.types import ERR_ACCESS_DENIED, ERR_TEMP_UNAVAILABLE, AccessError

POLITICAS = ("livre", "ocupado", "negado", "somente_leitura")
CODIGOS = {"ok": None, "erro2": ERR_TEMP_UNAVAILABLE, "erro3": ERR_ACCESS_DENIED}
ATRIBUTOS = ("RptID", "RptEna", "Resv", "DatSet", "ConfRev", "OptFlds", "BufTm", "SqNum", "TrgOps", "IntgPd",
             "GI", "PurgeBuf", "EntryID", "TimeOfEntry", "ResvTms", "Owner")
CHAVES = ("politica", "atributos", "ips", "ocultar", "instanciar")
PADRAO = {"politica": "livre", "atributos": {}, "ips": [], "ocultar": False, "instanciar": None}


def validate(entry: dict, where: str = "") -> dict:
    """Valida e normaliza uma entrada (parcial) de política; ValueError com mensagem clara."""
    pre = f"{where}: " if where else ""
    if not isinstance(entry, dict):
        raise ValueError(f"{pre}a política deve ser um objeto JSON")
    extra = set(entry) - set(CHAVES)
    if extra:
        raise ValueError(f"{pre}campos desconhecidos {sorted(extra)}; use {list(CHAVES)}")
    out: dict = {}
    if "politica" in entry:
        if entry["politica"] not in POLITICAS:
            raise ValueError(f"{pre}política '{entry['politica']}' inválida; use {list(POLITICAS)}")
        out["politica"] = entry["politica"]
    if "atributos" in entry:
        m = entry["atributos"] or {}
        if not isinstance(m, dict):
            raise ValueError(f"{pre}'atributos' deve ser um objeto {{atributo: ok|erro2|erro3}}")
        for a, v in m.items():
            if a not in ATRIBUTOS:
                raise ValueError(f"{pre}atributo de RCB desconhecido '{a}'; use {list(ATRIBUTOS)}")
            if v not in CODIGOS:
                raise ValueError(f"{pre}resposta '{v}' inválida para {a}; use ok, erro2 ou erro3")
        out["atributos"] = dict(m)
    if "ips" in entry:
        ips = entry["ips"] or []
        if isinstance(ips, str):
            ips = [x for x in ips.replace(",", " ").split() if x]
        if not isinstance(ips, list):
            raise ValueError(f"{pre}'ips' deve ser uma lista de endereços IP")
        norm = []
        for ip in ips:
            try:
                norm.append(str(ipaddress.ip_address(str(ip).strip())))
            except ValueError as e:
                raise ValueError(f"{pre}IP inválido: {ip!r}") from e
        out["ips"] = sorted(set(norm), key=norm.index)
    if "ocultar" in entry:
        if not isinstance(entry["ocultar"], bool):
            raise ValueError(f"{pre}'ocultar' deve ser true ou false")
        out["ocultar"] = entry["ocultar"]
    if "instanciar" in entry:
        if entry["instanciar"] is not None and not isinstance(entry["instanciar"], bool):
            raise ValueError(f"{pre}'instanciar' deve ser true, false ou null (regra do perfil)")
        out["instanciar"] = entry["instanciar"]
    return out


def validate_all(politicas: dict, where: str) -> None:
    if not isinstance(politicas, dict):
        raise ValueError(f"{where}: deve ser um objeto {{nome do RCB: política}}")
    for nome, entry in politicas.items():
        validate(entry, f"{where}.{nome}")


def effective(profile: dict, cfg: dict, base: str) -> tuple[dict, dict]:
    """(política efetiva, origem de cada campo: 'padrao' | 'perfil' | 'ied')."""
    prof = (profile.get("rcb", {}).get("politicas") or {}).get(base) or {}
    ied = (cfg.get("rcb_politicas") or {}).get(base) or {}
    eff = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in PADRAO.items()}
    orig = dict.fromkeys(PADRAO, "padrao")
    for src, entry in (("perfil", prof), ("ied", ied)):
        for k in CHAVES:
            if k in entry:
                eff[k] = entry[k]
                orig[k] = src
    return eff, orig


class RcbPolicy:
    """Política em vigor num RCB em execução; muda sem reiniciar o IED (vale para as próximas escritas)."""

    def __init__(self, conf: dict | None = None):
        self.update(conf or PADRAO)

    def update(self, conf: dict):
        self.politica = conf.get("politica", "livre")
        self.atributos = dict(conf.get("atributos") or {})
        self.ips = list(conf.get("ips") or [])
        self.ocultar = bool(conf.get("ocultar"))
        self.instanciar = conf.get("instanciar")

    def for_client(self, ip: str | None) -> str:
        """Política aplicada a um cliente. Com lista de IPs, quem está nela é tratado como 'livre' e
        quem está fora recebe a política do RCB ('negado' se a política for 'livre', senão a lista não teria efeito)."""
        if not self.ips:
            return self.politica
        if ip in self.ips:
            return "livre"
        return "negado" if self.politica == "livre" else self.politica

    def check(self, ip: str | None, attr: str, value) -> None:
        """Levanta AccessError se a escrita deve ser recusada; retorna para seguir o tratamento normal."""
        pol = self.for_client(ip)
        if self.politica == "somente_leitura" and attr == "RptEna" and value:
            raise AccessError(ERR_ACCESS_DENIED, "política somente_leitura: o RCB nunca é habilitado")
        ov = self.atributos.get(attr)
        if ov is not None:
            if CODIGOS[ov] is not None:
                raise AccessError(CODIGOS[ov], f"política do RCB: {attr} responde {ov}")
            return
        if pol == "livre":
            return
        if pol == "ocupado":
            code = ERR_ACCESS_DENIED if attr in ("DatSet", "RptEna") else ERR_TEMP_UNAVAILABLE
            raise AccessError(code, f"política ocupado{' (IP fora da lista)' if self.ips else ''}")
        raise AccessError(ERR_ACCESS_DENIED, f"política {pol}{' (IP fora da lista)' if self.ips else ''}")

    def to_json(self) -> dict:
        return {"politica": self.politica, "atributos": dict(self.atributos), "ips": list(self.ips),
                "ocultar": self.ocultar, "instanciar": self.instanciar}
