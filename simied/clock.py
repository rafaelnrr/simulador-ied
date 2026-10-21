"""Relógio por IED (RN-TMP-01 a RN-TMP-06) e qualidade de ponto (seção 8)."""
from __future__ import annotations

import datetime as dt
import time

from .mms.types import Bits, UtcTime

TQ_LEAP_KNOWN = 0x80
TQ_CLOCK_FAILURE = 0x40
TQ_NOT_SYNC = 0x20


class IedClock:
    def __init__(self, cfg: dict | None = None):
        self.mode = "sistema"  # sistema | deslocamento | congelado
        self.offset_s = 0.0
        self.frozen_at = 0.0
        self.tq = 0x8B
        self.falha = False
        if cfg:
            self.update(cfg)

    def update(self, cfg: dict):
        if "modo" in cfg:
            if cfg["modo"] not in ("sistema", "deslocamento", "congelado"):
                raise ValueError("modo de relógio inválido (sistema, deslocamento ou congelado)")
            self.mode = cfg["modo"]
        if "deslocamento_s" in cfg:
            self.offset_s = float(cfg["deslocamento_s"])
        elif any(k in cfg for k in ("horas", "minutos", "segundos")):
            sign = -1 if cfg.get("negativo") else 1
            self.offset_s = sign * (abs(float(cfg.get("horas", 0))) * 3600 + abs(float(cfg.get("minutos", 0))) * 60
                                    + abs(float(cfg.get("segundos", 0))))
        if "congelado_em" in cfg and cfg["congelado_em"]:
            v = cfg["congelado_em"]
            if isinstance(v, (int, float)):
                self.frozen_at = float(v)
            else:
                d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.astimezone()  # hora local da máquina
                self.frozen_at = d.timestamp()
        if "qualidade_t" in cfg:
            q = cfg["qualidade_t"]
            self.tq = int(q, 0) if isinstance(q, str) else int(q)
        if "falha" in cfg:
            self.falha = bool(cfg["falha"])

    def now(self) -> float:
        if self.mode == "congelado":
            return self.frozen_at
        if self.mode == "deslocamento":
            return time.time() + self.offset_s
        return time.time()

    def tq_byte(self) -> int:
        q = self.tq
        if self.falha:
            q |= TQ_CLOCK_FAILURE | TQ_NOT_SYNC
        return q & 0xFF

    def stamp(self) -> UtcTime:
        return UtcTime.from_float(self.now(), self.tq_byte())

    def to_json(self) -> dict:
        now = self.now()
        sys_now = time.time()
        return {
            "modo": self.mode,
            "deslocamento_s": self.offset_s,
            "congelado_em": self.frozen_at,
            "qualidade_t": f"0x{self.tq:02x}",
            "qualidade_t_efetiva": f"0x{self.tq_byte():02x}",
            "falha": self.falha,
            "hora_local_maquina": dt.datetime.fromtimestamp(sys_now).astimezone().isoformat(timespec="milliseconds"),
            "hora_utc": dt.datetime.fromtimestamp(sys_now, dt.timezone.utc).isoformat(timespec="milliseconds"),
            "hora_ied_utc": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec="milliseconds"),
            "diferenca_ied_s": round(now - sys_now, 3),
        }

    def config(self) -> dict:
        return {"modo": self.mode, "deslocamento_s": self.offset_s, "congelado_em": self.frozen_at,
                "qualidade_t": f"0x{self.tq:02x}", "falha": self.falha}


# ---------------------------------------------------------------------------
# Qualidade (bit-string de 13 bits)

VALIDITY = {"good": 0, "invalid": 1, "reserved": 2, "questionable": 3}
VALIDITY_R = {v: k for k, v in VALIDITY.items()}
DETAIL = ["overflow", "outOfRange", "badReference", "oscillatory", "failure", "oldData", "inconsistent",
          "inaccurate"]


def q_from_dict(d: dict, base: Bits | None = None) -> Bits:
    q = base if base is not None else Bits(13, 0)
    if "validity" in d:
        v = VALIDITY.get(d["validity"])
        if v is None:
            raise ValueError(f"validity inválida: {d['validity']}")
        q = q.set(0, bool(v & 2)).set(1, bool(v & 1))
    det = d.get("detailQual")
    if det is not None:
        if isinstance(det, dict):
            det = [k for k, on in det.items() if on]
        for i, name in enumerate(DETAIL):
            q = q.set(2 + i, name in det)
    if "source" in d:
        if d["source"] not in ("process", "substituted"):
            raise ValueError("source deve ser process ou substituted")
        q = q.set(10, d["source"] == "substituted")
    if "test" in d:
        q = q.set(11, bool(d["test"]))
    if "operatorBlocked" in d:
        q = q.set(12, bool(d["operatorBlocked"]))
    return q


def q_to_dict(q: Bits | None) -> dict | None:
    if q is None:
        return None
    v = (2 if q.get(0) else 0) | (1 if q.get(1) else 0)
    return {
        "validity": VALIDITY_R[v],
        "detailQual": [n for i, n in enumerate(DETAIL) if q.get(2 + i)],
        "source": "substituted" if q.get(10) else "process",
        "test": q.get(11),
        "operatorBlocked": q.get(12),
        "hex": q.encode_value().hex(" "),
    }
