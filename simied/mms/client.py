"""Cliente MMS assíncrono mínimo.

Usado pelo vínculo com o relé real (somente leitura, RN-SEG-02) e pelos testes automáticos.
"""
from __future__ import annotations

import asyncio
import itertools

from ..ber import children, dec_tlv
from . import iso
from .pdu import (PDU_CONF_ERR, PDU_CONF_RESP, PDU_REJECT, PDU_UNCONF, InitParams, ObjName, conf_req, parse_var_access,
                  enc_initiate, parse_initiate, parse_namelist_resp, parse_write_resp, req_defnvl,
                  req_getnamelist, req_getnvlattr, req_getvarattr, req_identify, req_read, req_read_nvl,
                  req_write, services_supported)
from .types import dec_data_raw


class MmsClientError(Exception):
    pass


class MmsClient:
    def __init__(self, host: str, port: int = 102, timeout: float = 5.0, read_only: bool = False):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.read_only = read_only
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.tpdu = 1024
        self.pending: dict[int, asyncio.Future] = {}
        self.invoke = itertools.count(1)
        self.reports: asyncio.Queue = asyncio.Queue()
        self.on_report = None
        self.rx_task: asyncio.Task | None = None
        self.connected = False
        self.init_resp: InitParams | None = None
        self.raw_reports: list[bytes] = []

    async def connect(self):
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), self.timeout)
        self.writer.write(iso.cotp_cr())
        cc = iso.parse_cotp(await asyncio.wait_for(iso.read_tpkt(self.reader), self.timeout))
        if cc.code != 0xD0:
            raise MmsClientError("COTP CC não recebido")
        code = cc.params.get(0xC0, b"\x0a")[0]
        self.tpdu = iso.TPDU_SIZES.get(code, 1024)
        init = enc_initiate(0xA8, InitParams(services=services_supported(
            ["status", "getNameList", "identify", "read", "write", "getVariableAccessAttributes",
             "getNamedVariableListAttributes", "defineNamedVariableList", "informationReport"])))
        cn = iso.spdu_cn(iso.build_cp(iso.build_aarq(init)))
        for f in iso.cotp_dt_frames(cn, self.tpdu):
            self.writer.write(f)
        data = await asyncio.wait_for(self._read_spdu(), self.timeout)
        s = iso.parse_spdu(data)
        if s.si != iso.SPDU_AC:
            raise MmsClientError(f"sessão recusada (SPDU {s.si})")
        pdvs = iso.parse_cpa(s.user_data)
        ok, mms = iso.parse_aare(pdvs[0][1]) if pdvs else (False, b"")
        if not ok:
            raise MmsClientError("associação recusada pelo servidor (AARE rejected)")
        t, v, _ = dec_tlv(mms, 0)
        if t != 0xA9:
            raise MmsClientError("initiate-Response ausente")
        self.init_resp = parse_initiate(v)
        self.connected = True
        self.rx_task = asyncio.get_running_loop().create_task(self._rx_loop())

    async def _read_spdu(self) -> bytes:
        buf = bytearray()
        while True:
            c = iso.parse_cotp(await iso.read_tpkt(self.reader))
            if c.code == 0xF0:
                buf.extend(c.data)
                if c.eot:
                    return bytes(buf)
            elif c.code == 0x80:
                raise MmsClientError("desconectado (COTP DR)")

    async def _rx_loop(self):
        try:
            while True:
                data = await self._read_spdu()
                s = iso.parse_spdu(data)
                if s.si == iso.SPDU_DN:
                    break
                if s.si != iso.SPDU_DT:
                    continue
                for pci, pdu in iso.parse_user_data(s.user_data):
                    self._on_pdu(pdu)
        except (asyncio.IncompleteReadError, ConnectionError, MmsClientError, asyncio.CancelledError):
            pass
        finally:
            self.connected = False
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(MmsClientError("conexão encerrada"))
            self.pending.clear()

    def _on_pdu(self, pdu: bytes):
        tag, val, _ = dec_tlv(pdu, 0)
        if tag in (PDU_CONF_RESP, PDU_CONF_ERR, PDU_REJECT):
            c = children(val)
            inv = int.from_bytes(c[0][1], "big") if c else -1
            f = self.pending.pop(inv, None)
            if f is not None and not f.done():
                f.set_result((tag, c[1:]))
        elif tag == PDU_UNCONF:
            self.raw_reports.append(pdu)
            rpt = parse_info_report(val)
            self.reports.put_nowait(rpt)
            if self.on_report:
                self.on_report(rpt)
        elif tag == 0x8C:
            f = self.pending.pop(-2, None)
            if f and not f.done():
                f.set_result((tag, []))

    async def request(self, service: bytes):
        if not self.connected:
            raise MmsClientError("não conectado")
        inv = next(self.invoke)
        fut = asyncio.get_running_loop().create_future()
        self.pending[inv] = fut
        self._send(conf_req(inv, service))
        tag, rest = await asyncio.wait_for(fut, self.timeout)
        if tag == PDU_CONF_ERR:
            raise MmsClientError(f"erro de serviço: {[(hex(t), v.hex()) for t, v in rest]}")
        if tag == PDU_REJECT:
            raise MmsClientError(f"reject: {[(hex(t), v.hex()) for t, v in rest]}")
        return rest[0] if rest else (0, b"")

    def _send(self, pdu: bytes):
        spdu = iso.spdu_dt(iso.user_data(3, pdu))
        for f in iso.cotp_dt_frames(spdu, self.tpdu):
            self.writer.write(f)

    # ------------------------------------------------------------------
    async def read(self, names: list[ObjName]) -> list:
        t, v = await self.request(req_read(names))
        out = []
        for t2, v2 in children(v):
            if t2 == 0xA1:
                for t3, v3 in children(v2):
                    out.append(("error", v3[0]) if t3 == 0x80 else dec_data_raw(t3, v3))
        return out

    async def read_nvl(self, name: ObjName) -> list:
        t, v = await self.request(req_read_nvl(name))
        out = []
        for t2, v2 in children(v):
            if t2 == 0xA1:
                for t3, v3 in children(v2):
                    out.append(("error", v3[0]) if t3 == 0x80 else dec_data_raw(t3, v3))
        return out

    async def write(self, names: list[ObjName], datas: list[bytes]) -> list:
        if self.read_only:
            raise MmsClientError("cliente somente leitura (RN-SEG-02)")
        t, v = await self.request(req_write(names, datas))
        return parse_write_resp(v)

    async def get_name_list(self, obj_class: int, domain: str | None = None) -> list[str]:
        names: list[str] = []
        cont = None
        while True:
            t, v = await self.request(req_getnamelist(obj_class, domain, cont))
            got, more = parse_namelist_resp(v)
            names += got
            if not more or not got:
                return names
            cont = got[-1]

    async def get_var_attr(self, name: ObjName) -> bytes:
        t, v = await self.request(req_getvarattr(name))
        return v

    async def get_nvl_attr(self, name: ObjName) -> list[str]:
        t, v = await self.request(req_getnvlattr(name))
        out = []
        for t2, v2 in children(v):
            if t2 == 0xA1:
                for _, item in children(v2):
                    for t3, v3 in children(item):
                        if t3 == 0xA0:
                            _, on, _ = dec_tlv(v3, 0)
                            c = children(on)
                            out.append(f"{c[0][1].decode()}/{c[1][1].decode()}")
        return out

    async def define_nvl(self, name: ObjName, members: list[ObjName]):
        return await self.request(req_defnvl(name, members))

    async def identify(self) -> tuple[str, str, str]:
        t, v = await self.request(req_identify())
        c = {t2: v2.decode("latin-1") for t2, v2 in children(v)}
        return c.get(0x80, ""), c.get(0x81, ""), c.get(0x82, "")

    async def close(self):
        try:
            if self.connected:
                fut = asyncio.get_running_loop().create_future()
                self.pending[-2] = fut
                self._send(b"\x8b\x00")
                try:
                    await asyncio.wait_for(fut, 1.0)
                except (asyncio.TimeoutError, MmsClientError):
                    pass
                fn = iso.spdu_fn(iso.user_data(1, iso.RLRQ))
                for f in iso.cotp_dt_frames(fn, self.tpdu):
                    self.writer.write(f)
                await asyncio.sleep(0.05)
        except Exception:  # noqa: BLE001
            pass
        if self.rx_task:
            self.rx_task.cancel()
        if self.writer:
            self.writer.close()
        self.connected = False


