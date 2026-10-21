"""Codificação e decodificação BER mínima usada pela pilha ISO/MMS."""
from __future__ import annotations

import struct


class BerError(Exception):
    pass


def enc_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag: int | bytes, value: bytes = b"") -> bytes:
    t = tag if isinstance(tag, bytes) else bytes([tag])
    return t + enc_len(len(value)) + value


def enc_int_value(v: int) -> bytes:
    """Inteiro com sinal em complemento de dois, forma mínima."""
    n = 1
    while True:
        try:
            return v.to_bytes(n, "big", signed=True)
        except OverflowError:
            n += 1


def enc_uint_value(v: int) -> bytes:
    """Inteiro sem sinal: complemento de dois com zero à esquerda quando preciso."""
    if v < 0:
        raise BerError("valor negativo para unsigned")
    return enc_int_value(v)


def dec_int(b: bytes) -> int:
    if not b:
        return 0
    return int.from_bytes(b, "big", signed=True)


def dec_uint(b: bytes) -> int:
    return int.from_bytes(b, "big", signed=False)


def int_tlv(tag: int, v: int) -> bytes:
    return tlv(tag, enc_int_value(v))


def uint_tlv(tag: int, v: int) -> bytes:
    return tlv(tag, enc_uint_value(v))


def bool_tlv(tag: int, v: bool) -> bytes:
    return tlv(tag, b"\xff" if v else b"\x00")


def str_tlv(tag: int, s: str) -> bytes:
    return tlv(tag, s.encode("latin-1", errors="replace"))


def dec_tag(buf: bytes, pos: int) -> tuple[int, int]:
    """Lê uma tag (1 ou mais bytes). Retorna (tag, nova posição).

    Tags de número alto são devolvidas como (primeiro_byte << 16) | número.
    """
    if pos >= len(buf):
        raise BerError("fim inesperado ao ler tag")
    t = buf[pos]
    pos += 1
    if t & 0x1F != 0x1F:
        return t, pos
    num = 0
    while True:
        if pos >= len(buf):
            raise BerError("tag longa truncada")
        b = buf[pos]
        pos += 1
        num = (num << 7) | (b & 0x7F)
        if not b & 0x80:
            break
    return (t << 16) | num, pos


def dec_len(buf: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(buf):
        raise BerError("fim inesperado ao ler comprimento")
    b = buf[pos]
    pos += 1
    if b < 0x80:
        return b, pos
    n = b & 0x7F
    if n == 0 or n > 4 or pos + n > len(buf):
        raise BerError("comprimento BER inválido")
    return int.from_bytes(buf[pos:pos + n], "big"), pos + n


def dec_tlv(buf: bytes, pos: int = 0) -> tuple[int, bytes, int]:
    """Retorna (tag, valor, posição após o TLV)."""
    tag, pos = dec_tag(buf, pos)
    ln, pos = dec_len(buf, pos)
    end = pos + ln
    if end > len(buf):
        raise BerError(f"TLV truncado (tag {tag:#x}, len {ln}, disponível {len(buf) - pos})")
    return tag, buf[pos:end], end


def children(buf: bytes) -> list[tuple[int, bytes]]:
    out = []
    pos = 0
    while pos < len(buf):
        tag, val, pos = dec_tlv(buf, pos)
        out.append((tag, val))
    return out


def child_map(buf: bytes) -> dict[int, bytes]:
    """Primeira ocorrência de cada tag."""
    out: dict[int, bytes] = {}
    for t, v in children(buf):
        out.setdefault(t, v)
    return out


def enc_oid(oid: str) -> bytes:
    parts = [int(p) for p in oid.split(".")]
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out.extend(reversed(chunk))
    return bytes(out)


def dec_oid(b: bytes) -> str:
    if not b:
        return ""
    parts = [b[0] // 40, b[0] % 40]
    v = 0
    for x in b[1:]:
        v = (v << 7) | (x & 0x7F)
        if not x & 0x80:
            parts.append(v)
            v = 0
    return ".".join(str(p) for p in parts)


def enc_float32(v: float) -> bytes:
    return b"\x08" + struct.pack(">f", v)


def enc_float64(v: float) -> bytes:
    return b"\x0b" + struct.pack(">d", v)


def dec_float(b: bytes) -> float:
    if len(b) == 5:
        return struct.unpack(">f", b[1:])[0]
    if len(b) == 9:
        return struct.unpack(">d", b[1:])[0]
    raise BerError("floating-point com tamanho inválido")


def hexs(b: bytes) -> str:
    return b.hex(" ")
