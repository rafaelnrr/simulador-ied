"""Servidor MMS por IED (seção 7: serviços MMS atendidos)."""
from __future__ import annotations

import asyncio
import bisect
import itertools
import logging
import time

from ..ber import BerError, bool_tlv, children, dec_tlv, str_tlv, tlv
from . import iso
from .pdu import (PDU_CONCL_REQ, PDU_CONF_REQ, PDU_INIT_REQ, PDU_UNCONF, SERVICE_NAMES, SRV_DEFNVL, SRV_DELNVL,
                  SRV_GETNAMELIST, SRV_GETNVLATTR, SRV_GETVARATTR, SRV_IDENTIFY, SRV_READ, SRV_STATUS,
                  SRV_WRITE, InitParams, ObjName, conf_error, conf_resp, enc_initiate, enc_objname,
                  parse_confirmed, parse_initiate, parse_objname, parse_var_access, reject, services_supported)

log = logging.getLogger("simied.mms")


class ServiceError(Exception):
    def __init__(self, cls: str, code: int, msg: str = ""):
        super().__init__(msg or f"{cls}:{code}")
        self.cls = cls
        self.code = code


class Silent(Exception):
    """Não responder (falha RN-FLT-07)."""


_assoc_ids = itertools.count(1)


class Association:
    def __init__(self, server: "MmsServer", reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.id = next(_assoc_ids)
        self.server = server
        self.ied = server.ied
        self.reader = reader
        self.writer = writer
        peer = writer.get_extra_info("peername") or ("?", 0)
        local = writer.get_extra_info("sockname") or ("?", 0)
        self.peer = (peer[0], peer[1])
        self.local = (local[0], local[1])
        self.peer_str = f"{peer[0]}:{peer[1]}"
        self.closed = False
        self.associated = False
        self.tpdu = 128
        self.max_pdu = 65000
        self.mms_pci = 3
        self.acse_pci = 1
        self.rx = bytearray()
        self.nvls: dict = {}
        self.started = time.time()
        self.close_reason = ""
        self.calling_ap = ""
        self.requests = 0
        self.was_associated = False
        self.dst_ref = 0
        self._close_task: asyncio.TimerHandle | None = None

    def owner_bytes(self) -> bytes:
        try:
            import ipaddress
            return ipaddress.ip_address(self.peer[0]).packed
        except ValueError:
            return b""

    # ------------------------------------------------------------------
    async def run(self):
        try:
            while not self.closed:
                frame = await iso.read_tpkt(self.reader)
                self.ied.capture(self, "rx", frame)
                self.on_frame(frame)
        except (asyncio.IncompleteReadError, ConnectionError):
            self.close_reason = self.close_reason or "conexão encerrada pelo cliente"
        except asyncio.CancelledError:
            self.close_reason = self.close_reason or "servidor parado"
        except (iso.IsoError, BerError) as e:
            self.close_reason = f"erro de protocolo: {e}"
            log.warning("[%s] %s", self.peer_str, e)
        except Exception as e:  # noqa: BLE001
            self.close_reason = f"erro interno: {e}"
            log.exception("erro na associação %s", self.peer_str)
        finally:
            self.close(self.close_reason or "encerrada")

    def close(self, reason: str = ""):
        if self.closed:
            return
        self.closed = True
        self.close_reason = self.close_reason or reason
        if self._close_task:
            self._close_task.cancel()
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass
        self.server.closed(self)

    def raw_send(self, data: bytes):
        if self.closed:
            return
        self.ied.capture(self, "tx", data)
        self.writer.write(data)

    def send_spdu(self, spdu: bytes):
        for f in iso.cotp_dt_frames(spdu, self.tpdu):
            self.raw_send(f)

    def send_mms(self, pdu: bytes):
        self.send_spdu(iso.spdu_dt(iso.user_data(self.mms_pci, pdu)))

    # ------------------------------------------------------------------
    def on_frame(self, frame: bytes):
        c = iso.parse_cotp(frame)
        if c.code == 0xE0:
            cc, self.tpdu = iso.cotp_cc(c)
            self.dst_ref = c.src_ref
            self.raw_send(cc)
        elif c.code == 0xF0:
            self.rx.extend(c.data)
            if c.eot:
                data = bytes(self.rx)
                self.rx.clear()
                self.on_session(data)
        elif c.code in (0x80, 0xC0):
            self.close_reason = "desconexão COTP pelo cliente"
            self.close()
        else:
            raise iso.IsoError(f"TPDU COTP não suportado {c.code:#x}")

    def on_session(self, data: bytes):
        s = iso.parse_spdu(data)
        if s.si == iso.SPDU_CN:
            self.on_connect(s)
        elif s.si == iso.SPDU_DT:
            if not self.associated:
                raise iso.IsoError("dados antes da associação")
            for pci, pdu in iso.parse_user_data(s.user_data):
                if pci == self.acse_pci:
                    continue
                self.on_mms(pdu)
        elif s.si == iso.SPDU_FN:
            self.associated = False
            self.ied.mms_event(self, "Release", "liberação ACSE (FN/RLRQ)", "ok")
            self.send_spdu(iso.spdu_dn(iso.user_data(self.acse_pci, iso.RLRE)))
            self.close_reason = "liberação normal (Conclude/Release)"
        elif s.si == iso.SPDU_AB:
            self.close_reason = "abort de sessão pelo cliente"
            self.close()
        else:
            raise iso.IsoError(f"SPDU não suportado {s.si}")

    def on_connect(self, s: iso.Spdu):
        cp = iso.parse_cp(s.user_data)
        self.acse_pci = cp.ctx_id(iso.OID_ACSE_AS, 1)
        self.mms_pci = cp.ctx_id(iso.OID_MMS_AS, 3)
        acse = next((d for pci, d in cp.pdvs if pci == self.acse_pci), cp.pdvs[0][1] if cp.pdvs else b"")
        aarq = iso.parse_aarq(acse)
        self.calling_ap = aarq.calling_ap_title
        tag, val, _ = dec_tlv(aarq.mms, 0)
        if tag != PDU_INIT_REQ:
            raise iso.IsoError("AARQ sem initiate-RequestPDU")
        req = parse_initiate(val)
        ok, why = self.server.can_accept(self)
        prof = self.ied.profile
        if not ok:
            self.ied.mms_event(self, "Initiate", why, "recusada")
            self.ied.mgr.netmon.app(self.ied, self, "recusa", "bad", f"associação recusada: {why}")
            aare = iso.build_aare(b"", self.mms_pci, accepted=False)
            self.send_spdu(iso.spdu_ac(s, iso.build_cpa(cp, aare, self.acse_pci)))
            self.close_reason = why
            asyncio.get_running_loop().call_later(0.2, self.close)
            return
        lim = prof["limites"]
        self.max_pdu = min(req.local_detail or 65000, int(lim["max_pdu"]))
        resp = InitParams(
            local_detail=self.max_pdu,
            max_serv_calling=min(req.max_serv_calling, int(lim["max_serv_outstanding"])) or 1,
            max_serv_called=min(req.max_serv_called, int(lim["max_serv_outstanding"])) or 1,
            nesting=min(req.nesting or 10, int(lim["nesting"])),
            version=1,
            cbb=req.cbb if req.cbb else iso_cbb(),
            services=services_supported(self.server.services),
        )
        aare = iso.build_aare(enc_initiate(0xA9, resp), self.mms_pci, True)
        self.send_spdu(iso.spdu_ac(s, iso.build_cpa(cp, aare, self.acse_pci)))
        self.associated = True
        self.was_associated = True
        self.ied.session_opened(self)
        f = self.ied.faults
        if f.on("fechar_apos"):
            secs = max(int(f.p("fechar_apos", "segundos")), 1)
            self._close_task = asyncio.get_running_loop().call_later(secs, self._fault_close, secs)

    def _fault_close(self, secs):
        self.ied.mms_event(self, "Abort", f"RN-FLT-05: associação fechada após {secs} s", "falha")
        self.close_reason = f"RN-FLT-05: fechada pelo simulador após {secs} s"
        self.close()

    # ------------------------------------------------------------------
    def on_mms(self, pdu: bytes):
        tag, val, _ = dec_tlv(pdu, 0)
        if tag == PDU_CONF_REQ:
            self.requests += 1
            try:
                req = parse_confirmed(val)
            except BerError:
                self.send_mms(reject(None, 0x81, 4))
                return
            try:
                resp = self.dispatch(req)
            except Silent:
                return
            except ServiceError as e:
                self.ied.mms_event(self, SERVICE_NAMES.get(req.service, f"serviço {req.service}"),
                                   str(e), f"erro {e.cls}/{e.code}")
                resp = conf_error(req.invoke, e.cls, e.code)
            except BerError as e:
                self.ied.mms_event(self, SERVICE_NAMES.get(req.service, f"serviço {req.service}"),
                                   f"PDU inválido: {e}", "reject")
                resp = reject(req.invoke, 0x81, 4)
            self.send_mms(resp)
        elif tag == PDU_CONCL_REQ:
            self.ied.mms_event(self, "Conclude", "conclude-Request", "ok")
            self.send_mms(b"\x8c\x00")
        elif tag == PDU_UNCONF:
            pass
        else:
            self.ied.mms_event(self, "PDU", f"PDU MMS inesperado {tag:#x}", "ignorado")

    def dispatch(self, req) -> bytes:
        s = req.service
        ied = self.ied
        if s == SRV_READ:
            return conf_resp(req.invoke, self.h_read(req))
        if s == SRV_WRITE:
            return conf_resp(req.invoke, self.h_write(req))
        if s == SRV_GETNAMELIST:
            return conf_resp(req.invoke, self.h_getnamelist(req))
        if s == SRV_GETVARATTR:
            return conf_resp(req.invoke, self.h_getvarattr(req))
        if s == SRV_GETNVLATTR and "getNamedVariableListAttributes" in self.server.services:
            return conf_resp(req.invoke, self.h_getnvlattr(req))
        if s == SRV_IDENTIFY:
            idn = ied.profile["identificacao"]
            ied.mms_event(self, "Identify", f"{idn['fabricante']} / {idn['modelo']} / {idn['revisao']}", "ok")
            body = str_tlv(0x80, idn["fabricante"]) + str_tlv(0x81, idn["modelo"]) + str_tlv(0x82, idn["revisao"])
            return conf_resp(req.invoke, tlv(0xA2, body))
        if s == SRV_STATUS:
            ied.mms_event(self, "Status", "teste de vida", "ok")
            return conf_resp(req.invoke, tlv(0xA0, b"\x80\x01\x00\x81\x01\x00"))
        if s == SRV_DEFNVL and "defineNamedVariableList" in self.server.services:
            return conf_resp(req.invoke, self.h_defnvl(req))
        if s == SRV_DELNVL and "deleteNamedVariableList" in self.server.services:
            return conf_resp(req.invoke, self.h_delnvl(req))
        name = SERVICE_NAMES.get(s, f"serviço {s}")
        ied.mms_event(self, name, "serviço não oferecido pelo perfil", "reject unrecognized-service")
        return reject(req.invoke, 0x81, 1)

    # ------------------------------------------------------------------
    def h_read(self, req) -> bytes:
        with_result = False
        va = None
        for t, v in children(req.body):
            if t == 0x80:
                with_result = bool(v and v[0])
            elif t == 0xA1:
                t2, v2, _ = dec_tlv(v, 0)
                va = parse_var_access(t2, v2)
        if va is None:
            raise BerError("Read sem variableAccessSpecification")
        if self.ied.faults.on("silenciar_read"):
            self.ied.mms_event(self, "Read", "RN-FLT-07: Read sem resposta", "silenciado")
            raise Silent()
        if va.kind == "list":
            results = [self.ied.mms_read(self, vs) for vs in va.vars]
            names = ", ".join(vs.name.text() if vs.name else "?" for vs in va.vars[:4])
            more = f" (+{len(va.vars) - 4})" if len(va.vars) > 4 else ""
            self.ied.mms_event(self, "Read", f"{names}{more}", "ok")
        else:
            ds = self.ied.find_nvl(self, va.nvl)
            if ds is None:
                raise ServiceError("access", 2, f"DataSet {va.nvl.text()} inexistente")
            results = [m.node.encode() for m in ds.members]
            self.ied.mms_event(self, "Read", f"GetDataSetValues {va.nvl.text()} ({len(results)} membros)", "ok")
        body = (tlv(0xA0, va.raw) if with_result else b"") + tlv(0xA1, b"".join(results))
        return tlv(0xA4, body)

    def h_write(self, req) -> bytes:
        c = children(req.body)
        if len(c) < 2:
            raise BerError("Write incompleto")
        va = parse_var_access(c[0][0], c[0][1])
        datas = children(c[1][1])
        if va.kind == "list":
            names = [vs.name for vs in va.vars]
        else:
            ds = self.ied.find_nvl(self, va.nvl)
            if ds is None:
                raise ServiceError("access", 2, "DataSet inexistente")
            names = [ObjName("dom", m.domain, m.item) for m in ds.members]
        if len(names) != len(datas):
            raise BerError("Write com número de dados diferente do número de variáveis")
        out = b""
        for n, d in zip(names, datas):
            code = self.ied.mms_write(self, n, d)
            out += tlv(0x81, b"") if code is None else tlv(0x80, bytes([code]))
        return tlv(0xA5, out)

    def h_getnamelist(self, req) -> bytes:
        m = {}
        for t, v in children(req.body):
            m.setdefault(t, v)
        oc = m.get(0xA0, b"")
        cls = None
        if oc:
            t, v, _ = dec_tlv(oc, 0)
            if t == 0x80:
                cls = int.from_bytes(v, "big")
        scope_raw = m.get(0xA1, b"")
        st, sv, _ = dec_tlv(scope_raw, 0)
        if st == 0x80:
            scope, domain = "vmd", None
        elif st == 0x81:
            scope, domain = "dom", sv.decode("latin-1")
        else:
            scope, domain = "aa", None
        cont = m.get(0x82)
        names = self.ied.namelist(self, cls, scope, domain)
        if names is None:
            dica = self.ied.mgr.netmon.dono_dominio(self.ied, domain)
            self.ied.mgr.netmon.app(self.ied, self, "dominio", "bad", f"pediu o LD {domain}, que não existe{dica}")
            raise ServiceError("definition", 1, f"domínio {domain} inexistente{dica}")
        start = 0
        if cont is not None:
            start = bisect.bisect_right(names, cont.decode("latin-1"))
        budget = self.max_pdu - 60
        out = []
        size = 0
        i = start
        while i < len(names):
            enc = str_tlv(0x1A, names[i])
            if size + len(enc) > budget and out:
                break
            out.append(enc)
            size += len(enc)
            i += 1
        more = i < len(names)
        what = {0: "namedVariable", 2: "namedVariableList", 9: "domain"}.get(cls, f"classe {cls}")
        self.ied.mms_event(self, "GetNameList", f"{what} {domain or scope} → {len(out)} nomes"
                           f"{' (moreFollows)' if more else ''}", "ok")
        return tlv(0xA1, tlv(0xA0, b"".join(out)) + bool_tlv(0x81, more))

    def h_getvarattr(self, req) -> bytes:
        c = children(req.body)
        if not c or c[0][0] != 0xA0:
            raise ServiceError("access", 1, "GetVariableAccessAttributes só por nome")
        t, v, _ = dec_tlv(c[0][1], 0)
        on = parse_objname(t, v)
        node = self.ied.lookup(on)
        if node is None or self.ied.is_hidden(on):
            raise ServiceError("access", 2, f"{on.text()} inexistente")
        self.ied.mms_event(self, "GetVariableAccessAttributes", on.text(), "ok")
        return tlv(0xA6, bool_tlv(0x80, False) + tlv(0xA2, self.ied.var_type_spec(node)))

    def h_getnvlattr(self, req) -> bytes:
        t, v, _ = dec_tlv(req.body, 0)
        on = parse_objname(t, v)
        ds = self.ied.find_nvl(self, on)
        if ds is None:
            raise ServiceError("access", 2, f"DataSet {on.text()} inexistente")
        items = b"".join(tlv(0x30, tlv(0xA0, enc_objname(ObjName("dom", m.domain, m.item)))) for m in ds.members)
        deletable = ds.extra and on.scope == "aa" or getattr(ds, "dynamic", False)
        self.ied.mms_event(self, "GetNamedVariableListAttributes", f"{on.text()} ({len(ds.members)} membros)", "ok")
        return tlv(0xAC, bool_tlv(0x80, bool(deletable)) + tlv(0xA1, items))

    def h_defnvl(self, req) -> bytes:
        c = children(req.body)
        on = parse_objname(c[0][0], c[0][1])
        va = parse_var_access(c[1][0], c[1][1])
        self.ied.define_nvl(self, on, [vs.name for vs in va.vars])
        self.ied.mms_event(self, "DefineNamedVariableList", f"{on.text()} ({len(va.vars)} membros)", "ok")
        return tlv(0x8B, b"")

    def h_delnvl(self, req) -> bytes:
        names = []
        for t, v in children(req.body):
            if t == 0xA1:
                for t2, v2 in children(v):
                    names.append(parse_objname(t2, v2))
        matched, deleted = self.ied.delete_nvl(self, names)
        self.ied.mms_event(self, "DeleteNamedVariableList", ", ".join(n.text() for n in names), f"{deleted} apagado(s)")
        from ..ber import uint_tlv
        return tlv(0xAD, uint_tlv(0x80, matched) + uint_tlv(0x81, deleted))


def iso_cbb() -> bytes:
    return bytes([0x05, 0xF1, 0x00])


class MmsServer:
    def __init__(self, ied):
        self.ied = ied
        self.server: asyncio.base_events.Server | None = None
        self.assocs: dict[int, Association] = {}
        self.tasks: set = set()
        self.services: list[str] = []

    async def start(self, host: str, port: int):
        self.server = await asyncio.start_server(self._client, host, port, reuse_address=True)

    async def _client(self, reader, writer):
        a = Association(self, reader, writer)
        self.assocs[a.id] = a
        self.ied.emit("tcp.opened", {"cliente": a.peer_str})
        self.ied.mgr.netmon.tcp_aceita(self.ied, a)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            await a.run()
        finally:
            self.tasks.discard(task)

    def can_accept(self, a: Association) -> tuple[bool, str]:
        if self.ied.faults.on("recusar_assoc"):
            return False, "RN-FLT-05: associação recusada pela falha injetada"
        maxa = int(self.ied.cfg.get("max_associacoes") or self.ied.profile["limites"]["max_associacoes"])
        active = sum(1 for x in self.assocs.values() if x.associated and not x.closed and x is not a)
        if active >= maxa:
            return False, f"limite de {maxa} associações atingido (RN-NET-06)"
        return True, ""

    def closed(self, a: Association):
        self.assocs.pop(a.id, None)
        if a.was_associated:
            self.ied.session_closed(a)
        else:
            why = a.close_reason or "encerrada"
            self.ied.mms_event(a, "Disconnect", f"conexão TCP fechada antes da associação MMS: {why}", "fechada")
            self.ied.mgr.netmon.app(self.ied, a, "fim", "warn", f"conexão fechada antes da associação MMS: {why}")

    def associations(self) -> list[Association]:
        return [a for a in self.assocs.values() if a.associated and not a.closed]

    async def stop(self, reason: str = "IED parado"):
        if self.server is not None:
            self.server.close()
        for a in list(self.assocs.values()):
            a.close_reason = reason
            a.close(reason)
        for t in list(self.tasks):
            t.cancel()
        if self.server is not None:
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        self.server = None
