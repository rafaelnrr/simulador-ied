"""Tipos MMS, valores (Data) e suas codificações BER."""
from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass, field

from ..ber import (BerError, children, dec_float, dec_int, dec_tlv, dec_uint, enc_float32,
                   enc_float64, enc_int_value, enc_uint_value, int_tlv, tlv)

# Tags do CHOICE Data (ISO 9506-2)
T_ARRAY, T_STRUCT, T_BOOL, T_BITS, T_INT, T_UINT, T_FLOAT = 0xA1, 0xA2, 0x83, 0x84, 0x85, 0x86, 0x87
T_OCTET, T_VIS, T_GTIME, T_BTIME, T_MMSSTR, T_UTC = 0x89, 0x8A, 0x8B, 0x8C, 0x90, 0x91

# DataAccessError
ERR_OBJECT_INVALIDATED = 0
ERR_HW_FAULT = 1
ERR_TEMP_UNAVAILABLE = 2
ERR_ACCESS_DENIED = 3
ERR_OBJECT_UNDEFINED = 4
ERR_INVALID_ADDRESS = 5
ERR_TYPE_UNSUPPORTED = 6
ERR_TYPE_INCONSISTENT = 7
ERR_ATTR_INCONSISTENT = 8
ERR_ACCESS_UNSUPPORTED = 9
ERR_NON_EXISTENT = 10
ERR_VALUE_INVALID = 11


class AccessError(Exception):
    def __init__(self, code: int, msg: str = ""):
        super().__init__(msg or f"DataAccessError {code}")
        self.code = code


@dataclass(frozen=True)
class Bits:
    """Bit-string: bit 0 é o mais significativo do primeiro byte."""
    n: int
    val: int = 0

    def get(self, i: int) -> bool:
        return bool((self.val >> (self.n - 1 - i)) & 1)

    def set(self, i: int, on: bool = True) -> "Bits":
        m = 1 << (self.n - 1 - i)
        return Bits(self.n, (self.val | m) if on else (self.val & ~m))

    @staticmethod
    def from_list(n: int, idxs) -> "Bits":
        b = Bits(n, 0)
        for i in idxs:
            b = b.set(i)
        return b

    def encode_value(self) -> bytes:
        nbytes = (self.n + 7) // 8
        unused = nbytes * 8 - self.n
        return bytes([unused]) + (self.val << unused).to_bytes(nbytes, "big") if nbytes else b"\x00"

    @staticmethod
    def decode_value(b: bytes) -> "Bits":
        if not b:
            raise BerError("bit-string vazio")
        unused = b[0]
        data = b[1:]
        n = len(data) * 8 - unused
        v = int.from_bytes(data, "big") >> unused if data else 0
        return Bits(n, v)

    def bitstr(self) -> str:
        return "".join("1" if self.get(i) else "0" for i in range(self.n))


@dataclass(frozen=True)
class UtcTime:
    """utc-time MMS: 4 bytes de segundos, 3 de fração (1/2^24 s) e 1 de qualidade."""
    secs: int = 0
    frac: int = 0
    tq: int = 0

    @staticmethod
    def from_float(ts: float, tq: int = 0x8B) -> "UtcTime":
        s = int(ts)
        f = int(round((ts - s) * (1 << 24)))
        if f >= (1 << 24):
            s, f = s + 1, 0
        return UtcTime(s & 0xFFFFFFFF, f, tq)

    def to_float(self) -> float:
        return self.secs + self.frac / (1 << 24)

    def encode_value(self) -> bytes:
        return struct.pack(">I", self.secs & 0xFFFFFFFF) + self.frac.to_bytes(3, "big") + bytes([self.tq & 0xFF])

    @staticmethod
    def decode_value(b: bytes) -> "UtcTime":
        if len(b) != 8:
            raise BerError("utc-time deve ter 8 bytes")
        return UtcTime(struct.unpack(">I", b[:4])[0], int.from_bytes(b[4:7], "big"), b[7])

    def iso(self) -> str:
        if self.secs == 0 and self.frac == 0:
            return ""
        d = _dt.datetime.fromtimestamp(self.to_float(), _dt.timezone.utc)
        return d.isoformat(timespec="milliseconds").replace("+00:00", "Z")


