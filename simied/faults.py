"""Injeção de falhas por IED (RN-FLT-01 a RN-FLT-08)."""
from __future__ import annotations

import copy

FAULTS = {
    "atraso": {"regra": "RN-FLT-01", "titulo": "Atrasar reports / entregar fora de ordem",
               "params": {"ms": 500, "fora_de_ordem": False}},
    "descarte": {"regra": "RN-FLT-02", "titulo": "Descartar reports",
                 "params": {"um_a_cada": 2, "ponto": ""}},
    "duplicar": {"regra": "RN-FLT-03", "titulo": "Duplicar report ou repetir EntryID",
                 "params": {"modo": "duplicar"}},
    "confrev": {"regra": "RN-FLT-04", "titulo": "ConfRev diferente do CID", "params": {"valor": 999}},
    "rptid": {"regra": "RN-FLT-04", "titulo": "RptID diferente do escrito", "params": {"valor": "RPTID_ERRADO"}},
    "omitir_t": {"regra": "RN-FLT-04", "titulo": "Omitir t (estampa zerada nos reports)", "params": {}},
    "recusar_assoc": {"regra": "RN-FLT-05", "titulo": "Recusar associação", "params": {}},
    "fechar_apos": {"regra": "RN-FLT-05", "titulo": "Fechar associação após N segundos",
                    "params": {"segundos": 30}},
    "erro_escrita": {"regra": "RN-FLT-06", "titulo": "Erro fixo na escrita de atributo de RCB",
                     "params": {"atributo": "RptEna", "codigo": 2, "rcb": ""}},
    "silenciar_read": {"regra": "RN-FLT-07", "titulo": "Não responder Read (TCP aberto)", "params": {}},
    "report_unico": {"regra": "RN-FLT-08", "titulo": "Report só ao primeiro cliente (RCB compartilhado)",
                     "params": {}},
}


class Faults:
    def __init__(self, cfg: dict | None = None):
        self.state: dict = {k: {"ativo": False, **copy.deepcopy(v["params"])} for k, v in FAULTS.items()}
        self.counters: dict = {}
        if cfg:
            self.update(cfg)

    def update(self, cfg: dict):
        for k, v in cfg.items():
            if k not in self.state:
                raise ValueError(f"falha desconhecida: {k}")
            if isinstance(v, bool):
                v = {"ativo": v}
            for pk, pv in v.items():
                if pk != "ativo" and pk not in FAULTS[k]["params"]:
                    raise ValueError(f"parâmetro desconhecido {pk} na falha {k}")
                default = FAULTS[k]["params"].get(pk)
                if isinstance(default, bool):
                    pv = bool(pv)
                elif isinstance(default, int) and not isinstance(default, bool):
                    pv = int(pv)
                self.state[k][pk] = pv
            self.state[k]["ativo"] = bool(self.state[k]["ativo"])

    def on(self, k: str) -> bool:
        return self.state[k]["ativo"]

    def p(self, k: str, name: str):
        return self.state[k][name]

    def active(self) -> list[dict]:
        return [{"id": k, "regra": FAULTS[k]["regra"], "titulo": FAULTS[k]["titulo"],
                 **{pk: pv for pk, pv in v.items() if pk != "ativo"}}
                for k, v in self.state.items() if v["ativo"]]

    def to_json(self) -> dict:
        return {k: {"regra": FAULTS[k]["regra"], "titulo": FAULTS[k]["titulo"], **v} for k, v in self.state.items()}

    def config(self) -> dict:
        return copy.deepcopy(self.state)
