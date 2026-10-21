"""Exportação de PCAP sintético a partir dos quadros TPKT capturados (seção 10/14)."""
from __future__ import annotations

import ipaddress
import struct


def _csum(b: bytes) -> int:
    if len(b) % 2:
        b += b"\x00"
    s = sum(struct.unpack(f">{len(b) // 2}H", b))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def _ip4(ip: str) -> bytes:
    try:
        return ipaddress.IPv4Address(ip).packed
    except ValueError:
        return b"\x7f\x00\x00\x01"


def _frame(src: tuple, dst: tuple, seq: int, ack: int, flags: int, payload: bytes) -> bytes:
    tcp = struct.pack(">HHIIBBHHH", src[1], dst[1], seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, 5 << 4, flags, 65535, 0, 0)
    tot = 20 + len(tcp) + len(payload)
    iph = struct.pack(">BBHHHBBH4s4s", 0x45, 0, tot, 0, 0x4000, 64, 6, 0, _ip4(src[0]), _ip4(dst[0]))
    iph = iph[:10] + struct.pack(">H", _csum(iph)) + iph[12:]
    pseudo = _ip4(src[0]) + _ip4(dst[0]) + struct.pack(">BBH", 0, 6, len(tcp) + len(payload))
    c = _csum(pseudo + tcp + payload)
    tcp = tcp[:16] + struct.pack(">H", c) + tcp[18:]
    eth = b"\x02\x00\x00\x00\x00\x02" + b"\x02\x00\x00\x00\x00\x01" + b"\x08\x00"
    return eth + iph + tcp + payload


def build_pcap(frames: list[tuple]) -> bytes:
    """frames: [(ts, 'rx'|'tx', id_sessao, (ip_cli, porta_cli), (ip_srv, porta_srv), bytes)]"""
    out = [struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)]
    seqs: dict = {}

    def rec(ts: float, data: bytes):
        sec = int(ts)
        usec = int((ts - sec) * 1_000_000)
        out.append(struct.pack("<IIII", sec, usec, len(data), len(data)) + data)

    for ts, direction, sid, cli, srv, data in frames:
        st = seqs.get(sid)
        if st is None:
            st = seqs[sid] = {"c": 1000, "s": 5000}
            rec(ts, _frame(cli, srv, st["c"] - 1, 0, 0x02, b""))
            rec(ts, _frame(srv, cli, st["s"] - 1, st["c"], 0x12, b""))
            rec(ts, _frame(cli, srv, st["c"], st["s"], 0x10, b""))
        if direction == "rx":
            rec(ts, _frame(cli, srv, st["c"], st["s"], 0x18, data))
            st["c"] += len(data)
        else:
            rec(ts, _frame(srv, cli, st["s"], st["c"], 0x18, data))
            st["s"] += len(data)
    return b"".join(out)
