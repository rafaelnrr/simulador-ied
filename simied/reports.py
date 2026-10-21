"""Motor de reports: BRCB/URCB conforme IEC 61850-7-2 / 8-1 (seção 7, RN-RCB-01 a RN-RCB-13)."""
from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from dataclasses import dataclass, field

from .ber import bool_tlv, str_tlv, tlv, uint_tlv
from .model import DataSet, Node
from .mms.pdu import info_report
from .mms.types import (ERR_ACCESS_DENIED, ERR_ATTR_INCONSISTENT, ERR_TEMP_UNAVAILABLE, ERR_VALUE_INVALID,
                        AccessError, Bits, MmsType, btime6, dec_data, enc_data, raw_to_json)
from .rcbpolicy import RcbPolicy
from .scl import SclRcb

OPT_NAMES = ["reserved", "seqnum", "timestamp", "reasoncode", "datset", "dataref", "bufovfl", "entryid",
             "confrev", "segmentation"]
TRG_NAMES = ["reserved", "dchg", "qchg", "dupd", "period", "gi"]
REASON_BIT = {"dchg": 1, "qchg": 2, "dupd": 3, "period": 4, "gi": 5}
SCL_OPT = {"seqNum": 1, "timeStamp": 2, "reasonCode": 3, "dataSet": 4, "dataRef": 5, "bufOvfl": 6,
           "entryID": 7, "configRef": 8, "segmentation": 9}
SCL_TRG = {"dchg": 1, "qchg": 2, "dupd": 3, "period": 4, "gi": 5}

_entry_ids = itertools.count(1)


def bits_names(b: Bits, names: list[str]) -> list[str]:
    return [n for i, n in enumerate(names) if i < b.n and b.get(i) and n != "reserved"]


def bits_from_names(n: int, names_all: list[str], names: list[str]) -> Bits:
    return Bits.from_list(n, [names_all.index(x) for x in names if x in names_all])


def encode_node(node: Node, zero_t: bool = False) -> bytes:
    if not zero_t:
        return node.encode()
    if node.is_leaf:
        if node.name == "t" and node.mt.kind == "utc":
            return enc_data(node.mt, None)
        return node.encode()
    inner = b"".join(encode_node(c, True) for c in node.comps.values())
    return tlv(0xA1 if node.mt.kind == "array" else 0xA2, inner)


@dataclass
class Entry:
    seq: int
    eid: bytes
    toe: bytes
    pdus: list
    included: list  # [(idx, reasonbits, data)]
    kind: str
    change_ids: list = field(default_factory=list)
    ts: float = 0.0
    sqnum: int = 0


