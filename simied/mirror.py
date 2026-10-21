"""Vínculo de pontos com o relé real (seção 5, RN-DIG-04 a RN-DIG-07, RN-SEG-02, RN-SEG-06).

O cliente MMS do vínculo é somente leitura: associa e faz Read. Por RN-SEG-02 ele não escreve
nada no relé, inclusive atributos de RCB; por isso a leitura remota é sempre periódica
(o modo "por report" de RN-DIG-05 exigiria habilitar um RCB no relé real, o que é escrita).
"""
from __future__ import annotations

import asyncio
import logging
import time

from .mms.client import MmsClient
from .mms.pdu import ObjName
from .mms.types import Bits, UtcTime

log = logging.getLogger("simied.mirror")


def remote_parts(ied, p, link: dict) -> tuple[str, str]:
    """Domínio e item remotos do DO do ponto. Padrão: mesmo caminho, trocando o nome do IED."""
    path = (link.get("caminho") or "").strip()
    if path:
        if "/" in path:
            dom, item = path.split("/", 1)
            return dom, item.replace(".", "$")
        return f"{link.get('ied') or ied.ied_name}{ied.domains[p.domain].inst}", path.replace(".", "$")
    remote_ied = link.get("ied") or ied.ied_name
    return f"{remote_ied}{ied.domains[p.domain].inst}", p.ln_path


def _leaf_names(p) -> list[tuple[str, str]]:
    base = len(p.do_node.path) + 1
    out = []
    for role, leaf in (("v", p.v), ("a", p.a), ("q", p.q), ("t", p.t)):
        if leaf is not None:
            out.append((role, leaf.path[base:]))
    return out


class MirrorGroup:
    def __init__(self, mgr: "MirrorManager", ip: str, port: int):
        self.mgr = mgr
        self.ip = ip
        self.port = port
        self.points: list = []
        self.interval = 1.0
        self.client: MmsClient | None = None
        self.task: asyncio.Task | None = None
        self.state = "parado"
        self.error = ""
        self.last_update = 0.0
        self.last: dict = {}
        self.lost = False

    def json(self) -> dict:
        return {"ip": self.ip, "porta": self.port, "estado": self.state, "erro": self.error,
                "ultima_atualizacao": self.last_update, "intervalo_s": self.interval,
                "pontos": [p.path for p in self.points]}

    def set_state(self, st: str, err: str = ""):
        if st != self.state or err != self.error:
            self.state, self.error = st, err
            self.mgr.ied.emit("link.state", self.json())

    async def run(self):
        ied = self.mgr.ied
        while True:
            try:
                if self.client is None or not self.client.connected:
                    self.set_state("conectando")
                    self.client = MmsClient(self.ip, self.port, timeout=5.0, read_only=True)
                    await self.client.connect()
                    self.set_state("conectado")
                await self.poll()
                if self.lost:
                    self.lost = False
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                self.set_state("erro", str(e) or e.__class__.__name__)
                if not self.lost:
                    self.lost = True
                    for p in self.points:
                        ied.apply_mirror(p, None, None, None, lost=True)
                if self.client is not None:
                    try:
                        await self.client.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.client = None
                await asyncio.sleep(max(self.interval, 5.0))
        if self.client is not None:
            await self.client.close()
            self.client = None
        self.set_state("parado")

    async def poll(self):
        ied = self.mgr.ied
        names: list[ObjName] = []
        index: list[tuple] = []
        for p in self.points:
            dom, item = remote_parts(ied, p, p.link or {})
            for role, rel in _leaf_names(p):
                names.append(ObjName("dom", dom, f"{item}${rel}"))
                index.append((p, role))
        vals: list = []
        for i in range(0, len(names), 50):
            vals += await self.client.read(names[i:i + 50])
        got: dict = {}
        for (p, role), raw in zip(index, vals):
            got.setdefault(p.id, {"p": p})[role] = raw
        for pid, g in got.items():
            p = g["p"]
            v = g.get("v")
            if v is None or v[0] == "error":
                continue
            value = v[1]
            if isinstance(value, Bits):
                value = value.val
            if p.cdc == "CMV":
                a = g.get("a")
                value = {"mag": value, "ang": a[1] if a and a[0] != "error" else 0.0}
            q = g.get("q")
            q = q[1] if q and q[0] == "bits" else None
            t = g.get("t")
            t = t[1] if t and t[0] == "utc" else None
            self.last[pid] = (value, q, t)
            ied.apply_mirror(p, value, q, t)
        self.last_update = time.time()
        ied.emit("link.state", self.json())


class MirrorManager:
    def __init__(self, ied):
        self.ied = ied
        self.groups: dict[tuple, MirrorGroup] = {}
        self.running = False

    def refresh(self):
        want: dict[tuple, list] = {}
        intervals: dict[tuple, float] = {}
        for p in self.ied.points:
            if p.link and p.link.get("ip") and p.mode in ("espelhado", "invertido", "forcado"):
                key = (p.link["ip"], int(p.link.get("porta") or 102))
                want.setdefault(key, []).append(p)
                iv = float(p.link.get("intervalo_ms") or 1000) / 1000.0
                intervals[key] = min(intervals.get(key, iv), iv)
        for key in list(self.groups):
            if key not in want:
                g = self.groups.pop(key)
                if g.task:
                    g.task.cancel()
        for key, pts in want.items():
            g = self.groups.get(key)
            if g is None:
                g = self.groups[key] = MirrorGroup(self, key[0], key[1])
            g.points = pts
            g.interval = max(intervals[key], 0.1)
            if self.running and g.task is None:
                g.task = asyncio.get_running_loop().create_task(g.run())

    def resync_point(self, p):
        for g in self.groups.values():
            if p.id in g.last and p in g.points:
                v, q, t = g.last[p.id]
                self.ied.apply_mirror(p, v, q, t)

    def start(self):
        self.running = True
        for g in self.groups.values():
            if g.task is None:
                g.task = asyncio.get_running_loop().create_task(g.run())

    def stop(self):
        self.running = False
        for g in self.groups.values():
            if g.task:
                g.task.cancel()
                g.task = None
            g.state = "parado"

    def state(self) -> list[dict]:
        return [g.json() for g in self.groups.values()]
