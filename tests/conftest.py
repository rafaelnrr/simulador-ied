"""Infraestrutura dos testes: simulador real em subprocesso (loopback, --teste-local) e cliente MMS."""
from __future__ import annotations

import asyncio
import itertools
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from simied.mms.client import MmsClient  # noqa: E402
from simied.mms.pdu import ObjName  # noqa: E402
from simied.mms.types import MmsType, enc_data  # noqa: E402

CID_I15 = ROOT / "exemplos" / "SE_BAB_H06_BCM1_i15_exemplo_ed1.cid"
CID_REF = ROOT / "exemplos" / "SE_BAB_UCDQSA1_referencia_exemplo_ed2.cid"
I15 = "SE_BAB__H06_BCM1_MD2"
CTRL = I15 + "CTRL"
MED = I15 + "MED"
IND3 = "sAutGGIO2$ST$Ind3"

VIS = MmsType("vis", -65)
BOOL = MmsType("bool")
UINT = MmsType("uint", 32)

_names = itertools.count(1)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sim:
    """Acesso à API do simulador em execução."""

    def __init__(self, base: str):
        self.base = base
        self.h = httpx.Client(base_url=base + "/api/v1", timeout=15)

    def upload(self, cid: Path) -> dict:
        r = self.h.post("/cid", files={"arquivo": (cid.name, cid.read_bytes())})
        r.raise_for_status()
        return r.json()

    def create(self, cid: Path = CID_I15, perfil: str = "ingeteam-ef-i15", port: int | None = None,
               start: bool = True, **extra) -> dict:
        up = self.upload(cid)
        body = {"token": up["token"], "perfil": perfil, "ip": "127.0.0.1", "porta": port or free_port(),
                "nome": f"teste{next(_names)}_{os.getpid()}", "aceitar_erros": True, **extra}
        r = self.h.post("/ieds", json=body)
        assert r.status_code == 200, r.text
        ied = r.json()
        if start:
            r = self.h.post(f"/ieds/{ied['id']}/start")
            assert r.status_code == 200, r.text
            assert r.json()["estado"] == "rodando", r.text
        return ied

    def point(self, iid: int, q: str) -> dict:
        pts = self.h.get(f"/ieds/{iid}/points", params={"q": q}).json()["pontos"]
        exact = [p for p in pts if p["path"].endswith(q)]
        assert exact, f"ponto {q} não encontrado"
        return exact[0]

    def set(self, iid: int, q: str, **body) -> httpx.Response:
        return self.h.patch(f"/ieds/{iid}/points/{self.point(iid, q)['id']}", json=body)

    def faults(self, iid: int, **cfg) -> dict:
        r = self.h.put(f"/ieds/{iid}/faults", json=cfg)
        assert r.status_code == 200, r.text
        return r.json()

    def reports(self, iid: int, wait_for: int = 0, timeout: float = 3.0) -> list:
        end = time.time() + timeout
        while True:
            rows = self.h.get(f"/ieds/{iid}/reports").json()["reports"]
            if len(rows) >= wait_for or time.time() > end:
                return rows
            time.sleep(0.2)


def start_server(data: Path) -> tuple[subprocess.Popen, Sim]:
    """Sobe o simulador real (loopback, --teste-local) com a pasta de dados indicada."""
    port = free_port()
    log = open(data / "server.log", "ab")
    proc = subprocess.Popen([sys.executable, "-u", "-m", "simied", "--dados", str(data), "serve", "--teste-local",
                             "--admin-port", str(port), "--log", "warning"], cwd=ROOT, stdout=log, stderr=log)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/api/v1/info", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            raise RuntimeError((data / "server.log").read_text())
        time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("simulador não respondeu")
    s = Sim(base)
    s.data_dir = data
    return proc, s


def stop_server(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def sim(tmp_path_factory):
    proc, s = start_server(tmp_path_factory.mktemp("dados"))
    yield s
    stop_server(proc)


# ---------------------------------------------------------------------------
# helpers MMS (async)

def on(dom: str, item: str) -> ObjName:
    return ObjName("dom", dom, item)


async def connect(port: int, timeout: float = 5.0) -> MmsClient:
    c = MmsClient("127.0.0.1", port, timeout=timeout)
    await c.connect()
    return c


async def write1(c: MmsClient, dom: str, item: str, mtype: MmsType, value):
    return (await c.write([on(dom, item)], [enc_data(mtype, value)]))[0]


async def enable(c: MmsClient, rcb: str, rptid: str | None = None, dom: str = CTRL, gi: bool = False):
    """Sequência do SAGE (RN-SAGE-01): escreve RptID e habilita o RCB."""
    if rptid is not None:
        assert await write1(c, dom, f"{rcb}$RptID", VIS, rptid) is None
    assert await write1(c, dom, f"{rcb}$RptEna", BOOL, True) is None
    if gi:
        assert await write1(c, dom, f"{rcb}$GI", BOOL, True) is None


async def next_report(c: MmsClient, timeout: float = 1.0) -> dict | None:
    try:
        return await asyncio.wait_for(c.reports.get(), timeout)
    except asyncio.TimeoutError:
        return None


async def drain(c: MmsClient, quiet: float = 0.3) -> list:
    out = []
    while (r := await next_report(c, quiet)) is not None:
        out.append(r)
    return out


def run(coro):
    return asyncio.run(coro)


async def ahttp(fn, *a, **kw):
    """Chama a API (síncrona) sem travar o loop do cliente MMS."""
    return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*a, **kw))