EPOCH_1984 = _dt.datetime(1984, 1, 1, tzinfo=_dt.timezone.utc).timestamp()


def btime6(ts: float) -> bytes:
    """binary-time (TimeOfDay com data): ms desde meia-noite + dias desde 1984-01-01."""
    rel = ts - EPOCH_1984
    days = int(rel // 86400)
    ms = int(round((rel - days * 86400) * 1000))
    if ms >= 86400000:
        days, ms = days + 1, ms - 86400000
    return struct.pack(">IH", ms, max(days, 0))


def btime6_to_float(b: bytes) -> float:
    if len(b) == 6:
        ms, days = struct.unpack(">IH", b)
        return EPOCH_1984 + days * 86400 + ms / 1000.0
    if len(b) == 4:
        return struct.unpack(">I", b)[0] / 1000.0
    return 0.0


# ---------------------------------------------------------------------------
# Tipos MMS

@dataclass
class MmsType:
    kind: str  # struct, array, bool, bits, int, uint, float, octet, vis, mmsstr, utc, btime
    size: int = 0  # bits (int/uint/float/bits), tamanho (octet/vis; negativo = variável)
    comps: list[tuple[str, "MmsType"]] = field(default_factory=list)
    elem: "MmsType | None" = None
    count: int = 0

    def default(self):
        k = self.kind
        if k == "struct":
            return [t.default() for _, t in self.comps]
        if k == "array":
            return [self.elem.default() for _ in range(self.count)]
        if k == "bool":
            return False
        if k == "bits":
            return Bits(abs(self.size), 0)
        if k in ("int", "uint"):
            return 0
        if k == "float":
            return 0.0
        if k == "octet":
            return bytes(self.size) if self.size > 0 else b""
        if k in ("vis", "mmsstr"):
            return ""
        if k == "utc":
            return UtcTime()
        if k == "btime":
            return bytes(6)
        return None

    # TypeSpecification (GetVariableAccessAttributes)
    def type_spec(self) -> bytes:
        k = self.kind
        if k == "struct":
            comps = b"".join(
                tlv(0x30, tlv(0x80, n.encode()) + tlv(0xA1, t.type_spec())) for n, t in self.comps)
            return tlv(0xA2, tlv(0xA1, comps))
        if k == "array":
            return tlv(0xA1, int_tlv(0x81, self.count) + tlv(0xA2, self.elem.type_spec()))
        if k == "bool":
            return tlv(0x83, b"")
        if k == "bits":
            return int_tlv(0x84, self.size)
        if k == "int":
            return tlv(0x85, bytes([self.size]))
        if k == "uint":
            return tlv(0x86, bytes([self.size]))
        if k == "float":
            if self.size == 64:
                return tlv(0xA7, tlv(0x02, b"\x40") + tlv(0x02, b"\x0b"))
            return tlv(0xA7, tlv(0x02, b"\x20") + tlv(0x02, b"\x08"))
        if k == "octet":
            return int_tlv(0x89, self.size)
        if k == "vis":
            return int_tlv(0x8A, self.size)
        if k == "mmsstr":
            return int_tlv(0x90, self.size)
        if k == "utc":
            return tlv(0x91, b"")
        if k == "btime":
            return tlv(0x8C, b"\xff")
        raise BerError(f"tipo sem TypeSpecification: {k}")


def T(kind: str, size: int = 0) -> MmsType:
    return MmsType(kind, size)


# ---------------------------------------------------------------------------
# Codificação de valores

def enc_data(t: MmsType, v) -> bytes:
    k = t.kind
    if k == "struct":
        return tlv(T_STRUCT, b"".join(enc_data(ct, cv) for (_, ct), cv in zip(t.comps, v)))
    if k == "array":
        return tlv(T_ARRAY, b"".join(enc_data(t.elem, x) for x in v))
    if k == "bool":
        return tlv(T_BOOL, b"\xff" if v else b"\x00")
    if k == "bits":
        if not isinstance(v, Bits):
            v = Bits(abs(t.size), int(v))
        return tlv(T_BITS, v.encode_value())
    if k == "int":
        return tlv(T_INT, enc_int_value(int(v)))
    if k == "uint":
        return tlv(T_UINT, enc_uint_value(int(v)))
    if k == "float":
        return tlv(T_FLOAT, enc_float64(float(v)) if t.size == 64 else enc_float32(float(v)))
    if k == "octet":
        return tlv(T_OCTET, bytes(v))
    if k == "vis":
        return tlv(T_VIS, str(v).encode("latin-1", errors="replace"))
    if k == "mmsstr":
        return tlv(T_MMSSTR, str(v).encode("utf-8"))
    if k == "utc":
        return tlv(T_UTC, (v or UtcTime()).encode_value())
    if k == "btime":
        return tlv(T_BTIME, bytes(v))
    raise BerError(f"tipo não codificável: {k}")


def dec_data_raw(tag: int, val: bytes):
    """Decodifica Data sem conhecer o tipo. Retorna (kind, valor)."""
    if tag == T_STRUCT:
        return "struct", [dec_data_raw(t, v) for t, v in children(val)]
    if tag == T_ARRAY:
        return "array", [dec_data_raw(t, v) for t, v in children(val)]
    if tag == T_BOOL:
        return "bool", bool(val and val[0])
    if tag == T_BITS:
        return "bits", Bits.decode_value(val)
    if tag == T_INT:
        return "int", dec_int(val)
    if tag == T_UINT:
        return "uint", dec_uint(val)
    if tag == T_FLOAT:
        return "float", dec_float(val)
    if tag == T_OCTET:
        return "octet", bytes(val)
    if tag == T_VIS:
        return "vis", val.decode("latin-1")
    if tag == T_MMSSTR:
        return "mmsstr", val.decode("utf-8", errors="replace")
    if tag == T_UTC:
        return "utc", UtcTime.decode_value(val)
    if tag == T_BTIME:
        return "btime", bytes(val)
    return f"tag{tag:#x}", bytes(val)


def dec_data(buf: bytes, pos: int = 0):
    tag, val, end = dec_tlv(buf, pos)
    return dec_data_raw(tag, val), end


def coerce(t: MmsType, raw) -> object:
    """Converte um valor decodificado (kind, v) para o tipo t; erro 7 se incompatível."""
    kind, v = raw
    k = t.kind
    ok = (kind == k or (k in ("int", "uint") and kind in ("int", "uint"))
          or (k == "vis" and kind in ("vis", "mmsstr")))
    if not ok:
        raise AccessError(ERR_TYPE_INCONSISTENT, f"esperado {k}, recebido {kind}")
    if k == "struct":
        if len(v) != len(t.comps):
            raise AccessError(ERR_TYPE_INCONSISTENT, "estrutura com número de componentes diferente")
        return [coerce(ct, cv) for (_, ct), cv in zip(t.comps, v)]
    if k == "array":
        return [coerce(t.elem, x) for x in v]
    if k == "bits":
        n = abs(t.size)
        if t.size > 0 and v.n != n:
            # aceita tamanho diferente ajustando (alguns clientes mandam OptFlds com 16 bits)
            val = v.val >> (v.n - n) if v.n > n else v.val << (n - v.n)
            return Bits(n, val & ((1 << n) - 1))
        return v
    if k == "uint" and v < 0:
        raise AccessError(ERR_TYPE_INCONSISTENT, "valor negativo para unsigned")
    if k == "vis" and t.size and len(v) > abs(t.size):
        raise AccessError(ERR_TYPE_INCONSISTENT, "texto maior que o tipo")
    return v


def raw_to_json(raw):
    kind, v = raw
    if kind in ("struct", "array"):
        return [raw_to_json(x) for x in v]
    if kind == "bits":
        return {"bits": v.bitstr()}
    if kind == "utc":
        return {"utc": v.iso(), "tq": v.tq}
    if kind in ("octet", "btime"):
        return v.hex()
    return v


def value_to_json(t: MmsType, v):
    k = t.kind
    if k == "struct":
        return {n: value_to_json(ct, cv) for (n, ct), cv in zip(t.comps, v)}
    if k == "array":
        return [value_to_json(t.elem, x) for x in v]
    if k == "bits":
        return v.bitstr()
    if k == "utc":
        return v.iso()
    if k in ("octet", "btime"):
        return bytes(v).hex()
    return v
