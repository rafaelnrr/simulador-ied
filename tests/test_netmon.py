"""Monitor de conexões: classificação dos pacotes capturados e eventos da aplicação (TCP sem associação, LD de outro IED)."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import struct
import time
from types import SimpleNamespace

import pytest
from conftest import connect, run

from simied.netmon import NetMonitor


def _ied(iid, nome, ip, porta, estado="rodando", ip_cid="", doms=()):
    return SimpleNamespace(id=iid, name=nome, cfg={"ip": ip, "porta": porta}, state=estado, server=None,
                           scl_ied=SimpleNamespace(address={"IP": ip_cid}), domains={d: None for d in doms})


def _mon(*ieds):
    mgr = SimpleNamespace(ieds={i.id: i for i in ieds}, admin_host="192.168.75.102", broadcast=lambda m: None)
    m = NetMonitor(mgr)
    m._locais = (1e18, {"192.168.56.4": "enp0s8", "192.168.75.102": "enp0s3"})
    return m


def _syn(src, sport, dst, dport):
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 40, 0, 0, 64, 6, 0, ipaddress.IPv4Address(src).packed,
                     ipaddress.IPv4Address(dst).packed)
    tcp = struct.pack("!HHIIBBHHH", sport, dport, 1, 0, 0x50, 0x02, 1024, 0, 0)
    return b"\xff" * 6 + b"\x08\x00\x27\x75\xe2\x58" + b"\x08\x00" + ip + tcp


def _arp(quem, alvo):
    return (b"\xff" * 6 + b"\x08\x00\x27\x75\xe2\x58" + b"\x08\x06" + struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1) +
            b"\x08\x00\x27\x75\xe2\x58" + ipaddress.IPv4Address(quem).packed + b"\x00" * 6 +
            ipaddress.IPv4Address(alvo).packed)


def test_syn_para_porta_sem_ied():
    async def go():
        m = _mon(_ied(1, "MD2", "192.168.56.4", 102), _ied(2, "411L", "192.168.56.4", 103, "parado"))
        m._pacote("enp0s8", _syn("192.168.56.102", 40000, "192.168.56.4", 104))
        assert not m.tentativas  # 104 não é porta de nenhum IED
        m._pacote("enp0s8", _syn("192.168.56.102", 40001, "192.168.56.4", 103))
        t = m.tentativas[("syn", "192.168.56.102", "192.168.56.4", 103)]
        assert t["nivel"] == "bad" and "411L" in t["situacao"] and "parado" in t["situacao"]
        m._pacote("enp0s8", _syn("192.168.56.102", 40001, "192.168.56.4", 103))
        assert t["vezes"] == 2 and t["repeticoes"] == 1
        m._pacote("enp0s8", _syn("192.168.56.102", 40002, "192.168.56.9", 102))
        assert "não existe nesta máquina" in m.tentativas[("syn", "192.168.56.102", "192.168.56.9", 102)]["situacao"]
        m._pacote("enp0s3", _syn("192.168.56.102", 40003, "192.168.75.102", 102))
        assert "interface web" in m.tentativas[("syn", "192.168.56.102", "192.168.75.102", 102)]["situacao"]
        est = m.estado()
        assert [e["porta"] for e in est["escutas"]] == [102, 103]
        assert est["escutas"][1]["syns"] == 2 and est["escutas"][1]["origens"] == ["192.168.56.102"]
    run(go())


def test_syn_sem_accept_aponta_firewall(monkeypatch):
    import simied.netmon as nm
    monkeypatch.setattr(nm, "ESPERA_ACCEPT", 0.05)

    async def go():
        m = _mon(_ied(1, "MD2", "192.168.56.4", 102))
        m._pacote("enp0s8", _syn("192.168.56.102", 40000, "192.168.56.4", 102))
        m._pacote("enp0s8", _syn("192.168.56.103", 40000, "192.168.56.4", 102))
        m.tcp_aceita(m.mgr.ieds[1], SimpleNamespace(peer=("192.168.56.103", 40000), peer_str="192.168.56.103:40000",
                                                     local=("192.168.56.4", 102)))
        await asyncio.sleep(0.15)
        assert "firewall" in m.tentativas[("syn", "192.168.56.102", "192.168.56.4", 102)]["situacao"]
        assert m.tentativas[("syn", "192.168.56.103", "192.168.56.4", 102)]["nivel"] == "ok"
    run(go())


def test_arp():
    m = _mon(_ied(1, "MD2", "192.168.56.4", 102, ip_cid="10.237.68.21"), _ied(2, "TD0", "192.168.56.8", 102))
    m._pacote("enp0s3", _arp("192.168.74.50", "192.168.74.60"))
    assert not m.tentativas  # ARP alheio na rede do DHCP
    m._pacote("enp0s8", _arp("192.168.56.102", "192.168.56.8"))
    assert "crie o IP" in m.tentativas[("arp", "192.168.56.102", "192.168.56.8", None)]["situacao"]
    m._pacote("enp0s3", _arp("192.168.74.50", "10.237.68.21"))
    assert "relé real" in m.tentativas[("arp", "192.168.74.50", "10.237.68.21", None)]["situacao"]
    m._pacote("enp0s8", _arp("192.168.56.102", "192.168.56.4"))
    assert m.tentativas[("arp", "192.168.56.102", "192.168.56.4", None)]["nivel"] == "ok"


def test_dica_ld_de_outro_ied():
    a = _ied(1, "TD0", "192.168.56.4", 102, doms=["SE_TD0CTRL"])
    b = _ied(2, "MD2", "192.168.56.4", 103, "parado", doms=["SE_MD2CTRL"])
    m = _mon(a, b)
    assert "MD2 (192.168.56.4:103, parado)" in m.dono_dominio(a, "SE_MD2CTRL")
    assert "nenhum IED" in m.dono_dominio(a, "OUTRO")


def test_conexao_sem_associacao_e_ld_errado(sim):
    ied = sim.create()
    outro = sim.create(start=False)
    port = ied["porta"]

    async def go():
        s = socket.create_connection(("127.0.0.1", port))
        s.close()
        await asyncio.sleep(0.3)
        c = await connect(port)
        with pytest.raises(Exception):  # noqa: B017
            await c.get_name_list(0, "SEM_ESSE_LD")
        await c.close()
    run(go())
    time.sleep(0.8)
    ev = sim.h.get("/net/monitor").json()["eventos"]
    tipos = {e["tipo"] for e in ev if e["ied_id"] == ied["id"]}
    assert {"tcp", "fim", "assoc", "dominio"} <= tipos
    assert any("antes da associação" in e["texto"] for e in ev if e["ied_id"] == ied["id"])
    log = sim.h.get(f"/ieds/{ied['id']}/mms-log").json()["eventos"]
    assert any("antes da associação" in e["detalhe"] for e in log)
    assert any("nenhum IED carregado tem esse LD" in e["detalhe"] for e in log)
    esc = [e for e in sim.h.get("/net/monitor").json()["escutas"] if e["ied_id"] in (ied["id"], outro["id"])]
    assert len(esc) == 2
