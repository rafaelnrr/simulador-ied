"""PDUs MMS (ISO 9506): leitura e montagem dos serviços usados pelo IEC 61850-8-1."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..ber import (BerError, bool_tlv, child_map, children, dec_int, dec_tlv, dec_uint, int_tlv, str_tlv,
                   tlv, uint_tlv)
from .types import Bits

# Tags do MMSpdu
PDU_CONF_REQ, PDU_CONF_RESP, PDU_CONF_ERR, PDU_UNCONF, PDU_REJECT = 0xA0, 0xA1, 0xA2, 0xA3, 0xA4
PDU_INIT_REQ, PDU_INIT_RESP, PDU_INIT_ERR, PDU_CONCL_REQ, PDU_CONCL_RESP = 0xA8, 0xA9, 0xAA, 0x8B, 0x8C

# Serviços confirmados (tag dentro do confirmed-RequestPDU)
SRV_STATUS, SRV_GETNAMELIST, SRV_IDENTIFY, SRV_READ, SRV_WRITE, SRV_GETVARATTR = 0, 1, 2, 4, 5, 6
SRV_DEFNVL, SRV_GETNVLATTR, SRV_DELNVL = 11, 12, 13

SERVICE_NAMES = {0: "Status", 1: "GetNameList", 2: "Identify", 4: "Read", 5: "Write",
                 6: "GetVariableAccessAttributes", 11: "DefineNamedVariableList",
                 12: "GetNamedVariableListAttributes", 13: "DeleteNamedVariableList"}

# Bits de ServiceSupportOptions
SSO_BITS = {"status": 0, "getNameList": 1, "identify": 2, "read": 4, "write": 5,
            "getVariableAccessAttributes": 6, "defineNamedVariableList": 11,
            "getNamedVariableListAttributes": 12, "deleteNamedVariableList": 13,
            "informationReport": 79, "conclude": 83, "cancel": 84}


def services_supported(names) -> bytes:
    b = Bits.from_list(85, [SSO_BITS[n] for n in names if n in SSO_BITS])
    return b.encode_value()


PARAM_CBB = bytes([0x05, 0xF1, 0x00])  # str1, str2, vnam, valt, vlis


@dataclass(frozen=True)
class ObjName:
    scope: str  # vmd | dom | aa
    domain: str = ""
    item: str = ""

    def text(self) -> str:
        return f"{self.domain}/{self.item}" if self.scope == "dom" else f"[{self.scope}]{self.item}"


def parse_objname(tag: int, val: bytes) -> ObjName:
    if tag == 0x80:
        return ObjName("vmd", "", val.decode("latin-1"))
    if tag == 0xA1:
        c = children(val)
        if len(c) < 2:
            raise BerError("domain-specific incompleto")
        return ObjName("dom", c[0][1].decode("latin-1"), c[1][1].decode("latin-1"))
    if tag == 0x82:
        return ObjName("aa", "", val.decode("latin-1"))
    raise BerError(f"ObjectName com tag inesperada {tag:#x}")


def enc_objname(o: ObjName) -> bytes:
    if o.scope == "vmd":
        return str_tlv(0x80, o.item)
    if o.scope == "aa":
        return str_tlv(0x82, o.item)
    return tlv(0xA1, str_tlv(0x1A, o.domain) + str_tlv(0x1A, o.item))


@dataclass
class VarSpec:
    name: ObjName | None
    alt: bytes | None = None


@dataclass
class VarAccess:
    kind: str  # list | nvl
    vars: list = field(default_factory=list)  # [VarSpec]
    nvl: ObjName | None = None
    raw: bytes = b""


def parse_var_access(tag: int, val: bytes) -> VarAccess:
    raw = tlv(tag, val)
    if tag == 0xA0:
        vs = []
        for t, item in children(val):
            name = None
            alt = None
            for t2, v2 in children(item):
                if t2 == 0xA0:
                    t3, v3, _ = dec_tlv(v2, 0)
                    name = parse_objname(t3, v3)
                elif t2 == 0xA5:
                    alt = v2
            vs.append(VarSpec(name, alt))
        return VarAccess("list", vs, raw=raw)
    if tag == 0xA1:
        t3, v3, _ = dec_tlv(val, 0)
        return VarAccess("nvl", nvl=parse_objname(t3, v3), raw=raw)
    raise BerError(f"VariableAccessSpecification inesperada {tag:#x}")


def enc_var_list(names: list[ObjName]) -> bytes:
    return tlv(0xA0, b"".join(tlv(0x30, tlv(0xA0, enc_objname(n))) for n in names))


def enc_nvl_spec(name: ObjName) -> bytes:
    return tlv(0xA1, enc_objname(name))


# ---------------------------------------------------------------------------
# Initiate

@dataclass
class InitParams:
    local_detail: int = 65000
    max_serv_calling: int = 5
    max_serv_called: int = 5
    nesting: int = 10
    version: int = 1
    cbb: bytes = PARAM_CBB
    services: bytes = b""


def parse_initiate(val: bytes) -> InitParams:
    p = InitParams()
    for t, v in children(val):
        if t == 0x80:
            p.local_detail = dec_uint(v)
        elif t == 0x81:
            p.max_serv_calling = dec_uint(v)
        elif t == 0x82:
            p.max_serv_called = dec_uint(v)
        elif t == 0x83:
            p.nesting = dec_uint(v)
        elif t == 0xA4:
            for t2, v2 in children(v):
                if t2 == 0x80:
                    p.version = dec_uint(v2)
                elif t2 == 0x81:
                    p.cbb = v2
                elif t2 == 0x82:
                    p.services = v2
    return p


def enc_initiate(tag: int, p: InitParams) -> bytes:
    detail = uint_tlv(0x80, p.version) + tlv(0x81, p.cbb) + tlv(0x82, p.services)
    return tlv(tag, int_tlv(0x80, p.local_detail) + int_tlv(0x81, p.max_serv_calling) +
               int_tlv(0x82, p.max_serv_called) + int_tlv(0x83, p.nesting) + tlv(0xA4, detail))


# ---------------------------------------------------------------------------
# Envelopes

def conf_resp(invoke: int, service_tlv: bytes) -> bytes:
    return tlv(PDU_CONF_RESP, uint_tlv(0x02, invoke) + service_tlv)


def conf_req(invoke: int, service_tlv: bytes) -> bytes:
    return tlv(PDU_CONF_REQ, uint_tlv(0x02, invoke) + service_tlv)


ERRCLASS = {"vmd-state": 0, "application-reference": 1, "definition": 2, "resource": 3, "service": 4,
            "service-preempt": 5, "time-resolution": 6, "access": 7, "initiate": 8, "conclude": 9,
            "cancel": 10, "file": 11, "others": 12}


def conf_error(invoke: int, err_class: str, code: int) -> bytes:
    ec = tlv(0x80 | ERRCLASS[err_class], bytes([code]))
    return tlv(PDU_CONF_ERR, uint_tlv(0x80, invoke) + tlv(0xA2, tlv(0xA0, ec)))


def reject(invoke: int | None, reason_tag: int = 0x81, code: int = 1) -> bytes:
    body = uint_tlv(0x80, invoke) if invoke is not None else b""
    return tlv(PDU_REJECT, body + tlv(reason_tag, bytes([code])))


def info_report(var_name: str, results: list[bytes]) -> bytes:
    """unconfirmed-PDU com informationReport (variável vmd-specific, ex. 'RPT')."""
    spec = tlv(0xA1, str_tlv(0x80, var_name))
    return tlv(PDU_UNCONF, tlv(0xA0, spec + tlv(0xA0, b"".join(results))))


def access_failure(code: int) -> bytes:
    return tlv(0x80, bytes([code]))


@dataclass
class ConfirmedReq:
    invoke: int
    service: int
    body: bytes
    service_tag: int


def parse_confirmed(val: bytes) -> ConfirmedReq:
    c = children(val)
    if not c or c[0][0] != 0x02:
        raise BerError("confirmed-RequestPDU sem invokeID")
    invoke = dec_uint(c[0][1])
    for t, v in c[1:]:
        if t == 0x30:  # listOfModifiers
            continue
        num = t & 0x1F if t < 0x10000 else t & 0xFFFF
        return ConfirmedReq(invoke, num, v, t)
    raise BerError("confirmed-RequestPDU sem serviço")


# ---------------------------------------------------------------------------
# Lado cliente: montagem de requisições e leitura de respostas

def req_read(names: list[ObjName], with_result: bool = False) -> bytes:
    body = (bool_tlv(0x80, True) if with_result else b"") + tlv(0xA1, enc_var_list(names))
    return tlv(0xA4, body)


def req_read_nvl(name: ObjName) -> bytes:
    return tlv(0xA4, tlv(0xA1, enc_nvl_spec(name)))


def req_write(names: list[ObjName], datas: list[bytes]) -> bytes:
    return tlv(0xA5, enc_var_list(names) + tlv(0xA0, b"".join(datas)))


def req_getnamelist(obj_class: int, domain: str | None, cont: str | None = None) -> bytes:
    scope = str_tlv(0x81, domain) if domain is not None else tlv(0x80, b"")
    body = tlv(0xA0, tlv(0x80, bytes([obj_class]))) + tlv(0xA1, scope)
    if cont:
        body += str_tlv(0x82, cont)
    return tlv(0xA1, body)


def req_getvarattr(name: ObjName) -> bytes:
    return tlv(0xA6, tlv(0xA0, enc_objname(name)))


def req_getnvlattr(name: ObjName) -> bytes:
    return tlv(0xAC, enc_objname(name))


def req_defnvl(name: ObjName, members: list[ObjName]) -> bytes:
    return tlv(0xAB, enc_objname(name) + enc_var_list(members))


def req_identify() -> bytes:
    return tlv(0x82, b"")


def parse_namelist_resp(val: bytes) -> tuple[list[str], bool]:
    m = child_map(val)
    names = [v.decode("latin-1") for t, v in children(m.get(0xA0, b""))]
    more = True
    if 0x81 in m:
        more = bool(m[0x81] and m[0x81][0])
    return names, more


def parse_write_resp(val: bytes) -> list[int | None]:
    out: list[int | None] = []
    for t, v in children(val):
        out.append(None if t == 0x81 else (v[0] if v else -1))
    return out


def dec_unsigned(b: bytes) -> int:
    return dec_uint(b)


def dec_signed(b: bytes) -> int:
    return dec_int(b)
