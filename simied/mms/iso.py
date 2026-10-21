"""Camadas ISO abaixo do MMS: TPKT (RFC 1006), COTP (ISO 8073 classe 0),
sessão (ISO 8327), apresentação (ISO 8823) e ACSE (ISO 8650).

Implementa só o subconjunto usado pelo perfil IEC 61850-8-1 (A-Profile/T-Profile).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..ber import BerError, child_map, children, dec_oid, dec_tlv, enc_oid, tlv

OID_BER = "2.1.1"
OID_ACSE_AS = "2.2.1.0.1"
OID_MMS_AS = "1.0.9506.2.1"
OID_MMS_AC = "1.0.9506.2.3"

TPDU_SIZES = {7: 128, 8: 256, 9: 512, 10: 1024, 11: 2048, 12: 4096, 13: 8192}


class IsoError(Exception):
    pass


# ---------------------------------------------------------------------------
# TPKT / COTP

async def read_tpkt(reader: asyncio.StreamReader) -> bytes:
    hdr = await reader.readexactly(4)
    if hdr[0] != 3:
        raise IsoError(f"TPKT versão inválida {hdr[0]}")
    ln = int.from_bytes(hdr[2:4], "big")
    if ln < 7:
        raise IsoError("TPKT curto demais")
    return hdr + await reader.readexactly(ln - 4)


def tpkt(payload: bytes) -> bytes:
    return b"\x03\x00" + (len(payload) + 4).to_bytes(2, "big") + payload


@dataclass
class Cotp:
    code: int
    data: bytes = b""
    eot: bool = True
    params: dict = field(default_factory=dict)
    dst_ref: int = 0
    src_ref: int = 0


def parse_cotp(frame: bytes) -> Cotp:
    p = frame[4:]
    li = p[0]
    code = p[1] & 0xF0
    if code == 0xF0:  # DT
        return Cotp(code, p[li + 1:], bool(p[2] & 0x80))
    hdr = p[1:li + 1]
    c = Cotp(code)
    if code in (0xE0, 0xD0, 0x80):
        c.dst_ref = int.from_bytes(hdr[1:3], "big")
        c.src_ref = int.from_bytes(hdr[3:5], "big")
        pos = 6 if code != 0x80 else 6
        while pos + 1 < len(hdr):
            pc, pl = hdr[pos], hdr[pos + 1]
            c.params[pc] = hdr[pos + 2:pos + 2 + pl]
            pos += 2 + pl
    c.data = p[li + 1:]
    return c


def cotp_cr(src_ref: int = 1, tpdu_code: int = 0x0D, called: bytes = b"\x00\x01",
            calling: bytes = b"\x00\x01") -> bytes:
    params = b"\xc0\x01" + bytes([tpdu_code]) + b"\xc1" + bytes([len(calling)]) + calling + \
        b"\xc2" + bytes([len(called)]) + called
    body = b"\xe0\x00\x00" + src_ref.to_bytes(2, "big") + b"\x00" + params
    return tpkt(bytes([len(body)]) + body)


def cotp_cc(cr: Cotp, src_ref: int = 1) -> tuple[bytes, int]:
    code = cr.params.get(0xC0, b"\x07")[0] if 0xC0 in cr.params else 0x07
    if code not in TPDU_SIZES:
        code = 0x0A
    params = b"\xc0\x01" + bytes([code])
    if 0xC1 in cr.params:
        params += b"\xc1" + bytes([len(cr.params[0xC1])]) + cr.params[0xC1]
    if 0xC2 in cr.params:
        params += b"\xc2" + bytes([len(cr.params[0xC2])]) + cr.params[0xC2]
    body = b"\xd0" + cr.src_ref.to_bytes(2, "big") + src_ref.to_bytes(2, "big") + b"\x00" + params
    return tpkt(bytes([len(body)]) + body), TPDU_SIZES[code]


def cotp_dt_frames(data: bytes, tpdu_size: int) -> list[bytes]:
    """Segmenta os dados em DT TPDUs respeitando o tamanho negociado."""
    room = max(tpdu_size - 3, 16)
    frames = []
    pos = 0
    while True:
        chunk = data[pos:pos + room]
        pos += room
        last = pos >= len(data)
        frames.append(tpkt(b"\x02\xf0" + (b"\x80" if last else b"\x00") + chunk))
        if last:
            return frames


def cotp_dr(dst: int, src: int = 1) -> bytes:
    body = b"\x80" + dst.to_bytes(2, "big") + src.to_bytes(2, "big") + b"\x00"
    return tpkt(bytes([len(body)]) + body)


# ---------------------------------------------------------------------------
# Sessão

SPDU_CN, SPDU_AC, SPDU_RF, SPDU_FN, SPDU_DN, SPDU_AB, SPDU_DT = 13, 14, 12, 9, 10, 25, 1


def _slen(n: int) -> bytes:
    return bytes([n]) if n < 255 else b"\xff" + n.to_bytes(2, "big")


def _sparam(pi: int, val: bytes) -> bytes:
    return bytes([pi]) + _slen(len(val)) + val


def _read_slen(b: bytes, pos: int) -> tuple[int, int]:
    if b[pos] == 0xFF:
        return int.from_bytes(b[pos + 1:pos + 3], "big"), pos + 3
    return b[pos], pos + 1


@dataclass
class Spdu:
    si: int
    params: dict
    user_data: bytes = b""


def parse_spdu(data: bytes) -> Spdu:
    if len(data) < 2:
        raise IsoError("SPDU curto")
    si = data[0]
    if si == 1:  # GIVE TOKENS + DATA TRANSFER
        ln, pos = _read_slen(data, 1)
        pos += ln
        if pos < len(data) and data[pos] == 1:
            ln2, pos = _read_slen(data, pos + 1)
            pos += ln2
        return Spdu(SPDU_DT, {}, data[pos:])
    ln, pos = _read_slen(data, 1)
    end = pos + ln
    params: dict = {}
    ud = b""
    while pos < end:
        pi = data[pos]
        pl, pos = _read_slen(data, pos + 1)
        val = data[pos:pos + pl]
        if pi in (0xC1, 0xC2):
            ud = val
        else:
            params[pi] = val
        pos += pl
    if end < len(data) and not ud:  # dados de usuário após o SPDU (pouco comum)
        ud = data[end:]
    return Spdu(si, params, ud)


def spdu_dt(user: bytes) -> bytes:
    return b"\x01\x00\x01\x00" + user


def spdu_cn(user: bytes) -> bytes:
    body = _sparam(5, b"\x13\x01\x00\x16\x01\x02") + _sparam(0x14, b"\x00\x02") + \
        _sparam(0x33, b"\x00\x01") + _sparam(0x34, b"\x00\x01") + _sparam(0xC1, user)
    return bytes([SPDU_CN]) + _slen(len(body)) + body


def spdu_ac(cn: Spdu, user: bytes) -> bytes:
    body = _sparam(5, b"\x13\x01\x00\x16\x01\x02") + _sparam(0x14, cn.params.get(0x14, b"\x00\x02"))
    if 0x34 in cn.params:
        body += _sparam(0x34, cn.params[0x34])
    body += _sparam(0xC1, user)
    return bytes([SPDU_AC]) + _slen(len(body)) + body


def spdu_dn(user: bytes) -> bytes:
    body = _sparam(0xC1, user)
    return bytes([SPDU_DN]) + _slen(len(body)) + body


def spdu_fn(user: bytes) -> bytes:
    body = _sparam(0x11, b"\x09") + _sparam(0xC1, user)
    return bytes([SPDU_FN]) + _slen(len(body)) + body


def spdu_rf() -> bytes:
    body = _sparam(0x32, b"\x00")
    return bytes([SPDU_RF]) + _slen(len(body)) + body


# ---------------------------------------------------------------------------
# Apresentação

@dataclass
class PresConnect:
    calling_sel: bytes = b""
    called_sel: bytes = b""
    contexts: list = field(default_factory=list)  # [(id, abstract_oid)]
    pdvs: list = field(default_factory=list)  # [(pci, data)]

    def ctx_id(self, oid: str, default: int) -> int:
        for cid, aoid in self.contexts:
            if aoid == oid:
                return cid
        return default


def parse_user_data(b: bytes) -> list[tuple[int, bytes]]:
    """Fully-encoded-data [APPLICATION 1] → lista de (pci, dados)."""
    tag, val, _ = dec_tlv(b, 0)
    if tag != 0x61:
        raise IsoError(f"user-data de apresentação inesperado {tag:#x}")
    out = []
    for t, pdv in children(val):
        if t != 0x30:
            continue
        pci = 0
        data = b""
        for t2, v2 in children(pdv):
            if t2 == 0x02:
                pci = int.from_bytes(v2, "big")
            elif t2 in (0xA0, 0x81):
                data = v2
        out.append((pci, data))
    return out


def user_data(pci: int, data: bytes) -> bytes:
    return tlv(0x61, tlv(0x30, tlv(0x02, bytes([pci])) + tlv(0xA0, data)))


def parse_cp(b: bytes) -> PresConnect:
    tag, val, _ = dec_tlv(b, 0)
    if tag != 0x31:
        raise IsoError("CP-type esperado")
    pc = PresConnect()
    m = child_map(val)
    nm = m.get(0xA2)
    if nm is None:
        raise IsoError("apresentação sem normal-mode-parameters")
    for t, v in children(nm):
        if t == 0x81:
            pc.calling_sel = v
        elif t == 0x82:
            pc.called_sel = v
        elif t == 0xA4:
            for t2, item in children(v):
                cm = children(item)
                cid = next((int.from_bytes(x, "big") for tt, x in cm if tt == 0x02), 0)
                oid = next((dec_oid(x) for tt, x in cm if tt == 0x06), "")
                pc.contexts.append((cid, oid))
        elif t == 0x61:
            pc.pdvs = parse_user_data(tlv(0x61, v))
    return pc


def build_cpa(cp: PresConnect, user: bytes, acse_pci: int) -> bytes:
    results = b"".join(tlv(0x30, b"\x80\x01\x00" + tlv(0x81, enc_oid(OID_BER))) for _ in cp.contexts)
    nm = b""
    if cp.called_sel:
        nm += tlv(0x83, cp.called_sel)
    nm += tlv(0xA5, results) + user_data(acse_pci, user)
    return tlv(0x31, tlv(0xA0, b"\x80\x01\x01") + tlv(0xA2, nm))


def build_cp(user: bytes, acse_pci: int = 1, mms_pci: int = 3, called_sel: bytes = b"\x00\x00\x00\x01",
             calling_sel: bytes = b"\x00\x00\x00\x01") -> bytes:
    ts = tlv(0x30, tlv(0x06, enc_oid(OID_BER)))
    ctx = tlv(0x30, tlv(0x02, bytes([acse_pci])) + tlv(0x06, enc_oid(OID_ACSE_AS)) + ts) + \
        tlv(0x30, tlv(0x02, bytes([mms_pci])) + tlv(0x06, enc_oid(OID_MMS_AS)) + ts)
    nm = tlv(0x81, calling_sel) + tlv(0x82, called_sel) + tlv(0xA4, ctx) + \
        tlv(0x88, b"\x06\x00") + user_data(acse_pci, user)
    return tlv(0x31, tlv(0xA0, b"\x80\x01\x01") + tlv(0xA2, nm))


def parse_cpa(b: bytes) -> list[tuple[int, bytes]]:
    tag, val, _ = dec_tlv(b, 0)
    if tag != 0x31:
        raise IsoError("CPA-type esperado")
    nm = child_map(val).get(0xA2, b"")
    for t, v in children(nm):
        if t == 0x61:
            return parse_user_data(tlv(0x61, v))
    return []


# ---------------------------------------------------------------------------
# ACSE

@dataclass
class Aarq:
    app_context: str = ""
    called_ap_title: str = ""
    calling_ap_title: str = ""
    mms: bytes = b""
    auth: bytes = b""


def _external_mms(ext: bytes) -> bytes:
    for t, v in children(ext):
        if t == 0xA0:
            return v
        if t == 0x81:
            return v
    return b""


def parse_aarq(b: bytes) -> Aarq:
    tag, val, _ = dec_tlv(b, 0)
    if tag != 0x60:
        raise IsoError(f"AARQ esperado, recebido {tag:#x}")
    a = Aarq()
    for t, v in children(val):
        try:
            if t == 0xA1:
                a.app_context = dec_oid(dec_tlv(v, 0)[1])
            elif t == 0xA2:
                a.called_ap_title = dec_oid(dec_tlv(v, 0)[1])
            elif t == 0xA6:
                a.calling_ap_title = dec_oid(dec_tlv(v, 0)[1])
            elif t == 0xAC:
                a.auth = v
            elif t == 0xBE:
                for t2, ext in children(v):
                    if t2 == 0x28:
                        a.mms = _external_mms(ext)
        except BerError:
            continue
    return a


def build_aare(mms: bytes, mms_pci: int = 3, accepted: bool = True) -> bytes:
    body = tlv(0xA1, tlv(0x06, enc_oid(OID_MMS_AC)))
    body += tlv(0xA2, tlv(0x02, b"\x00" if accepted else b"\x01"))
    body += tlv(0xA3, tlv(0xA1, tlv(0x02, b"\x00" if accepted else b"\x01")))
    if mms:
        body += tlv(0xBE, tlv(0x28, tlv(0x02, bytes([mms_pci])) + tlv(0xA0, mms)))
    return tlv(0x61, body)


def build_aarq(mms: bytes, mms_pci: int = 3) -> bytes:
    body = tlv(0xA1, tlv(0x06, enc_oid(OID_MMS_AC)))
    body += tlv(0xBE, tlv(0x28, tlv(0x06, enc_oid(OID_BER)) + tlv(0x02, bytes([mms_pci])) + tlv(0xA0, mms)))
    return tlv(0x60, body)


def parse_aare(b: bytes) -> tuple[bool, bytes]:
    tag, val, _ = dec_tlv(b, 0)
    if tag != 0x61:
        raise IsoError("AARE esperado")
    ok = False
    mms = b""
    for t, v in children(val):
        if t == 0xA2:
            ok = dec_tlv(v, 0)[1] == b"\x00"
        elif t == 0xBE:
            for t2, ext in children(v):
                if t2 == 0x28:
                    mms = _external_mms(ext)
    return ok, mms


RLRQ = tlv(0x62, b"\x80\x01\x00")
RLRE = tlv(0x63, b"\x80\x01\x00")
