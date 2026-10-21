"""Serviço de controle IEC 61850 (7-2 cláusula 20, 8-1 cláusula 20): Operate, Select (SBO), SelectWithValue
(SBOw), Cancel, LastApplError e CommandTermination, com o registro de cada comando recebido.

Cada DO com atributos de FC=CO (Oper/SBOw/Cancel/SBO) vira um objeto de controle. O ctlModel vem do CF do
próprio DO. Sem lógica associada, o simulador faz o "eco": o estado (stVal) do mesmo DO passa a valer o comando,
opcionalmente com tempo de manobra (intermediário). Com uma lógica que usa o bloco Comando, quem decide é a lógica; o estado do próprio DO ainda faz eco se nenhuma lógica escreve nele.
Bloqueios (intertravamento, sincronismo, processo) vêm dos blocos Bloqueio das lógicas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from .mms.pdu import PDU_UNCONF, ObjName, enc_var_list
from .ber import tlv
from .mms.types import (ERR_ACCESS_DENIED, ERR_TEMP_UNAVAILABLE, ERR_TYPE_INCONSISTENT, AccessError, Bits, MmsType,
                        UtcTime, coerce, dec_data_raw, enc_data, raw_to_json)

log = logging.getLogger("simied.control")

CTL_MODELS = {0: "status-only", 1: "direct-with-normal-security", 2: "sbo-with-normal-security",
              3: "direct-with-enhanced-security", 4: "sbo-with-enhanced-security"}
OR_CAT = {0: "not-supported", 1: "bay-control", 2: "station-control", 3: "remote-control", 4: "automatic-bay",
          5: "automatic-station", 6: "automatic-remote", 7: "maintenance", 8: "process"}
ADD_CAUSE = {0: "Unknown", 1: "Not-supported", 2: "Blocked-by-switching-hierarchy", 3: "Select-failed",
             4: "Invalid-position", 5: "Position-reached", 6: "Parameter-change-in-execution", 7: "Step-limit",
             8: "Blocked-by-Mode", 9: "Blocked-by-process", 10: "Blocked-by-interlocking",
             11: "Blocked-by-synchrocheck", 12: "Command-already-in-execution", 13: "Blocked-by-health",
             14: "1-of-n-control", 15: "Abortion-by-cancel", 16: "Time-limit-over", 17: "Abortion-by-trip",
             18: "Object-not-selected", 19: "Object-already-selected", 20: "No-access-authority",
             21: "Ended-with-overshoot", 22: "Abortion-due-to-deviation", 23: "Abortion-by-communication-loss",
             24: "Blocked-by-command", 25: "None", 26: "Inconsistent-parameters", 27: "Locked-by-other-client"}
CAUSA_TXT = {1: "não suportado", 5: "já está na posição pedida", 8: "bloqueado pelo modo (Test)",
             9: "bloqueado pelo processo", 10: "bloqueado por intertravamento", 11: "bloqueado por sincronismo",
             12: "comando anterior ainda em execução", 16: "tempo de execução esgotado", 18: "objeto não selecionado",
             19: "objeto já selecionado", 26: "parâmetros diferentes da seleção", 27: "selecionado por outro cliente",
             15: "cancelado"}
SERVICOS = {"Oper": "Operate", "SBOw": "SelectWithValue", "SBO": "Select", "Cancel": "Cancel"}
BLOQ_CAUSA = {"intertravamento": 10, "sincronismo": 11, "processo": 9}
# SAGE (Manual de Configuração, Anexo 17, entidade CGF): o atributo KCONV diz se o comando é precedido de
# Select (SBO = leitura do $SBO, SBOw = SelectWithValue) e se o SAGE espera o CommandTermination (TERM).
SAGE_KCONV = {1: "", 2: "SBO", 3: "TERM", 4: "SBOw TERM"}

_LAE_T = MmsType("struct", comps=[
    ("CntrlObj", MmsType("vis", -129)), ("Error", MmsType("int", 8)),
    ("Origin", MmsType("struct", comps=[("orCat", MmsType("int", 8)), ("orIdent", MmsType("octet", -64))])),
    ("ctlNum", MmsType("uint", 8)), ("AddCause", MmsType("int", 8))])


def info_report_vars(names: list[ObjName], results: list[bytes]) -> bytes:
    """InformationReport com listOfVariable (LastApplError e CommandTermination)."""
    return tlv(PDU_UNCONF, tlv(0xA0, enc_var_list(names) + tlv(0xA0, b"".join(results))))


class CmdReject(Exception):
    def __init__(self, cause: int, code: int = ERR_ACCESS_DENIED, detail: str = "", error: int = 1):
        super().__init__(detail or CAUSA_TXT.get(cause, ADD_CAUSE.get(cause, "")))
        self.cause, self.code, self.error = cause, code, error


class CtlObj:
    def __init__(self, svc: "ControlService", domain: str, ln: str, do: str, co, st, cf, cdc: str, desc: str):
        self.svc = svc
        self.domain, self.ln, self.do = domain, ln, do
        self.co, self.st, self.cf = co, st, cf
        self.cdc, self.desc = cdc, desc
        self.sel_assoc = None
        self.sel_value = None
        self.sel_handle = None
        self.busy = None  # tarefa da execução com segurança reforçada
        self.last_oper = None

    @property
    def key(self) -> str:
        return f"{self.domain}/{self.ln}${self.do}"

    @property
    def ref(self) -> str:
        return f"{self.domain}/{self.ln}.{self.do.replace('$', '.')}"

    def mms(self, attr: str = "Oper") -> str:
        return f"{self.domain}/{self.ln}$CO${self.do}${attr}"

    def _cf(self, name, default):
        n = self.cf.child(name) if self.cf is not None else None
        return n.value if n is not None and n.value is not None else default

    @property
    def model_from_cid(self) -> bool:
        v = self._cf("ctlModel", None)
        return isinstance(v, int) and v in CTL_MODELS

    @property
    def model(self) -> int:
        if self.model_from_cid:
            return self._cf("ctlModel", None)
        comps = self.co.comps or {}
        return 4 if "SBOw" in comps else 2 if "SBO" in comps else 1

    @property
    def sbo_timeout(self) -> float:
        v = self._cf("sboTimeout", 0)
        return (int(v) if v else 30000) / 1000.0

    @property
    def oper_timeout(self) -> float:
        v = self._cf("operTimeout", 0)
        return (int(v) if v else int(self.svc.conf(self).get("timeout_ms", 10000))) / 1000.0

    def sage(self) -> dict:
        """Como o ponto de controle (CGF) do SAGE deve ser configurado para este objeto."""
        m = self.model
        kconv = SAGE_KCONV.get(m)
        cgf = f"{self.ln}$CO${self.do}"
        if kconv is None:
            return {"cgf": cgf, "kconv": None, "texto": "objeto só de estado (status-only): não aceita comando; "
                    "no SAGE comande o objeto de controle (ex.: CSWI) e não este"}
        partes = []
        if m == 2:
            partes.append("Select pela leitura do $SBO antes do Operate")
        if m == 4:
            partes.append("SelectWithValue ($SBOw) antes do Operate")
        if m in (1, 3):
            partes.append("Operate direto, sem Select")
        partes.append(f"espera o CommandTermination (até {self.oper_timeout * 1000:g} ms após o Operate)" if m in (3, 4)
                      else "sem CommandTermination")
        return {"cgf": cgf, "kconv": kconv, "texto": "; ".join(partes)}

    def sage_hint(self) -> str:
        sg = self.sage()
        if sg["kconv"] is None:
            return f"SAGE: {sg['texto']}"
        k = f"KCONV = {sg['kconv']}" if sg["kconv"] else "KCONV vazio (sem SBO, SBOw nem TERM)"
        return (f"SAGE: no CGF …{sg['cgf']} use {k}, porque o CID define ctlModel "
                f"{CTL_MODELS[self.model]} ({sg['texto']})")

    @property
    def point(self):
        return self.svc.ied.by_path.get(f"{self.domain}/{self.ln}$ST${self.do}")

    def services(self) -> list:
        """Atributos de controle que existem no CID (o DOType pode ter todos, qualquer que seja o ctlModel)."""
        return [k for k in ("SBO", "SBOw", "Oper", "Cancel") if k in (self.co.comps or {})]

    def valid_services(self) -> list:
        """Dos atributos existentes, os que o ctlModel aceita (o resto é recusado)."""
        ok = {0: set(), 1: {"Oper"}, 2: {"SBO", "Oper", "Cancel"}, 3: {"Oper"}, 4: {"SBOw", "Oper", "Cancel"}}
        return [k for k in self.services() if k in ok.get(self.model, set())]

    def target(self, ctl_val):
        """Valor de estado que o comando pede (para eco, posição atingida e CommandTermination)."""
        if self.cdc in ("DPC",):
            return 2 if ctl_val else 1
        if self.cdc in ("SPC",):
            return bool(ctl_val)
        if self.cdc in ("INC", "ENC") and isinstance(ctl_val, int):
            return ctl_val
        return None

    def direction(self, ctl_val) -> str:
        if isinstance(ctl_val, bool):
            return "fechar" if ctl_val else "abrir"
        return "valor"

    def val_txt(self, ctl_val) -> str:
        if isinstance(ctl_val, bool):
            if self.cdc == "DPC":
                return "CLOSE (on/1)" if ctl_val else "TRIP (off/0)"
            return "LIGAR (true)" if ctl_val else "DESLIGAR (false)"
        return json.dumps(ctl_val, default=str)

    def json(self) -> dict:
        p = self.point
        conf = self.svc.conf(self)
        return {
            "ref": self.ref, "chave": self.key, "mms": self.mms(), "cdc": self.cdc, "desc": self.desc,
            "ctlModel": CTL_MODELS.get(self.model, str(self.model)), "ctlModel_n": self.model,
            "ctlModel_origem": "CID" if self.model_from_cid else "deduzido (CID sem ctlModel)",
            "operTimeout_ms": round(self.oper_timeout * 1000),
            "operTimeout_origem": "CID" if self._cf("operTimeout", 0) else "simulador (CID sem operTimeout)",
            "sboTimeout_ms": round(self.sbo_timeout * 1000),
            "sboTimeout_origem": "CID" if self._cf("sboTimeout", 0) else "padrão (CID sem sboTimeout)",
            "sage": self.sage(),
            "servicos": self.services(), "servicos_validos": self.valid_services(), "estado": p.value() if p is not None else None,
            "ponto": p.path if p is not None else None, "selecionado_por": self.sel_assoc.peer_str if self.sel_assoc else None,
            "em_execucao": self.busy is not None, "bloqueios": self.svc.blocks_for(self),
            "execucao": "lógica" if self.key in self.svc.logic_refs() else "eco", "config": conf,
        }


class ControlService:
    def __init__(self, ied):
        self.ied = ied
        self.objs: dict[str, CtlObj] = {}
        self.blocks: dict[str, list] = {}  # chave -> [{direcao, causa, so_com_check, logica}]

    # ------------------------------------------------------------------ montagem
    def build(self):
        self.objs = {}
        descs = {}
        for p in self.ied.points:
            descs[f"{p.domain}/{p.ln}${p.do}"] = (p.cdc, p.desc)
        for dom in self.ied.domains.values():
            for ln_name, ln in dom.lns.items():
                co = (ln.comps or {}).get("CO")
                if co is None:
                    continue
                for do_rel, node in self._ctl_nodes(co):
                    st = dom.lookup(f"{ln_name}$ST${do_rel}")
                    cf = dom.lookup(f"{ln_name}$CF${do_rel}")
                    cdc, desc = descs.get(f"{dom.name}/{ln_name}${do_rel}", (node.cdc or "", ""))
                    if not cdc:
                        cdc = getattr(st, "cdc", "") if st is not None else ""
                    o = CtlObj(self, dom.name, ln_name, do_rel, node, st, cf, cdc, desc)
                    self.objs[o.key] = o

    def _ctl_nodes(self, node, rel=""):
        for name, c in (node.comps or {}).items():
            r = f"{rel}${name}" if rel else name
            if c.comps and any(k in c.comps for k in ("Oper", "SBOw", "SBO")):
                yield r, c
            elif c.comps and not c.is_leaf:
                yield from self._ctl_nodes(c, r)

    def conf(self, o: CtlObj) -> dict:
        base = {"tempo_ms": 0, "rejeitar_mesma_posicao": True, "timeout_ms": 10000}
        base.update(self.ied.cfg.get("controles", {}).get(o.key, {}))
        return base

    def logic_refs(self) -> set:
        lg = getattr(self.ied, "logic", None)
        return lg.command_refs() if lg is not None else set()

    def blocks_for(self, o: CtlObj) -> list:
        return list(self.blocks.get(o.key, []))

    def match(self, on: ObjName):
        """(objeto, serviço) se o nome é Oper/SBOw/SBO/Cancel de um objeto de controle."""
        if on is None or on.scope != "dom":
            return None
        parts = on.item.split("$")
        if len(parts) < 4 or parts[1] != "CO":
            return None
        for i in range(len(parts) - 1, 2, -1):
            if parts[i] in SERVICOS:
                o = self.objs.get(f"{on.domain}/{parts[0]}${'$'.join(parts[2:i])}")
                if o is not None:
                    return o, parts[i], i < len(parts) - 1
        return None

    # ------------------------------------------------------------------ MMS
    def read_sbo(self, assoc, o: CtlObj) -> bytes:
        rec = self._rec(assoc, o, "SBO", None, {})
        try:
            if o.model != 2:
                raise CmdReject(1, detail=f"Select (leitura do SBO) não vale para ctlModel {CTL_MODELS.get(o.model)}")
            self._check_select(assoc, o)
            self._select(assoc, o, None)
            rec.update(resultado="aceito", detalhe=f"selecionado por {o.sbo_timeout:g} s")
            out = enc_data(MmsType("vis", -129), f"{o.domain}/{o.ln}$CO${o.do}")
        except CmdReject as e:
            rec.update(resultado="recusado", causa=ADD_CAUSE.get(e.cause), causa_txt=str(e))
            if e.cause == 1:
                rec["dica_sage"] = o.sage_hint()
            out = enc_data(MmsType("vis", -129), "")  # select negativo: string vazia
        self._log(rec)
        return out

    def write(self, assoc, o: CtlObj, svc: str, sub: bool, data: tuple) -> int | None:
        raw_tlv = tlv(data[0], data[1])
        try:
            raw = dec_data_raw(*data)
        except Exception:  # noqa: BLE001
            return ERR_TYPE_INCONSISTENT
        node = o.co.comps.get(svc)
        if node is None and not sub:
            self._log(self._rec(assoc, o, svc, None, {}, resultado="recusado",
                                causa_txt=f"o objeto não tem o atributo {svc} no CID", dica_sage=o.sage_hint()))
            return ERR_ACCESS_DENIED
        if sub or node is None or svc == "SBO":
            self._log(self._rec(assoc, o, svc, None, {}, resultado="recusado",
                                causa_txt="escrita em parte do atributo de controle (escreva a estrutura inteira)"))
            return ERR_ACCESS_DENIED
        try:
            vals = dict(zip(node.comps.keys(), coerce(node.mt, raw)))
        except AccessError as e:
            self._log(self._rec(assoc, o, svc, None, {}, resultado="recusado", causa_txt=f"estrutura incompatível: {e}"))
            return e.code
        rec = self._rec(assoc, o, svc, vals, raw)
        try:
            if svc == "Cancel":
                self._cancel(assoc, o)
                rec.update(resultado="aceito", detalhe="seleção cancelada")
            elif svc == "SBOw":
                if o.model != 4:
                    raise CmdReject(1, detail=f"SelectWithValue não vale para ctlModel {CTL_MODELS.get(o.model)}")
                self._check_select(assoc, o)
                self._check_exec(o, vals)
                self._select(assoc, o, vals.get("ctlVal"))
                rec.update(resultado="aceito", detalhe=f"selecionado por {o.sbo_timeout:g} s")
            else:
                self._operate(assoc, o, vals, raw_tlv, rec)
        except CmdReject as e:
            rec.update(resultado="recusado", causa=ADD_CAUSE.get(e.cause), causa_txt=str(e))
            if e.cause == 1 or e.cause == 18 and svc == "Oper":
                # serviço que não combina com o ctlModel: quase sempre é o KCONV do CGF no SAGE
                rec["dica_sage"] = o.sage_hint()
            self._last_appl_error(assoc, o, svc, vals, e.cause, e.error)
            self._log(rec)
            return e.code
        self._log(rec)
        return None

    # ------------------------------------------------------------------ máquina de estados
    def _check_select(self, assoc, o: CtlObj):
        if o.busy is not None:
            raise CmdReject(12, ERR_TEMP_UNAVAILABLE)
        if o.sel_assoc is not None and o.sel_assoc is not assoc:
            raise CmdReject(27 if o.model == 4 else 19, ERR_TEMP_UNAVAILABLE)

    def _check_exec(self, o: CtlObj, vals: dict):
        ctl = vals.get("ctlVal")
        check = vals.get("Check")
        chk_sync = isinstance(check, Bits) and check.get(0)
        chk_ilk = isinstance(check, Bits) and check.n > 1 and check.get(1)
        d = o.direction(ctl)
        for b in self.blocks.get(o.key, []):
            if b["direcao"] not in ("ambos", d):
                continue
            causa = BLOQ_CAUSA.get(b["causa"], 10)
            if b.get("so_com_check"):
                if causa == 10 and not chk_ilk or causa == 11 and not chk_sync:
                    continue
            raise CmdReject(causa, detail=f"{CAUSA_TXT[causa]} (lógica “{b['logica']}”)")
        tgt = o.target(ctl)
        p = o.point
        if tgt is not None and p is not None and self.conf(o).get("rejeitar_mesma_posicao") and p.value() == tgt:
            raise CmdReject(5)

    def _select(self, assoc, o: CtlObj, value):
        self._deselect(o, notify=False)
        o.sel_assoc, o.sel_value = assoc, value
        loop = asyncio.get_running_loop()
        o.sel_handle = loop.call_later(o.sbo_timeout, self._sel_timeout, o)
        self._set_st(o, stSeld=True)

    def _sel_timeout(self, o: CtlObj):
        if o.sel_assoc is not None:
            self._log({"ts": time.time(), "cliente": o.sel_assoc.peer_str, "ref": o.ref, "mms": o.mms("SBO"),
                       "servico": "Timeout", "resultado": "info", "detalhe": f"seleção expirou ({o.sbo_timeout:g} s)"})
        self._deselect(o)

    def _deselect(self, o: CtlObj, notify=True):
        if o.sel_handle is not None:
            o.sel_handle.cancel()
        was = o.sel_assoc is not None
        o.sel_assoc = o.sel_value = o.sel_handle = None
        if was and notify:
            self._set_st(o, stSeld=False)

    def _cancel(self, assoc, o: CtlObj):
        if o.busy is not None:
            raise CmdReject(6, detail="comando já em execução; não dá para cancelar")
        if o.sel_assoc is None:
            raise CmdReject(18)
        if o.sel_assoc is not assoc:
            raise CmdReject(27, ERR_TEMP_UNAVAILABLE)
        self._deselect(o)

    def _operate(self, assoc, o: CtlObj, vals: dict, raw_tlv: bytes, rec: dict):
        m = o.model
        if m == 0:
            raise CmdReject(1, detail="ctlModel status-only: o objeto não aceita comando")
        if m in (2, 4):
            if o.sel_assoc is None:
                raise CmdReject(18)
            if o.sel_assoc is not assoc:
                raise CmdReject(27, ERR_TEMP_UNAVAILABLE)
            if m == 4 and o.sel_value is not None and vals.get("ctlVal") != o.sel_value:
                raise CmdReject(26)
        if o.busy is not None:
            raise CmdReject(12, ERR_TEMP_UNAVAILABLE)
        self._check_exec(o, vals)
        ctl = vals.get("ctlVal")
        if m in (2, 4):
            self._deselect(o, notify=False)
        orig = vals.get("origin")
        st = {"stSeld": False} if m in (2, 4) else {}
        if isinstance(orig, list) and len(orig) == 2:
            st["origin"] = orig
        if vals.get("ctlNum") is not None:
            st["ctlNum"] = vals["ctlNum"]
        self._set_st(o, **st)
        o.last_oper = raw_tlv
        by_logic = o.key in self.logic_refs()
        rec.update(resultado="aceito", execucao="lógica" if by_logic else "eco")
        if by_logic:
            self.ied.logic.command(o, ctl, rec)
            # A lógica que não escreve o estado do próprio objeto (só o XCBR, por exemplo) deixaria o stVal parado:
            # o próximo comando seria recusado como Position-reached e a terminação nunca sairia. Ele faz eco.
            if o.point is not None and not self.ied.logic.writes_point(o.point.path):
                rec["eco_estado"] = True
                asyncio.get_running_loop().call_soon(self._eco, o, ctl)
        else:  # depois da resposta do Write, como num relé real
            asyncio.get_running_loop().call_soon(self._eco, o, ctl)
        if m in (3, 4):
            rec["terminacao"] = "aguardando"
            o.busy = self.ied._spawn(self._await_termination(assoc, o, vals, raw_tlv, rec))

    def _eco(self, o: CtlObj, ctl):
        p = o.point
        tgt = o.target(ctl)
        if p is None or tgt is None:
            return
        ms = int(self.conf(o).get("tempo_ms") or 0)
        origin = f"comando {o.ref}"
        if ms > 0 and o.cdc == "DPC":
            self.ied.set_points([{"id": p.id, "valor": 0}], origin=origin + " (manobra)")

            async def fim():
                await asyncio.sleep(ms / 1000.0)
                self.ied.set_points([{"id": p.id, "valor": tgt}], origin=origin)
            self.ied._spawn(fim())
        elif ms > 0:
            async def fim2():
                await asyncio.sleep(ms / 1000.0)
                self.ied.set_points([{"id": p.id, "valor": tgt}], origin=origin)
            self.ied._spawn(fim2())
        else:
            self.ied.set_points([{"id": p.id, "valor": tgt}], origin=origin)

    async def _await_termination(self, assoc, o: CtlObj, vals: dict, raw_tlv: bytes, rec: dict):
        tgt = o.target(vals.get("ctlVal"))
        deadline = time.monotonic() + o.oper_timeout
        ok = tgt is None
        try:
            while not ok and time.monotonic() < deadline:
                p = o.point
                if p is not None and p.value() == tgt:
                    ok = True
                    break
                await asyncio.sleep(0.02)
            if assoc.closed:
                rec["terminacao"] = "não enviada (cliente desconectou)"
            elif ok:
                assoc.send_mms(info_report_vars([ObjName("dom", o.domain, o.mms()[len(o.domain) + 1:])], [raw_tlv]))
                rec["terminacao"] = "positiva"
            else:
                lae = self._lae_data(o, vals, 16, 2)
                assoc.send_mms(info_report_vars([ObjName("vmd", "", "LastApplError"),
                                                 ObjName("dom", o.domain, o.mms()[len(o.domain) + 1:])],
                                                [lae, raw_tlv]))
                rec["terminacao"] = f"negativa (Time-limit-over: estado não chegou em {o.oper_timeout:g} s)"
            rec["t_terminacao"] = time.time()
            self._log(rec, update=True)
        finally:
            o.busy = None

    def _lae_data(self, o: CtlObj, vals: dict, cause: int, error: int) -> bytes:
        orig = vals.get("origin") if isinstance(vals.get("origin"), list) else [0, b""]
        return enc_data(_LAE_T, [o.mms(), error, [int(orig[0] or 0), bytes(orig[1] or b"")],
                                 int(vals.get("ctlNum") or 0), cause])

    def _last_appl_error(self, assoc, o: CtlObj, svc: str, vals: dict, cause: int, error: int):
        if assoc is None or getattr(assoc, "closed", True) or not hasattr(assoc, "send_mms"):
            return
        assoc.send_mms(info_report_vars([ObjName("vmd", "", "LastApplError")], [self._lae_data(o, vals, cause, error)]))

    def _set_st(self, o: CtlObj, **vals):
        if o.st is None:
            return
        changes = []
        for name, v in vals.items():
            n = o.st.child(name)
            if n is None:
                continue
            leaves = list(n.leaves())
            new = v if isinstance(v, list) else [v]
            for leaf, nv in zip(leaves, new):
                if leaf.mt.kind == "octet":
                    nv = bytes(nv or b"")
                elif leaf.mt.kind in ("int", "uint"):
                    nv = int(nv or 0)
                elif leaf.mt.kind == "bool":
                    nv = bool(nv)
                if leaf.value != nv:
                    leaf.value = nv
                    if leaf.trg:
                        changes.append((o.domain, leaf.path, self.ied._reason(leaf, "dchg"), None))
        if changes:
            self.ied.engine.notify(changes)

    def association_closed(self, assoc):
        for o in self.objs.values():
            if o.sel_assoc is assoc:
                self._deselect(o)

    # ------------------------------------------------------------------ registro
    def _rec(self, assoc, o: CtlObj, svc: str, vals: dict | None, raw, **kw) -> dict:
        vals = vals or {}
        orig = vals.get("origin")
        rec = {"ts": time.time(), "cliente": assoc.peer_str if assoc is not None and hasattr(assoc, "peer_str") else "simulador",
               "ref": o.ref, "mms": o.mms(svc), "servico": SERVICOS.get(svc, svc), "ctlModel": CTL_MODELS.get(o.model),
               "cdc": o.cdc, "desc": o.desc}
        if "ctlVal" in vals:
            cv = vals["ctlVal"]
            rec["valor"] = cv if isinstance(cv, (bool, int, float, str)) else str(cv)
            rec["valor_txt"] = o.val_txt(cv)
        if isinstance(orig, list) and len(orig) == 2:
            ident = bytes(orig[1] or b"").rstrip(b"\x00")  # o SAGE manda o orIdent terminado em NUL
            rec["origem"] = {"orCat": orig[0], "orCat_txt": OR_CAT.get(orig[0], str(orig[0])),
                             "orIdent": ident.decode("latin-1", errors="replace") if ident and all(32 <= c < 127 for c in ident) else ident.hex()}
        if vals.get("ctlNum") is not None:
            rec["ctlNum"] = vals["ctlNum"]
        if isinstance(vals.get("T"), UtcTime):
            rec["T"] = vals["T"].iso()
        if vals.get("Test") is not None:
            rec["teste"] = bool(vals["Test"])
        chk = vals.get("Check")
        if isinstance(chk, Bits):
            rec["check"] = {"sincronismo": chk.get(0), "intertravamento": chk.n > 1 and chk.get(1)}
        if isinstance(raw, tuple):
            rec["dados"] = raw_to_json(raw)
        rec.update(kw)
        return rec

    def _log(self, rec: dict, update: bool = False):
        rec.setdefault("ied_id", self.ied.id)
        if update and rec.get("id"):
            self.ied.mgr.storage.update_cmd(rec)
        else:
            rec["id"] = self.ied.mgr.storage.add_cmd(rec)
        self.ied.emit("comando", rec)
        if not update:
            res = rec.get("resultado", "")
            self.ied.mms_event(None, rec.get("servico", ""), f"{rec.get('mms', '')} {rec.get('valor_txt', '')}".strip(),
                               res + (f" ({rec['causa']})" if rec.get("causa") else ""))

    # ------------------------------------------------------------------ comando local (tela)
    def local_command(self, key: str, valor, servico: str = "auto") -> dict:
        """Executa um comando como se viesse de um cliente, passando pelas mesmas verificações (sem MMS)."""
        o = self.objs.get(key)
        if o is None:
            from .ied import PointError
            raise PointError(f"objeto de controle inexistente: {key}", status=404)
        node = o.co.comps.get("Oper")
        ctl_t = node.comps["ctlVal"].mt if node is not None and "ctlVal" in (node.comps or {}) else MmsType("bool")
        ctl = bool(valor) if ctl_t.kind == "bool" else int(valor) if ctl_t.kind in ("int", "uint") else valor
        vals = {"ctlVal": ctl, "origin": [2, b"simulador"], "ctlNum": 0, "Check": Bits(2, 0)}
        local = _LocalAssoc()
        rec = self._rec(None, o, "Oper", vals, None, cliente="simulador (tela)")
        try:
            if o.model in (2, 4):
                self._check_select(local, o)
                self._check_exec(o, vals)
                self._select(local, o, ctl)
            raw_tlv = enc_data(node.mt, [self._default_for(c, vals) for c in node.comps.values()]) if node else b""
            self._operate(local, o, vals, raw_tlv, rec)
        except CmdReject as e:
            rec.update(resultado="recusado", causa=ADD_CAUSE.get(e.cause), causa_txt=str(e))
        self._log(rec)
        return rec

    @staticmethod
    def _default_for(node, vals):
        v = vals.get(node.name)
        if v is None:
            return node.mt.default()
        if node.mt.kind == "struct" and isinstance(v, list):
            return [x if x is not None else c.mt.default() for x, c in zip(v, node.comps.values())]
        return v


class _LocalAssoc:
    closed = False
    peer_str = "simulador (tela)"

    def send_mms(self, pdu: bytes):
        pass
