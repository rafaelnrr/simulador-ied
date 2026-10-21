"""IED simulado em execução (seções 3 a 9)."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque

from . import netif
from .clock import IedClock, q_from_dict, q_to_dict
from .control import ControlService
from .faults import Faults
from .logic import LogicEngine
from .mirror import MirrorManager
from .mms.pdu import ObjName
from .mms.server import Association, MmsServer, ServiceError
from .mms.types import (ERR_ACCESS_DENIED, ERR_ACCESS_UNSUPPORTED, ERR_NON_EXISTENT, AccessError, Bits,
                        UtcTime, coerce, dec_data_raw, raw_to_json)
from .model import (Builder, DataSet, Member, Point, build_points, link_points_to_datasets, resolve_fcda)
from .profiles import resolve as resolve_profile
from .profiles import services_list
from .rcbpolicy import effective as rcb_policy_effective
from .reports import Rcb, ReportEngine, decode_included
from .scl import SclFcda, SclRcb, parse_scl
from .subida import Subida

log = logging.getLogger("simied.ied")

MODES = ("manual", "espelhado", "invertido", "forcado")
DPS_TXT = {"intermediario": 0, "intermediate-state": 0, "aberto": 1, "off": 1, "fechado": 2, "on": 2,
           "invalido": 3, "bad-state": 3}
_NOVAL = object()


def _jeq(a, b) -> bool:
    """Compara valores como ficam gravados (JSON): tupla e lista contam como iguais."""
    def norm(x):
        return json.dumps(json.loads(json.dumps(x, default=str)), sort_keys=True)
    return norm(a) == norm(b)


class PointError(Exception):
    def __init__(self, msg: str, path: str = "", code: str = "invalido", status: int = 400):
        super().__init__(msg)
        self.path = path
        self.code = code
        self.status = status


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in ("1", "true", "on", "ligado", "sim"):
        return True
    if s in ("0", "false", "off", "desligado", "nao", "não"):
        return False
    raise ValueError(f"valor booleano inválido: {v}")


class IedRuntime:
    def __init__(self, mgr, iid: int, cfg: dict):
        self.mgr = mgr
        self.id = iid
        self.cfg = cfg
        self.state = "parado"
        self.error = ""
        self.clock = IedClock(cfg.get("relogio"))
        self.faults = Faults(cfg.get("falhas"))
        self.server: MmsServer | None = None
        self.frames: deque = deque(maxlen=200000)
        self.engine = ReportEngine(self)
        self.mirror = MirrorManager(self)
        self.control = ControlService(self)
        self.logic = LogicEngine(self)
        self.subida = Subida(self)
        self.domains: dict = {}
        self.points: list[Point] = []
        self.by_path: dict[str, Point] = {}
        self.leaf_point: dict = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.profile: dict = {}
        self.scl_ied = None
        self.scl = None
        self.ln_parts: dict = {}
        self.snapshot: dict | None = None
        self.stats = {"reports": 0, "mudancas": 0, "associacoes_total": 0}
        self.started_at = 0.0
        self.tasks: set = set()
        self.load()

    # ------------------------------------------------------------------ carga do modelo
    @property
    def name(self) -> str:
        return self.cfg.get("nome") or self.cfg.get("nome_ied") or f"IED {self.id}"

    @property
    def ied_name(self) -> str:
        return self.cfg.get("nome_ied") or (self.scl_ied.name if self.scl_ied else "")

    def load(self):
        data = self.mgr.read_cid(self.cfg["cid_arquivo"])
        scl = parse_scl(data)
        ied = next((i for i in scl.ieds if i.name == self.cfg.get("ied_scl")), scl.ieds[0])
        self.scl, self.scl_ied = scl, ied
        prof_raw = self.mgr.profiles.get(self.cfg.get("perfil") or "") or {}
        self.profile = resolve_profile(prof_raw, self.cfg.get("perfil_sobrescritas"), ied.services, scl.edition)
        self.profile["id"] = self.cfg.get("perfil") or "(nenhum)"
        b = Builder(scl, ied)
        b.ied_name_override = self.cfg.get("nome_ied") or ied.name
        self.domains = b.build()
        errors = list(scl.errors) + b.errors
        warnings = list(scl.warnings) + b.warnings
        self.ln_parts = {}
        for ld in ied.lds:
            dn = b.ld_domain_name(ld)
            for ln in ld.lns:
                self.ln_parts[(dn, ln.name)] = (ld.inst, ln.prefix, ln.ln_class, ln.inst)
        # DataSets do CID e extras
        for ld in ied.lds:
            dom = self.domains.get(b.ld_domain_name(ld))
            if dom is None:
                continue
            for ln in ld.lns:
                for sds in ln.datasets:
                    self._add_dataset(b, dom, ln.name, sds.name, sds.desc, sds.fcdas, False, errors)
        for x in self.cfg.get("extras", {}).get("datasets", []):
            dom = next((d for d in self.domains.values() if d.inst == x["ld"]), None)
            if dom is None:
                errors.append(f"DataSet extra {x['name']}: LD {x['ld']} inexistente")
                continue
            fc = [SclFcda(**f) for f in x["fcdas"]]
            self._add_dataset(b, dom, x.get("ln", "LLN0"), x["name"], x.get("desc", ""), fc, True, errors)
        maxds = self.profile["rcb"].get("max_datasets")
        nds = sum(len(d.datasets) for d in self.domains.values())
        if maxds and nds > maxds:
            warnings.append(f"{nds} DataSets excedem o máximo do perfil/CID ({maxds})")
        # RCBs
        self.engine = ReportEngine(self)
        pos = self.profile["rcb"].get("posicao", "cid")
        for ld in ied.lds:
            dom = self.domains.get(b.ld_domain_name(ld))
            if dom is None:
                continue
            for ln in ld.lns:
                for r in ln.rcbs:
                    if pos == "LLN0" and ln.ln_class != "LLN0":
                        warnings.append(f"Perfil exige RCBs só no LLN0; {ln.name}.{r.name} existe no CID e foi mantido")
                    self._add_rcbs(dom, ln.name, r, False, warnings)
        for x in self.cfg.get("extras", {}).get("rcbs", []):
            dom = next((d for d in self.domains.values() if d.inst == x["ld"]), None)
            if dom is None:
                errors.append(f"RCB extra {x['name']}: LD {x['ld']} inexistente")
                continue
            r = SclRcb(x["name"], x["datset"], x.get("rptid", ""), int(x.get("conf_rev", 1)),
                       bool(x.get("buffered")), int(x.get("buf_time", 0)), int(x.get("intg_pd", 0)),
                       True, dict(x.get("trgops", {})), dict(x.get("optflds", {})), int(x.get("max", 1)),
                       x.get("desc", ""), True)
            self._add_rcbs(dom, x.get("ln", "LLN0"), r, True, warnings)
        maxr = self.profile["rcb"].get("max_rcbs")
        if maxr and len(self.engine.rcbs) > maxr:
            warnings.append(f"{len(self.engine.rcbs)} instâncias de RCB excedem o máximo do perfil/CID ({maxr})")
        for d in self.domains.values():
            for ln in d.lns.values():
                ln.finalize()
            d.reindex()
        # pontos
        self.points = build_points(self.domains, b)
        link_points_to_datasets(self.points, self.all_datasets())
        self.by_path = {p.path: p for p in self.points}
        self.leaf_point = {}
        for p in self.points:
            for leaf in (p.v, p.vi, p.a, p.ai):
                if leaf is not None:
                    self.leaf_point[(p.domain, leaf.path)] = p
        self.engine.rebuild_index()
        self.errors = errors
        self.warnings = warnings
        self.cfg["ip_cid_real"] = ied.address.get("IP", "")
        self._restore_state()
        self.control.build()
        self.logic.load(self.cfg.get("logicas", []))

    def _add_dataset(self, b, dom, ln_name, name, desc, fcdas, extra, errors):
        ds = DataSet(dom.name, ln_name, name, desc=desc, extra=extra, fcdas=fcdas)
        for f in fcdas:
            m, why = resolve_fcda(self.domains, b, dom, f, b.ied_name_override)
            if m is None:
                errors.append(f"DataSet {dom.name}/{ln_name}.{name}: FCDA {f.text()} ignorado ({why})")
                continue
            ds.members.append(m)
        if not ds.members:
            errors.append(f"DataSet {dom.name}/{ln_name}.{name} sem membros válidos")
        dom.datasets[ds.item] = ds

    def _add_rcbs(self, dom, ln_name, r: SclRcb, extra: bool, warnings: list):
        lnn = dom.lns.get(ln_name)
        if lnn is None:
            warnings.append(f"RCB {r.name}: LN {ln_name} inexistente")
            return
        if r.datset and f"{ln_name}${r.datset}" not in dom.datasets:
            warnings.append(f"RCB {dom.name}/{ln_name}.{r.name} referencia DataSet inexistente '{r.datset}'")
        if r.max < 3:
            warnings.append(f"RCB {dom.name}/{ln_name}.{r.name}: RptEnabled max={r.max}; 3 SCADAs em paralelo "
                            f"precisam de 3 instâncias (RN-RCB-04)")
        pol, _ = rcb_policy_effective(self.profile, self.cfg, r.name)
        idx = self.profile["rcb"].get("indexacao", "se_max_maior_1")
        if pol["instanciar"] is True:
            names = [f"{r.name}{i:02d}" for i in range(1, max(r.max, 1) + 1)]
        elif pol["instanciar"] is False:
            names = [r.name]
        elif idx == "sempre" or (idx == "cid" and r.indexed):
            names = [f"{r.name}{i:02d}" for i in range(1, r.max + 1)]
        elif idx == "nunca":
            names = [r.name] if r.max <= 1 else [f"{r.name}{i}" for i in range(1, r.max + 1)]
        else:
            names = [f"{r.name}{i:02d}" for i in range(1, r.max + 1)] if r.max > 1 else [r.name]
        fc = "BR" if r.buffered else "RP"
        from .model import Node
        fcn = lnn.comps.get(fc)
        if fcn is None:
            fcn = Node(fc, lnn)
            fcn.fc = fc
            fcn.comps = {}
            fcn.domain = dom.name
            lnn.comps[fc] = fcn
        for n in names:
            if n in fcn.comps:
                warnings.append(f"RCB duplicado {dom.name}/{ln_name}${fc}${n}; ignorado")
                continue
            rcb = Rcb(self.engine, dom.name, ln_name, n, r, extra)
            rcb.policy.update(pol)
            node = rcb.build_node(fcn)
            for x in node.walk():
                x.domain = dom.name
            fcn.comps[n] = node
            self.engine.rcbs.append(rcb)

    def _restore_state(self):
        states = self.mgr.storage.point_states(self.id)
        restore_values = self.cfg.get("opcoes", {}).get("restaurar_valores", True)
        init_q = self.profile.get("qualidade", {}).get("inicial") or {}
        for p in self.points:
            if p.q is not None and init_q and init_q != {"validity": "good"}:
                try:
                    p.q.value = q_from_dict(init_q, p.q.value)
                except ValueError:
                    pass
            p.cid_val = p.value() if p.kind != "ro" else None
            st = states.get(p.path)
            if not st:
                continue
            p.mode = st.get("modo", "manual")
            p.prev_mode = st.get("modo_anterior", "manual")
            p.link = st.get("vinculo")
            p.writable = bool(st.get("escrevivel"))
            # o CID mudou o valor deste ponto desde que o estado foi salvo (editor de CID, reimportação): vale o CID
            cid_mudou = "valor_cid" in st and not _jeq(st["valor_cid"], p.cid_val)
            if restore_values and p.kind != "ro" and not cid_mudou:
                try:
                    for leaf, val in self._convert(p, st.get("valor")).items():
                        leaf.value = val
                    if p.q is not None and st.get("q") is not None:
                        p.q.value = Bits(13, int(st["q"]))
                    if p.t is not None and st.get("t"):
                        p.t.value = UtcTime.decode_value(bytes.fromhex(st["t"]))
                except (ValueError, TypeError, PointError):
                    pass
        # o relé real nunca envia t zerado (1970): ponto sem estampa recebe a hora do IED na carga
        boot = self.clock.stamp()
        for p in self.points:
            if p.t is not None and (not isinstance(p.t.value, UtcTime) or
                                    (p.t.value.secs == 0 and p.t.value.frac == 0)):
                p.t.value = boot

    def _persist_point(self, p: Point):
        self.mgr.storage.queue_point(self.id, p.path, {
            "valor": p.value() if p.kind != "ro" else None, "valor_cid": p.cid_val,
            "q": p.q.value.val if p.q is not None else None,
            "t": p.t.value.encode_value().hex() if p.t is not None and p.t.value else None,
            "modo": p.mode, "modo_anterior": p.prev_mode, "vinculo": p.link, "escrevivel": p.writable,
        })

    # ------------------------------------------------------------------ acesso ao modelo
    def all_datasets(self) -> list[DataSet]:
        return [ds for d in self.domains.values() for ds in d.datasets.values()]

    def dataset_by_ref(self, ref: str) -> DataSet | None:
        if not ref or "/" not in ref:
            return None
        dom, item = ref.split("/", 1)
        d = self.domains.get(dom)
        if d is None:
            return None
        return d.datasets.get(item.replace(".", "$"))

    def lookup(self, on: ObjName):
        if on is None or on.scope != "dom":
            return None
        d = self.domains.get(on.domain)
        return d.lookup(on.item) if d else None

    def find_nvl(self, assoc, on: ObjName) -> DataSet | None:
        if on is None:
            return None
        if on.scope == "dom":
            d = self.domains.get(on.domain)
            return d.datasets.get(on.item) if d else None
        if on.scope == "aa":
            return assoc.nvls.get(on.item)
        return None

    def _hidden_nodes(self, domain: str) -> set:
        """Nós que o GetNameList e o GetVariableAccessAttributes não mostram: RCBs com 'ocultar' e
        o nó de FC (BR/RP) que fica vazio quando todos os RCBs dele estão ocultos."""
        hidden = {r.node for r in self.engine.rcbs if r.policy.ocultar and r.domain == domain and r.node is not None}
        for fcn in {n.parent for n in hidden}:
            if fcn is not None and all(c in hidden for c in fcn.comps.values()):
                hidden.add(fcn)
        return hidden

    def is_hidden(self, on: ObjName) -> bool:
        if on is None or on.scope != "dom":
            return False
        return any(on.item == n.path or on.item.startswith(n.path + "$") for n in self._hidden_nodes(on.domain))

    def var_type_spec(self, node) -> bytes:
        """typeSpecification do nó sem os componentes ocultos (RCB com 'ocultar')."""
        hidden = self._hidden_nodes(node.domain)
        if not hidden:
            return node.mt.type_spec()

        def strip(n):
            if n.is_leaf or not any(h is n or self._under(h, n) for h in hidden):
                return n.mt
            if n.mt.kind == "array":
                return n.mt
            from .mms.types import MmsType
            return MmsType("struct", comps=[(c.name, strip(c)) for c in n.comps.values() if c not in hidden])

        return strip(node).type_spec()

    @staticmethod
    def _under(n, anc) -> bool:
        p = n.parent
        while p is not None:
            if p is anc:
                return True
            p = p.parent
        return False

    def namelist(self, assoc, cls, scope, domain):
        if cls == 9:
            return sorted(self.domains) if scope == "vmd" else []
        if scope == "dom":
            d = self.domains.get(domain)
            if d is None:
                return None
            if cls == 0:
                hidden = self._hidden_nodes(domain)
                if not hidden:
                    return d.var_names()
                paths = tuple(n.path for n in hidden)
                return [x for x in d.var_names()
                        if not any(x == hp or x.startswith(hp + "$") for hp in paths)]
            if cls == 2:
                return sorted(d.datasets)
            return []
        if scope == "aa" and cls == 2:
            return sorted(assoc.nvls)
        return []

    def mms_read(self, assoc, vs) -> bytes:
        from .mms.pdu import access_failure
        if vs.name is None or vs.alt is not None:
            return access_failure(ERR_ACCESS_UNSUPPORTED)
        ctl = self.control.match(vs.name)
        if ctl is not None and ctl[1] == "SBO" and not ctl[2]:
            return self.control.read_sbo(assoc, ctl[0])
        node = self.lookup(vs.name)
        if node is None:
            return access_failure(ERR_NON_EXISTENT)
        return node.encode()

    def mms_write(self, assoc, on: ObjName, data: tuple) -> int | None:
        ctl = self.control.match(on)
        if ctl is not None:
            return self.control.write(assoc, ctl[0], ctl[1], ctl[2], data)
        node = self.lookup(on)
        if node is None:
            self.mms_event(assoc, "Write", f"{on.text()} inexistente", f"erro {ERR_NON_EXISTENT}")
            return ERR_NON_EXISTENT
        try:
            raw = dec_data_raw(*data)
        except Exception:  # noqa: BLE001
            return 7
        try:
            if node.handler is not None:
                rcb = node.handler
                val = coerce(node.mt, raw)
                try:
                    rcb.write(assoc, node.name, val)
                except AccessError as e:
                    self.rcb_write_denied(assoc, rcb, node.name, e)
                    raise
                self.mms_event(assoc, "Write", f"{rcb.ref}.{node.name} = {raw_to_json(raw)}", "ok")
                self.emit("rcb.changed", rcb.state())
                return None
            if node.comps and any(c.handler is not None for c in node.comps.values()):
                if raw[0] != "struct" or len(raw[1]) != len(node.comps):
                    raise AccessError(7, "estrutura de RCB incompatível")
                for c, cv in zip(node.comps.values(), raw[1]):
                    if c.name in ("ConfRev", "SqNum", "TimeOfEntry", "Owner"):
                        continue
                    try:
                        c.handler.write(assoc, c.name, coerce(c.mt, cv))
                    except AccessError as e:
                        self.rcb_write_denied(assoc, c.handler, c.name, e)
                        raise
                self.mms_event(assoc, "Write", f"{on.text()} (RCB completo)", "ok")
                return None
            p = self.leaf_point.get((on.domain, on.item))
            if p is not None and p.writable and node.is_leaf:
                val = coerce(node.mt, raw)
                v = val.val if isinstance(val, Bits) else val
                self.set_points([{"id": p.id, "valor": v}], origin="mms")
                self.mms_event(assoc, "Write", f"{on.text()} = {raw_to_json(raw)}", "ok (ponto escrevível)")
                return None
            self.mms_event(assoc, "Write", f"{on.text()} = {raw_to_json(raw)}", f"erro {ERR_ACCESS_DENIED}")
            return ERR_ACCESS_DENIED
        except AccessError as e:
            self.mms_event(assoc, "Write", f"{on.text()} = {raw_to_json(raw)}: {e}", f"erro {e.code}")
            return e.code
        except PointError as e:
            self.mms_event(assoc, "Write", f"{on.text()}: {e}", "erro 11")
            return 11

    def define_nvl(self, assoc, on: ObjName, names: list):
        if on.scope == "dom":
            d = self.domains.get(on.domain)
            if d is None:
                raise ServiceError("definition", 1)
            if on.item in d.datasets:
                raise ServiceError("definition", 5, "DataSet já existe")
        elif on.scope == "aa":
            if on.item in assoc.nvls:
                raise ServiceError("definition", 5, "DataSet já existe")
        else:
            raise ServiceError("access", 1)
        mx = self.profile.get("max_dyn_datasets") or 0
        ndyn = sum(1 for ds in self.all_datasets() if getattr(ds, "dynamic", False)) + len(assoc.nvls)
        if mx and ndyn >= mx:
            raise ServiceError("resource", 4, "limite de DataSets dinâmicos")
        members = []
        for n in names:
            node = self.lookup(n)
            if node is None:
                raise ServiceError("access", 2, f"{n.text()} inexistente")
            members.append(Member(n.domain, n.item, n.item.split("$")[1] if "$" in n.item else "", node))
        ln, _, nm = on.item.rpartition("$")
        ds = DataSet(on.domain, ln or "LLN0", nm, members, desc="dinâmico (MMS)", extra=True)
        ds.dynamic = True
        if on.scope == "dom":
            self.domains[on.domain].datasets[on.item] = ds
            self.domains[on.domain]._names = None
            self.engine.rebuild_index()
            link_points_to_datasets(self.points, self.all_datasets())
        else:
            assoc.nvls[on.item] = ds

    def delete_nvl(self, assoc, names: list) -> tuple[int, int]:
        matched = deleted = 0
        for on in names:
            ds = self.find_nvl(assoc, on)
            if ds is None:
                continue
            matched += 1
            if not getattr(ds, "dynamic", False):
                continue
            if any(r.datset == ds.ref and r.ena for r in self.engine.rcbs):
                continue
            if on.scope == "dom":
                del self.domains[on.domain].datasets[on.item]
            else:
                del assoc.nvls[on.item]
            deleted += 1
        self.engine.rebuild_index()
        return matched, deleted

    # ------------------------------------------------------------------ pontos
    def find_point(self, key) -> Point:
        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            i = int(key)
            if 0 <= i < len(self.points):
                return self.points[i]
        elif isinstance(key, str):
            p = self.by_path.get(key)
            if p is not None:
                return p
            for p in self.points:  # aceita caminho sem o domínio
                if p.ln_path == key or p.path.endswith("/" + key) or p.path.endswith(key):
                    return p
        raise PointError(f"ponto inexistente: {key}", str(key), "inexistente", 404)

    def _convert(self, p: Point, value) -> dict:
        if p.kind == "ro" or p.v is None:
            raise PointError("ponto somente leitura (CDC não tratado)", p.path, "somente_leitura", 409)
        try:
            if p.cdc in ("DPS", "DPC"):
                if isinstance(value, str) and not value.isdigit():
                    if value.lower() not in DPS_TXT:
                        raise ValueError(value)
                    iv = DPS_TXT[value.lower()]
                else:
                    iv = int(value)
                if not 0 <= iv <= 3:
                    raise ValueError(value)
                return {p.v: Bits(2, iv)}
            if p.kind == "dig":
                return {p.v: _to_bool(value)}
            if p.cdc == "CMV":
                if isinstance(value, dict):
                    mag, ang = float(value.get("mag", 0)), float(value.get("ang", 0))
                elif isinstance(value, (list, tuple)):
                    mag, ang = float(value[0]), float(value[1])
                else:
                    mag, ang = float(value), float(p.a.value if p.a is not None else 0.0)
                out = {}
                self._set_num(out, p.v, p.vi, mag, p)
                if p.a is not None:
                    self._set_num(out, p.a, p.ai, ang, p)
                return out
            if p.cdc == "MV":
                out = {}
                self._set_num(out, p.v, p.vi, float(value), p)
                return out
            # INS, INC, ENS, ENC, BCR...
            if isinstance(value, str) and p.v.enum and not value.lstrip("-").isdigit():
                for o, txt in p.v.enum.items():
                    if txt == value:
                        return {p.v: o}
                raise ValueError(value)
            fv = float(value)
            if fv != int(fv):
                raise ValueError(value)
            iv = int(fv)
            k = p.v.mt.kind
            if k == "uint" and iv < 0:
                raise ValueError("negativo em tipo sem sinal")
            bits = p.v.mt.size or 32
            lim = (1 << bits) if k == "uint" else (1 << (bits - 1))
            if (k == "uint" and iv >= lim) or (k == "int" and not -lim <= iv < lim):
                raise ValueError("fora do tamanho do tipo")
            if k == "float":
                return {p.v: float(iv)}
            return {p.v: iv}
        except (ValueError, TypeError, KeyError) as e:
            raise PointError(f"valor inválido para {p.cdc}: {value!r} ({e})", p.path) from e

    def _set_num(self, out: dict, leaf_f, leaf_i, val: float, p: Point):
        if math.isnan(val) or math.isinf(val):
            raise ValueError("NaN/inf")
        if leaf_f is not None and leaf_f.mt.kind == "float":
            out[leaf_f] = val
            if leaf_i is not None:
                if p.svc and p.svc[0]:
                    out[leaf_i] = int(round((val - p.svc[1]) / p.svc[0]))
                else:
                    out[leaf_i] = int(round(val))
        elif leaf_f is not None:
            out[leaf_f] = int(round(val))

    @staticmethod
    def _reason(leaf, default: str) -> str:
        if leaf.trg and default not in leaf.trg:
            for r in ("dchg", "qchg", "dupd"):
                if r in leaf.trg:
                    return r
        return default

    def _apply(self, p: Point, value=_NOVAL, quality=None, t: UtcTime | None = None, origin: str = "manual",
               keep_t: bool = False, force_range: bool = False, changes: list | None = None) -> bool:
        new_vals = {}
        if value is not _NOVAL and value is not None:
            new_vals = self._convert(p, value)
            if p.kind == "ana" and p.rng and p.cdc in ("MV", "CMV") and not force_range:
                main = new_vals.get(p.v)
                lo, hi = p.rng
                if main is not None and not lo <= float(main) <= hi:
                    raise PointError(f"valor {main} fora da faixa [{lo}, {hi}] do CID (RN-ANA-03); "
                                     f"confirme a exceção", p.path, "fora_da_faixa", 422)
            if p.kind == "ana" and p.zero_db and p.rng and p.cdc in ("MV", "CMV"):
                thr0 = p.zero_db / 100000.0 * (p.rng[1] - p.rng[0])
                if abs(float(new_vals.get(p.v, 0))) <= thr0:
                    for leaf in list(new_vals):
                        if leaf in (p.v, p.vi):
                            new_vals[leaf] = 0.0 if leaf.mt.kind == "float" else 0
        v_changed = [leaf for leaf, val in new_vals.items() if leaf.value != val]
        q_new = None
        if quality is not None:
            if p.q is None:
                raise PointError("ponto sem atributo de qualidade", p.path)
            try:
                q_new = quality if isinstance(quality, Bits) else q_from_dict(quality, p.q.value)
            except ValueError as e:
                raise PointError(str(e), p.path) from e
            if q_new == p.q.value:
                q_new = None
        if not v_changed and q_new is None:
            return False  # RN-DIG-03: mesmo valor não gera evento nem altera t
        cid = self.mgr.storage.new_change_id()
        for leaf in v_changed:
            leaf.value = new_vals[leaf]
        if q_new is not None:
            p.q.value = q_new
        if p.t is not None and (t is not None or not keep_t):
            p.t.value = t if t is not None else self.clock.stamp()
        out = changes if changes is not None else []
        suppressed = False
        if v_changed and p.kind == "ana" and p.cdc in ("MV", "CMV") and p.db and p.rng:
            cur = float(p.v.value)
            thr = p.db / 100000.0 * (p.rng[1] - p.rng[0])
            if p.last_reported is not None and abs(cur - p.last_reported) <= thr:
                suppressed = True
        if v_changed and not suppressed:
            if p.kind == "ana" and p.cdc in ("MV", "CMV"):
                p.last_reported = float(p.v.value)
            for leaf in v_changed:
                out.append((p.domain, leaf.path, self._reason(leaf, "dchg"), cid))
        if q_new is not None:
            out.append((p.domain, p.q.path, self._reason(p.q, "qchg"), cid))
        p.last_change = time.time()
        self.stats["mudancas"] += 1
        rec = {"id": cid, "ied_id": self.id, "ts": time.time(), "path": p.path, "valor": p.value(),
               "qualidade": q_to_dict(p.quality()), "t": p.t.value.iso() if p.t is not None else "",
               "origem": origin + (" (zona morta: sem dchg)" if suppressed else "")}
        self.mgr.storage.queue_change(rec)
        self._persist_point(p)
        self.emit("point.changed", dict(self.point_json(p), mudanca=cid, origem=rec["origem"]))
        self.logic.kick()
        if changes is None:
            self.engine.notify(out)
        return True

    def set_points(self, ops: list[dict], origin: str = "manual") -> list[dict]:
        """Altera vários pontos numa mesma operação (RN-DIG-09); todas as validações antes de aplicar."""
        plan = []
        for op in ops:
            key = op.get("id", op.get("path", op.get("ponto")))
            p = self.find_point(key)
            if "modo" in op and op["modo"] not in MODES:
                raise PointError(f"modo inválido: {op['modo']}", p.path)
            mode = op.get("modo", p.mode)
            if op.get("liberar"):
                mode = p.prev_mode if p.mode == "forcado" else p.mode
            if op.get("vinculo") is not None:
                link = op["vinculo"]
                if link and not link.get("ip"):
                    raise PointError("vínculo exige o IP do relé real", p.path)
            else:
                link = p.link
            if mode in ("espelhado", "invertido") and not link:
                raise PointError("modo espelhado exige vínculo configurado (RN-DIG-04)", p.path, "sem_vinculo", 409)
            if "valor" in op and op["valor"] is not None and origin in ("manual", "cenario", "script", "mms") \
                    and mode in ("espelhado", "invertido"):
                raise PointError("ponto espelhado: a chave está bloqueada; passe para 'forçado' (RN-UI-02)",
                                 p.path, "espelhado", 409)
            if "valor" in op and op["valor"] is not None:
                self._convert(p, op["valor"])
            if op.get("qualidade") is not None:
                if p.q is None:
                    raise PointError("ponto sem qualidade", p.path)
                try:
                    q_from_dict(op["qualidade"], p.q.value)
                except ValueError as e:
                    raise PointError(str(e), p.path) from e
            plan.append((p, op, mode, link))
        changes: list = []
        results = []
        for p, op, mode, link in plan:
            meta_changed = False
            if mode != p.mode:
                if mode == "forcado":
                    p.prev_mode = p.mode
                p.mode = mode
                meta_changed = True
            if op.get("vinculo") is not None:
                p.link = link or None
                meta_changed = True
            if "escrevivel" in op:
                p.writable = bool(op["escrevivel"])
                meta_changed = True
            changed = self._apply(p, op.get("valor", _NOVAL), op.get("qualidade"), None, origin,
                                  bool(op.get("manter_t")), bool(op.get("confirmar_fora_faixa")), changes)
            if meta_changed:
                self._persist_point(p)
                if not changed:
                    self.emit("point.changed", self.point_json(p))
            results.append({"id": p.id, "path": p.path, "alterado": changed})
        if any("vinculo" in op or "modo" in op or op.get("liberar") for _, op, _, _ in plan):
            self.mirror.refresh()
            for p, op, _, _ in plan:
                if op.get("liberar") or ("modo" in op and op["modo"] in ("espelhado", "invertido")):
                    self.mirror.resync_point(p)
        self.engine.notify(changes)
        return results

    def pulse(self, pid, ms: int = 500, ciclos: int = 1, periodo_ms: int | None = None, origin="manual"):
        p = self.find_point(pid)
        if p.kind != "dig":
            raise PointError("pulso/oscilação só para pontos digitais", p.path)
        if p.mode in ("espelhado", "invertido"):
            raise PointError("ponto espelhado: passe para 'forçado'", p.path, "espelhado", 409)
        if ms <= 0 or ciclos <= 0 or ciclos > 10000:
            raise PointError("parâmetros de pulso inválidos", p.path)

        def other(v):
            if p.cdc in ("DPS", "DPC"):
                return 1 if v == 2 else 2
            return not v

        async def run():
            base = p.value()
            half = (periodo_ms / 2.0 if periodo_ms else ms) / 1000.0
            for i in range(ciclos):
                self.set_points([{"id": p.id, "valor": other(base)}], origin=f"{origin} (pulso {i + 1}/{ciclos})")
                await asyncio.sleep(half if periodo_ms else ms / 1000.0)
                self.set_points([{"id": p.id, "valor": base}], origin=f"{origin} (pulso {i + 1}/{ciclos})")
                if i < ciclos - 1:
                    await asyncio.sleep(half)

        self._spawn(run())

    def _spawn(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def apply_mirror(self, p: Point, value, q: Bits | None, t: UtcTime | None, lost: bool = False):
        """Valor vindo do relé real (RN-DIG-05 a RN-DIG-07)."""
        if p.mode == "forcado" or p.mode == "manual":
            return
        link = p.link or {}
        if lost:
            if link.get("invalida_na_perda", True) and p.q is not None:
                self._apply(p, _NOVAL, {"validity": link.get("qualidade_na_perda", "invalid")}, None,
                            "espelho (perda de comunicação)")
            return
        if p.mode == "invertido" and value is not None:
            if p.cdc in ("DPS", "DPC"):
                value = {1: 2, 2: 1}.get(value, value)
            elif p.kind == "dig":
                value = not value
        quality = q if (link.get("replica_q") and q is not None) else None
        if quality is None and p.q is not None and link.get("invalida_na_perda", True) and \
                (p.q.value.get(0) or p.q.value.get(1)) and not link.get("replica_q"):
            quality = {"validity": "good"}
        ts = t if (link.get("replica_t", True) and t is not None) else None
        try:
            self._apply(p, value, quality, ts, "espelho", force_range=True)
        except PointError as e:
            log.warning("espelho %s: %s", p.path, e)

    # ------------------------------------------------------------------ eventos e registros
    def emit(self, ev: str, data):
        self.mgr.broadcast({"evento": ev, "ied": self.id, "dados": data})

    def capture(self, assoc: Association, direction: str, frame: bytes):
        self.frames.append((time.time(), direction, assoc.id, assoc.peer, assoc.local, frame))

    def mms_event(self, assoc, servico: str, detalhe: str, resultado: str):
        m = {"ied_id": self.id, "ts": time.time(), "cliente": assoc.peer_str if assoc else "",
             "servico": servico, "detalhe": detalhe, "resultado": resultado}
        self.mgr.storage.queue_mms(m)
        self.emit("mms", m)

    def log_report(self, rcb: Rcb, e, assoc, notes: list, dropped: bool = False):
        rid = self.mgr.storage.new_report_id()
        f = self.faults
        rec = {"id": rid, "ied_id": self.id, "ts": time.time(), "rcb": rcb.ref,
               "rptid": f.p("rptid", "valor") if f.on("rptid") else rcb.rptid,
               "entry_id": e.eid.hex() if rcb.buffered else "", "sqnum": e.sqnum, "tipo": e.kind,
               "cliente": assoc.peer_str if assoc else "(sem cliente)",
               "pontos": decode_included(e, rcb.dataset), "pdu": b"".join(e.pdus).hex(),
               "notas": notes, "change_ids": e.change_ids, "descartado": dropped}
        self.mgr.storage.queue_report(rec)
        if not dropped:
            self.stats["reports"] += 1
        self.emit("report.sent", {k: v for k, v in rec.items() if k != "pdu"} | {"pdu_len": len(rec["pdu"]) // 2})

    def rcb_write_denied(self, assoc, rcb: Rcb, attr: str, e: AccessError):
        """Escrita recusada num RCB: histórico (IED, RCB, atributo, código, IP, horário) e evento rcb.write_denied."""
        rcb.denied += 1
        ip = assoc.peer[0] if getattr(assoc, "peer", None) else ""
        rec = {"ied_id": self.id, "ts": time.time(), "rcb": rcb.ref, "base": rcb.base, "atributo": attr,
               "codigo": e.code, "ip": ip, "cliente": assoc.peer_str if assoc else "",
               "politica": rcb.policy.for_client(ip or None), "motivo": str(e)}
        self.mgr.storage.queue_rcb_denied(rec)
        self.emit("rcb.write_denied", dict(rec, ied_nome=self.name, recusadas=rcb.denied))

    def rcbs_named(self, nome: str) -> list[Rcb]:
        """RCBs pelo nome-base (todas as instâncias), pelo nome da instância ou pela referência completa."""
        out = [r for r in self.engine.rcbs if r.base == nome]
        return out or [r for r in self.engine.rcbs if nome in (r.name, r.ref)]

    def log_buffered(self, rcb: Rcb, e):
        self.log_report(rcb, e, None, ["guardado no buffer do BRCB (sem cliente habilitado)"], dropped=True)

    def session_opened(self, a: Association):
        self.stats["associacoes_total"] += 1
        self.mms_event(a, "Associate", f"associação aceita (max PDU {a.max_pdu}, TPDU {a.tpdu})", "ok")
        self.emit("session.opened", self.session_json(a))
        self.mgr.netmon.app(self, a, "assoc", "ok", f"associação MMS aceita pelo IED {self.name}")

    def session_closed(self, a: Association):
        self.engine.association_closed(a)
        self.control.association_closed(a)
        self.mms_event(a, "Disconnect", a.close_reason or "encerrada", "fechada")
        self.emit("session.closed", dict(self.session_json(a), motivo=a.close_reason))
        self.mgr.netmon.app(self, a, "fim", "info", f"associação encerrada: {a.close_reason or 'encerrada'}")

    def session_json(self, a: Association) -> dict:
        return {"id": a.id, "cliente": a.peer_str, "local": f"{a.local[0]}:{a.local[1]}", "inicio": a.started,
                "max_pdu": a.max_pdu, "requisicoes": a.requests,
                "rcbs_habilitados": [r.ref for r in self.engine.rcbs if r.owner is a],
                "rcbs_reservados": [r.ref for r in self.engine.rcbs if r.reserved_by is a]}

    # ------------------------------------------------------------------ execução
    async def start(self, admin_host: str, local_test: bool):
        if self.state == "rodando":
            return
        ip = self.cfg.get("ip") or ""
        port = int(self.cfg.get("porta") or 102)
        if not ip:
            raise PointError("escolha a interface e o IP do IED antes de iniciar (RN-NET-13)")
        it = netif.iface_of(ip)
        if it is None:
            raise PointError(f"o IP {ip} não pertence a nenhuma interface da máquina (RN-NET-03)")
        if it["loopback"] and not local_test:
            raise PointError("loopback só é permitido com a opção de teste local (RN-NET-14)")
        if not it["ativa"]:
            raise PointError(f"interface {it['nome']} inativa (RN-NET-13)")
        if ip and ip == self.cfg.get("ip_cid_real"):
            raise PointError(f"{ip} é o IP do relé real no CID; o simulador nunca o usa (RN-NET-03)")
        if ip == admin_host and admin_host not in ("127.0.0.1", "::1", "localhost"):
            raise PointError("o IP de administração não pode ser usado por um IED (RN-NET-08)")
        for other in self.mgr.ieds.values():
            if other is not self and other.state == "rodando" and other.cfg.get("ip") == ip and \
                    int(other.cfg.get("porta") or 102) == port:
                raise PointError(f"{ip}:{port} já está em uso pelo IED '{other.name}' (RN-NET-04)", status=409)
        self.server = MmsServer(self)
        self.server.services = services_list(self.profile)
        try:
            await self.server.start(ip, port)
        except PermissionError as e:
            self.server = None
            raise PointError(f"sem permissão para escutar em {ip}:{port}. A porta 102 exige root ou "
                             f"CAP_NET_BIND_SERVICE (RN-NET-05): sudo setcap cap_net_bind_service=+ep "
                             f"$(readlink -f .venv/bin/python)") from e
        except OSError as e:
            self.server = None
            raise PointError(f"não foi possível escutar em {ip}:{port}: {e.strerror or e}", status=409) from e
        self.cfg["interface"] = it["nome"]
        self.state = "rodando"
        self.error = ""
        self.started_at = time.time()
        self.snapshot = {p.path: (p.value(), p.q.value if p.q else None, p.t.value if p.t else None)
                         for p in self.points if p.kind != "ro"}
        self.mirror.refresh()
        self.mirror.start()
        self.emit("ied.state", self.summary())
        log.info("IED %s escutando em %s:%s", self.name, ip, port)

    async def stop(self, reason: str = "IED parado", error: str = ""):
        self.mirror.stop()
        if self.server is not None:
            await self.server.stop(reason)
            self.server = None
        self.engine.stop()
        for t in list(self.tasks):
            t.cancel()
        self.state = "erro" if error else "parado"
        self.error = error
        self.emit("ied.state", self.summary())

    def restore_snapshot(self):
        """RN-SEG-05: devolve os pontos ao estado anterior ao início."""
        if not self.snapshot:
            return 0
        n = 0
        for path, (v, q, t) in self.snapshot.items():
            p = self.by_path.get(path)
            if p is None:
                continue
            for leaf, val in self._convert(p, v).items():
                leaf.value = val
            if p.q is not None and q is not None:
                p.q.value = q
            if p.t is not None and t is not None:
                p.t.value = t
            self._persist_point(p)
            n += 1
        self.emit("points.reload", {})
        return n

    # ------------------------------------------------------------------ JSON
    def summary(self) -> dict:
        assocs = self.server.associations() if self.server else []
        return {
            "id": self.id, "nome": self.name, "nome_ied": self.ied_name, "ied_scl": self.scl_ied.name,
            "cid": self.cfg.get("cid_nome", self.cfg["cid_arquivo"]), "edicao": self.scl.edition_label,
            "perfil": self.cfg.get("perfil"), "ip": self.cfg.get("ip", ""), "porta": int(self.cfg.get("porta") or 102),
            "interface": self.cfg.get("interface", ""), "estado": self.state, "erro": self.error,
            "associacoes": len(assocs), "max_associacoes": int(self.cfg.get("max_associacoes") or
                                                                self.profile["limites"]["max_associacoes"]),
            "reports_enviados": self.stats["reports"], "mudancas": self.stats["mudancas"],
            "pontos": len(self.points), "falhas_ativas": self.faults.active(),
            "fabricante": self.scl_ied.manufacturer, "tipo": self.scl_ied.type,
            "ip_cid": self.scl_ied.address.get("IP", ""), "iniciado_em": self.started_at,
            "ld": sorted(self.domains),
        }

    def detail(self) -> dict:
        d = self.summary()
        d.update({
            "erros_importacao": self.errors, "avisos": self.warnings, "relogio": self.clock.to_json(),
            "sessoes": [self.session_json(a) for a in (self.server.associations() if self.server else [])],
            "sage_hosts": self.sage_hosts_line(),
            "perfil_efetivo": self.profile, "servicos_cid": self.scl_ied.services,
            "config": {k: v for k, v in self.cfg.items() if k not in ("falhas",)},
            "vinculos": self.mirror.state(),
        })
        return d

    def sage_hosts_line(self) -> str:
        ip = self.cfg.get("ip") or "<IP-do-simulador>"
        return f"{ip}\t{self.ied_name}\t# host_mms_<ENM> – simulador ({self.name})"

    def point_json(self, p: Point) -> dict:
        t = p.timestamp()
        q = p.quality()
        return {
            "id": p.id, "path": p.path, "ld": p.domain, "ln": p.ln, "do": p.do.replace("$", "."), "fc": p.fc,
            "cdc": p.cdc, "tipo": p.kind, "desc": p.desc, "valor": p.value(),
            "valor_i": p.vi.value if p.vi is not None else None,
            "qualidade": q_to_dict(q), "t": t.iso() if t else "", "t_qualidade": f"0x{t.tq:02x}" if t else "",
            "modo": p.mode, "modo_anterior": p.prev_mode, "vinculo": p.link,
            "datasets": [{"ds": d, "pos": i} for d, i in p.datasets], "unidade": p.units,
            "faixa": list(p.rng) if p.rng else None, "db": p.db, "zeroDb": p.zero_db,
            "sVC": list(p.svc) if p.svc else None, "escrevivel": p.writable,
            "enum": p.v.enum if p.v is not None and p.v.enum else None, "ultima_mudanca": p.last_change,
        }