def parse_info_report(val: bytes) -> dict:
    """Decodifica um informationReport 'RPT' (IEC 61850-8-1) sem conhecer o DataSet."""
    c = children(val)
    if not c:
        return {}
    inner = children(c[0][1]) if c[0][0] == 0xA0 else []
    spec = inner[0] if inner else (0, b"")
    results = [dec_data_raw(t, v) for t, v in children(inner[1][1])] if len(inner) > 1 else []
    rpt: dict = {"var": "", "itens": results}
    if spec[0] == 0xA0:  # listOfVariable (LastApplError, CommandTermination)
        try:
            rpt["vars"] = [vs.name.text() if vs.name.scope == "dom" else vs.name.item
                           for vs in parse_var_access(spec[0], spec[1]).vars]
        except Exception:  # noqa: BLE001
            rpt["vars"] = []
        return rpt
    try:
        _, on, _ = dec_tlv(spec[1], 0)
        rpt["var"] = on.decode("latin-1")
    except Exception:  # noqa: BLE001
        pass
    if rpt["var"] != "RPT" or len(results) < 2:
        return rpt
    i = 0
    rpt["RptID"] = results[i][1]
    i += 1
    opt = results[i][1]
    rpt["OptFlds"] = opt.bitstr()
    i += 1
    if opt.get(1):
        rpt["SqNum"] = results[i][1]
        i += 1
    if opt.get(2):
        rpt["TimeOfEntry"] = results[i][1].hex()
        i += 1
    if opt.get(4):
        rpt["DatSet"] = results[i][1]
        i += 1
    if opt.get(6):
        rpt["BufOvfl"] = results[i][1]
        i += 1
    if opt.get(7):
        rpt["EntryID"] = results[i][1].hex()
        i += 1
    if opt.get(8):
        rpt["ConfRev"] = results[i][1]
        i += 1
    if opt.get(9):
        rpt["SubSeqNum"] = results[i][1]
        rpt["MoreSegmentsFollow"] = results[i + 1][1]
        i += 2
    inc = results[i][1]
    i += 1
    idxs = [k for k in range(inc.n) if inc.get(k)]
    rpt["Inclusion"] = inc.bitstr()
    rpt["incluidos"] = idxs
    if opt.get(5):
        rpt["DataRef"] = [results[i + k][1] for k in range(len(idxs))]
        i += len(idxs)
    rpt["valores"] = results[i:i + len(idxs)]
    i += len(idxs)
    if opt.get(3):
        rpt["ReasonCode"] = [results[i + k][1].bitstr() for k in range(len(idxs)) if i + k < len(results)]
    return rpt