class Rcb:
    def __init__(self, engine: "ReportEngine", domain: str, ln: str, name: str, cfg: SclRcb, extra: bool = False):
        self.engine = engine
        self.domain = domain
        self.ln = ln
        self.name = name
        self.base = cfg.name
        self.cfg = cfg
        self.extra = extra
        self.buffered = cfg.buffered
        self.fc = "BR" if self.buffered else "RP"
        ed = engine.edition
        self.attr_names = (["RptID", "RptEna", "DatSet", "ConfRev", "OptFlds", "BufTm", "SqNum", "TrgOps",
                            "IntgPd", "GI", "PurgeBuf", "EntryID", "TimeOfEntry"] + (["ResvTms", "Owner"] if ed >= 2 else [])
                           if self.buffered else
                           ["RptID", "RptEna", "Resv", "DatSet", "ConfRev", "OptFlds", "BufTm", "SqNum", "TrgOps",
                            "IntgPd", "GI"] + (["Owner"] if ed >= 2 else []))
        self.types = {
            "RptID": MmsType("vis", -65 if ed < 2 else -129), "RptEna": MmsType("bool"), "Resv": MmsType("bool"),
            "DatSet": MmsType("vis", -65 if ed < 2 else -129), "ConfRev": MmsType("uint", 32),
            "OptFlds": MmsType("bits", 10), "BufTm": MmsType("uint", 32),
            "SqNum": MmsType("uint", 16 if self.buffered else 8), "TrgOps": MmsType("bits", 6),
            "IntgPd": MmsType("uint", 32), "GI": MmsType("bool"), "PurgeBuf": MmsType("bool"),
            "EntryID": MmsType("octet", 8), "TimeOfEntry": MmsType("btime", 6), "ResvTms": MmsType("int", 16),
            "Owner": MmsType("octet", -64),
        }
        self.rptid_cid = cfg.rptid
        self.rptid = cfg.rptid or self.obj_ref
        self.ena = False
        self.datset = f"{domain}/{ln}${cfg.datset}" if cfg.datset else ""
        self.confrev = cfg.conf_rev
        self.optflds = Bits.from_list(10, [b for k, b in SCL_OPT.items() if cfg.optflds.get(k)])
        self.buftm = cfg.buf_time
        self.sqnum = 0
        self.trgops = Bits.from_list(6, [b for k, b in SCL_TRG.items() if cfg.trgops.get(k)])
        self.intgpd = cfg.intg_pd
        self.gi_req = False
        self.resvtms = 0
        self.owner_bytes = b""
        self.owner = None  # associação que habilitou
        self.reserved_by = None
        self.pending: dict[int, list] = {}
        self.pending_batch = 0
        self.timer: asyncio.TimerHandle | None = None
        self.intg_task: asyncio.Task | None = None
        self.buffer: deque[Entry] = deque()
        # EntryID sempre crescente, inclusive entre reinícios do simulador e recargas do modelo: começa no
        # instante da carga (ms << 16). Recomeçar do 1 fazia o SAGE, que guarda o último EntryID recebido e o
        # escreve antes do RptEna, descartar os reports novos como repetidos.
        self.entry_base = int(time.time() * 1000) << 16
        self.entry_seq = self.entry_base
        self.last_sent_seq = 0
        self.resync_eid: bytes | None = None
        self.bufovfl = False
        self.was_enabled = False
        self.last_eid = bytes(8)
        self.last_toe = bytes(6)
        self.reports_sent = 0
        self.node: Node | None = None
        self.policy = RcbPolicy()
        self.denied = 0  # escritas recusadas desde a carga do modelo

    # ------------------------------------------------------------------
    @property
    def ref(self) -> str:
        return f"{self.domain}/{self.ln}${self.fc}${self.name}"

    @property
    def obj_ref(self) -> str:
        return f"{self.domain}/{self.ln}${self.fc}${self.name}"

    @property
    def dataset(self) -> DataSet | None:
        return self.engine.ied.dataset_by_ref(self.datset) if self.datset else None

    def eff_trgops(self) -> Bits:
        allowed = self.engine.profile["rcb"]["trgops_permitidos"]
        return Bits.from_list(6, [i for i in range(1, 6) if self.trgops.get(i) and TRG_NAMES[i] in allowed])

    def build_node(self, parent: Node) -> Node:
        n = Node(self.name, parent)
        n.comps = {}
        for a in self.attr_names:
            leaf = Node(a, n)
            leaf.mt = self.types[a]
            leaf.handler = self
            n.comps[a] = leaf
        n.finalize()
        self.node = n
        return n

    def read(self, leaf: Node):
        a = leaf.name
        return {
            "RptID": lambda: self.rptid, "RptEna": lambda: self.ena,
            "Resv": lambda: self.reserved_by is not None, "DatSet": lambda: self.datset,
            "ConfRev": lambda: self.confrev, "OptFlds": lambda: self.optflds, "BufTm": lambda: self.buftm,
            "SqNum": lambda: self.sqnum & (0xFFFF if self.buffered else 0xFF), "TrgOps": self.eff_trgops,
            "IntgPd": lambda: self.intgpd, "GI": lambda: False, "PurgeBuf": lambda: False,
            "EntryID": lambda: self.last_eid, "TimeOfEntry": lambda: self.last_toe,
            "ResvTms": lambda: self.resvtms, "Owner": lambda: self.owner_bytes,
        }[a]()

    # ------------------------------------------------------------------
    def write(self, assoc, attr: str, value):
        prof = self.engine.profile["rcb"]
        f = self.engine.ied.faults
        if f.on("erro_escrita") and f.p("erro_escrita", "atributo") == attr and \
                f.p("erro_escrita", "rcb") in ("", self.name, self.base):
            raise AccessError(int(f.p("erro_escrita", "codigo")), "falha injetada RN-FLT-06")
        self.policy.check(assoc.peer[0] if getattr(assoc, "peer", None) else None, attr, value)
        if self.ena and self.owner is not None and self.owner is not assoc:
            raise AccessError(ERR_TEMP_UNAVAILABLE, "RCB habilitado por outro cliente")
        if self.reserved_by is not None and self.reserved_by is not assoc:
            raise AccessError(int(prof["erro_reservado"]), "RCB reservado por outro cliente")
        if attr in ("ConfRev", "SqNum", "TimeOfEntry", "Owner"):
            raise AccessError(ERR_ACCESS_DENIED, f"{attr} é somente leitura")
        if attr == "RptEna":
            if value:
                if self.dataset is None:
                    raise AccessError(ERR_ATTR_INCONSISTENT, "RptEna=true sem DatSet válido")
                self._reserve(assoc)
                if not self.ena:
                    self.enable(assoc)
            else:
                if self.ena:
                    self.disable("RptEna=false")
            return
        if attr == "GI":
            if value:
                if self.ena:
                    self.flush()
                    self.emit_all("gi")
                else:
                    self.gi_req = True
            return
        if attr == "Resv":
            if value:
                self.reserved_by = assoc
            else:
                if self.ena:
                    raise AccessError(ERR_TEMP_UNAVAILABLE, "RCB habilitado")
                self.reserved_by = None
            return
        if attr == "PurgeBuf":
            if self.ena:
                raise AccessError(int(prof["erro_habilitado"]), "PurgeBuf com RCB habilitado")
            if value:
                self.buffer.clear()
                self.last_sent_seq = self.entry_seq
            return
        if attr == "EntryID":
            if self.ena:
                raise AccessError(int(prof["erro_habilitado"]), "EntryID com RCB habilitado")
            self.resync_eid = bytes(value)
            self._reserve(assoc)
            return
        if attr == "ResvTms":
            if self.ena:
                raise AccessError(int(prof["erro_habilitado"]), "ResvTms com RCB habilitado")
            self.resvtms = int(value)
            if self.resvtms > 0:
                self.reserved_by = assoc
            return
        # atributos configuráveis
        if self.ena:
            raise AccessError(int(prof["erro_habilitado"]), f"{attr} com RCB habilitado")
        mode = prof["atributos"].get(attr, "Dyn")
        if mode != "Dyn":
            raise AccessError(int(prof["erro_conf"]), f"{attr} é '{mode}' no perfil/CID")
        if attr == "RptID":
            self.rptid = str(value) or self.rptid_cid or self.obj_ref
        elif attr == "DatSet":
            v = str(value)
            if v:
                ds = self.engine.ied.dataset_by_ref(v)
                if ds is None:
                    raise AccessError(ERR_VALUE_INVALID, f"DataSet '{v}' inexistente")
                v = ds.ref
            if v != self.datset:
                self.datset = v
                self.confrev += 1
                self.buffer.clear()
                self.engine.rebuild_rcb_index()
        elif attr == "OptFlds":
            self.optflds = value
        elif attr == "BufTm":
            self.buftm = int(value)
        elif attr == "TrgOps":
            self.trgops = value
        elif attr == "IntgPd":
            self.intgpd = int(value)
        self._reserve(assoc)

    def _reserve(self, assoc):
        if not self.buffered and self.engine.profile["rcb"].get("reserva_implicita", True):
            self.reserved_by = assoc

    # ------------------------------------------------------------------
    def enable(self, assoc):
        self.ena = True
        self.owner = assoc
        self.was_enabled = True
        self.owner_bytes = assoc.owner_bytes() if hasattr(assoc, "owner_bytes") else b""
        self.engine.ied.emit("rcb.changed", self.state())
        if self.buffered:
            self._replay()
        self._restart_intg()
        if self.gi_req:
            self.gi_req = False
            self.emit_all("gi")

    def disable(self, reason: str = ""):
        self.ena = False
        if not self.buffered:
            self.pending.clear()
            if self.timer:
                self.timer.cancel()
                self.timer = None
        self.owner = None
        if self.intg_task:
            self.intg_task.cancel()
            self.intg_task = None
        self.engine.ied.emit("rcb.changed", self.state())

    def association_closed(self, assoc):
        if self.owner is assoc:
            self.disable("associação encerrada")
        if self.reserved_by is assoc:
            self.reserved_by = None
            self.resvtms = 0

    def _restart_intg(self):
        if self.intg_task:
            self.intg_task.cancel()
            self.intg_task = None
        if self.ena and self.intgpd > 0 and self.eff_trgops().get(4):
            self.intg_task = asyncio.get_running_loop().create_task(self._intg_loop())

    async def _intg_loop(self):
        try:
            while self.ena:
                await asyncio.sleep(self.intgpd / 1000.0)
                if not self.ena:
                    break
                self.flush()
                self.emit_all("period")
        except asyncio.CancelledError:
            pass

    def _replay(self):
        start = self.last_sent_seq
        if self.resync_eid is not None and self.resync_eid != bytes(8):
            found = next((e for e in self.buffer if e.eid == self.resync_eid), None)
            if found is not None:
                start = found.seq
            else:
                start = 0
                self.bufovfl = True
        self.resync_eid = None
        for e in list(self.buffer):
            if e.seq > start:
                self.engine.deliver(self, e, replay=True)

    # ------------------------------------------------------------------
    def active(self) -> bool:
        if self.ena:
            return True
        return self.buffered and self.was_enabled

    def on_event(self, idx: int, rbits: int, data: bytes, cids: list, batch: int, to_flush: set):
        if not self.active():
            return
        eff = self.eff_trgops()
        rbits_eff = 0
        for b in (1, 2, 3):
            if rbits & (1 << b) and eff.get(b):
                rbits_eff |= 1 << b
        if not rbits_eff:
            return
        if idx in self.pending and self.pending_batch != batch:
            self.flush()
        self.pending_batch = batch
        if idx in self.pending:
            self.pending[idx][0] |= rbits_eff
            self.pending[idx][1] = data
            self.pending[idx][2].extend(cids)
        else:
            self.pending[idx] = [rbits_eff, data, list(cids)]
        if self.buftm <= 0:
            to_flush.add(self)
        elif self.timer is None:
            self.timer = asyncio.get_running_loop().call_later(self.buftm / 1000.0, self.flush)

    def flush(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if not self.pending:
            return
        items = sorted(self.pending.items())
        self.pending.clear()
        cids = [c for _, v in items for c in v[2]]
        self._emit([(i, v[0], v[1]) for i, v in items], "evento", cids)

    def emit_all(self, why: str):
        ds = self.dataset
        if ds is None:
            return
        zero_t = self.engine.ied.faults.on("omitir_t")
        bit = 1 << REASON_BIT[why]
        inc = [(i, bit, encode_node(m.node, zero_t)) for i, m in enumerate(ds.members)]
        self._emit(inc, "integridade" if why == "period" else "gi", [])

    def _emit(self, included: list, kind: str, cids: list):
        ds = self.dataset
        if ds is None:
            return
        ied = self.engine.ied
        f = ied.faults
        now = ied.clock.now()
        toe = btime6(now)
        if self.buffered:
            if not (f.on("duplicar") and f.p("duplicar", "modo") == "repetir_entryid" and self.entry_seq != self.entry_base):
                self.entry_seq += 1
            eid = self.entry_seq.to_bytes(8, "big")
        else:
            self.entry_seq += 1
            eid = bytes(8)
        max_pdu = self.owner.max_pdu if self.owner is not None else int(self.engine.profile["limites"]["max_pdu"])
        pdus = self._build(ds, included, eid, toe, max_pdu)
        e = Entry(self.entry_seq, eid, toe, pdus, included, kind, cids, time.time(), self.sqnum)
        self.sqnum = (self.sqnum + 1) & (0xFFFF if self.buffered else 0xFF)
        self.last_eid = eid
        self.last_toe = toe
        if self.buffered:
            self.buffer.append(e)
            limit = int(self.engine.profile["rcb"].get("buffer_max", 1000))
            while len(self.buffer) > limit:
                self.buffer.popleft()
                self.bufovfl = True
        if self.ena and self.owner is not None:
            self.engine.deliver(self, e)
        elif self.buffered:
            ied.log_buffered(self, e)

    def _build(self, ds: DataSet, included: list, eid: bytes, toe: bytes, max_pdu: int) -> list[bytes]:
        f = self.engine.ied.faults
        prof = self.engine.profile
        opt = self.optflds
        if not self.buffered:
            opt = opt.set(6, False).set(7, False)
        rptid = f.p("rptid", "valor") if f.on("rptid") else self.rptid
        confrev = int(f.p("confrev", "valor")) if f.on("confrev") else self.confrev
        rc_bits = int(prof["rcb"].get("reasoncode_bits", 6))
        n = len(ds.members)
        ovf = self.bufovfl
        if self.buffered and opt.get(6):
            self.bufovfl = False

        def head(o: Bits, seg: tuple | None) -> bytes:
            h = str_tlv(0x8A, rptid) + tlv(0x84, o.encode_value())
            if o.get(1):
                h += uint_tlv(0x86, self.sqnum)
            if o.get(2):
                h += tlv(0x8C, toe)
            if o.get(4):
                h += str_tlv(0x8A, ds.ref)
            if o.get(6):
                h += bool_tlv(0x83, ovf)
            if o.get(7):
                h += tlv(0x89, eid)
            if o.get(8):
                h += uint_tlv(0x86, confrev)
            if seg is not None:
                h += uint_tlv(0x86, seg[0]) + bool_tlv(0x83, seg[1])
            return h

        def body(items) -> list[bytes]:
            inc = Bits.from_list(n, [i for i, _, _ in items])
            parts = [tlv(0x84, inc.encode_value())]
            if opt.get(5):
                parts += [str_tlv(0x8A, ds.members[i].ref) for i, _, _ in items]
            parts += [d for _, _, d in items]
            if opt.get(3):
                parts += [tlv(0x84, Bits.from_list(rc_bits, [b for b in range(rc_bits) if rb & (1 << b)])
                              .encode_value()) for _, rb, _ in items]
            return parts

        single = info_report("RPT", [head(opt, None)] + body(included))
        if len(single) + 40 <= max_pdu or len(included) <= 1 or not prof["rcb"].get("segmentacao", True):
            return [single]
        # segmentação (OptFlds bit 9, SubSeqNum e MoreSegmentsFollow)
        sopt = opt.set(9, True)
        hsize = len(head(sopt, (0, True))) + 40
        segs, cur, size = [], [], hsize
        for it in included:
            isz = len(it[2]) + (len(ds.members[it[0]].ref) + 4 if opt.get(5) else 0) + 6
            if cur and size + isz > max_pdu:
                segs.append(cur)
                cur, size = [], hsize
            cur.append(it)
            size += isz
        if cur:
            segs.append(cur)
        return [info_report("RPT", [head(sopt, (k, k < len(segs) - 1))] + body(s)) for k, s in enumerate(segs)]

    # ------------------------------------------------------------------
    def state(self) -> dict:
        ds = self.dataset
        return {
            "ref": self.ref, "nome": self.name, "base": self.base, "ld": self.domain, "ln": self.ln,
            "tipo": "BRCB" if self.buffered else "URCB", "extra": self.extra,
            "RptID": self.rptid, "RptID_cid": self.rptid_cid, "RptEna": self.ena,
            "Resv": self.reserved_by is not None, "DatSet": self.datset, "dataset_ok": ds is not None,
            "ConfRev": self.confrev, "OptFlds": bits_names(self.optflds, OPT_NAMES),
            "BufTm": self.buftm, "SqNum": self.sqnum, "TrgOps": bits_names(self.trgops, TRG_NAMES),
            "TrgOps_efetivo": bits_names(self.eff_trgops(), TRG_NAMES), "IntgPd": self.intgpd,
            "EntryID": self.last_eid.hex(), "buffer": len(self.buffer) if self.buffered else None,
            "cliente": self.owner.peer_str if self.owner is not None else None,
            "reservado_por": self.reserved_by.peer_str if self.reserved_by is not None else None,
            "reports_enviados": self.reports_sent,
            "politica": self.policy.to_json(), "estado_politica": self.policy.politica,
            "recusadas": self.denied,
        }


class ReportEngine:
    def __init__(self, ied):
        self.ied = ied
        self.rcbs: list[Rcb] = []
        self.leaf_index: dict[tuple, list] = {}
        self.by_ds: dict[str, list[Rcb]] = {}
        self._batch = itertools.count(1)
        self._held = None
        self._drop_counter = 0

    @property
    def profile(self) -> dict:
        return self.ied.profile

    @property
    def edition(self) -> int:
        return int(self.ied.profile.get("edicao", 1))

    def rebuild_index(self):
        self.leaf_index = {}
        for ds in self.ied.all_datasets():
            for idx, m in enumerate(ds.members):
                for leaf in m.node.leaves():
                    self.leaf_index.setdefault((leaf.domain, leaf.path), []).append((ds, idx))
        self.rebuild_rcb_index()

    def rebuild_rcb_index(self):
        self.by_ds = {}
        for r in self.rcbs:
            if r.datset:
                self.by_ds.setdefault(r.datset, []).append(r)

    def notify(self, changes: list[tuple]):
        """changes: [(domínio, caminho_da_folha, motivo, id_da_mudança)] numa mesma operação."""
        batch = next(self._batch)
        acc: dict[tuple, list] = {}
        for dom, path, reason, cid in changes:
            for ds, idx in self.leaf_index.get((dom, path), ()):
                key = (ds.ref, idx)
                a = acc.get(key)
                if a is None:
                    a = acc[key] = [ds, 0, []]
                a[1] |= 1 << REASON_BIT[reason]
                if cid is not None and cid not in a[2]:
                    a[2].append(cid)
        if not acc:
            return
        zero_t = self.ied.faults.on("omitir_t")
        to_flush: set = set()
        for (dsref, idx), (ds, rbits, cids) in acc.items():
            rcbs = self.by_ds.get(dsref)
            if not rcbs:
                continue
            data = encode_node(ds.members[idx].node, zero_t)
            for r in rcbs:
                r.on_event(idx, rbits, data, cids, batch, to_flush)
        for r in to_flush:
            r.flush()

    def association_closed(self, assoc):
        for r in self.rcbs:
            r.association_closed(assoc)

    def stop(self):
        for r in self.rcbs:
            if r.timer:
                r.timer.cancel()
                r.timer = None
            if r.intg_task:
                r.intg_task.cancel()
                r.intg_task = None
            r.ena = False
            r.owner = None
            r.reserved_by = None
            r.pending.clear()

    # ------------------------------------------------------------------
    def deliver(self, rcb: Rcb, e: Entry, replay: bool = False):
        ied = self.ied
        f = ied.faults
        assoc = rcb.owner
        if assoc is None or assoc.closed:
            return
        notes = []
        if replay:
            notes.append("reenvio do buffer (BRCB)")
        if f.on("report_unico"):
            same = [r for r in self.rcbs if r.base == rcb.base and r.ln == rcb.ln and r.domain == rcb.domain
                    and r.ena and r.owner is not None]
            first = min(same, key=lambda r: r.owner.id) if same else rcb
            if first is not rcb:
                ied.log_report(rcb, e, assoc, ["RN-FLT-08: report entregue só ao primeiro cliente"], dropped=True)
                return
            notes.append("RN-FLT-08 ativo")
        if f.on("descarte"):
            ponto = (f.p("descarte", "ponto") or "").strip()
            drop = False
            if ponto:
                ds = rcb.dataset
                drop = any(ponto in ds.members[i].ref for i, _, _ in e.included) if ds else False
            else:
                n = int(f.p("descarte", "um_a_cada") or 0)
                self._drop_counter += 1
                drop = n > 0 and self._drop_counter % n == 0
            if drop:
                ied.log_report(rcb, e, assoc, ["RN-FLT-02: report descartado"], dropped=True)
                return
        pdus = list(e.pdus)
        if f.on("duplicar"):
            if f.p("duplicar", "modo") == "duplicar":
                pdus = pdus + pdus
                notes.append("RN-FLT-03: report duplicado")
            else:
                notes.append("RN-FLT-03: EntryID repetido")
        if f.on("rptid"):
            notes.append("RN-FLT-04: RptID alterado")
        if f.on("confrev"):
            notes.append("RN-FLT-04: ConfRev alterado")
        if f.on("omitir_t"):
            notes.append("RN-FLT-04: t omitido (zerado)")

        def send(ps=pdus, ns=notes, entry=e, a=assoc, r=rcb):
            if a.closed:
                return
            for p in ps:
                a.send_mms(p)
            r.reports_sent += 1
            if r.buffered:
                r.last_sent_seq = max(r.last_sent_seq, entry.seq)
            ied.log_report(r, entry, a, ns)

        if f.on("atraso"):
            ms = int(f.p("atraso", "ms") or 0)
            loop = asyncio.get_running_loop()
            if f.p("atraso", "fora_de_ordem"):
                notes.append("RN-FLT-01: fora de ordem")
                if self._held is None:
                    self._held = send
                    loop.call_later(max(ms, 50) / 1000.0, self._release_held)
                    return
                send()
                self._release_held()
                return
            notes.append(f"RN-FLT-01: atraso {ms} ms")
            loop.call_later(ms / 1000.0, send)
            return
        send()

    def _release_held(self):
        h, self._held = self._held, None
        if h is not None:
            h()


def decode_included(e: Entry, ds: DataSet | None) -> list[dict]:
    out = []
    for idx, rb, data in e.included:
        try:
            raw, _ = dec_data(data)
            val = raw_to_json(raw)
        except Exception:  # noqa: BLE001
            val = data.hex()
        out.append({"pos": idx, "ref": ds.members[idx].ref if ds and idx < len(ds.members) else str(idx),
                    "motivo": [n for n, b in REASON_BIT.items() if rb & (1 << b)], "valor": val})
    return out
