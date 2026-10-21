"""Monitor de conexões: mostra quem tenta falar com os IEDs simulados, inclusive o que não chega a um IED.

Duas fontes:
- a própria aplicação (TCP aceito, associação aceita ou recusada, conexão encerrada antes de associar,
  domínio pedido que pertence a outro IED);
- um socket AF_PACKET (precisa de CAP_NET_RAW) que vê pedidos ARP, SYNs TCP para as portas MMS e pings,
  mesmo para IP ou porta onde nenhum IED escuta. É aí que aparece "o SAGE tenta 10.0.0.5:102, que não existe".
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import struct
import time
from collections import deque

from . import netif

log = logging.getLogger(__name__)

ETH_P_ALL = 0x0003
SO_ATTACH_FILTER = 26
PACKET_OUTGOING = 4
# tcpdump -dd -y EN10MB 'arp or (tcp[tcpflags] & (tcp-syn|tcp-ack) == tcp-syn) or icmp[icmptype] == icmp-echo'
BPF = [
    (0x28, 0, 0, 0x0000000c), (0x15, 15, 0, 0x00000806), (0x15, 0, 15, 0x00000800), (0x30, 0, 0, 0x00000017),
    (0x15, 0, 6, 0x00000006), (0x28, 0, 0, 0x00000014), (0x45, 11, 0, 0x00001fff), (0xb1, 0, 0, 0x0000000e),
    (0x50, 0, 0, 0x0000001b), (0x54, 0, 0, 0x00000012), (0x15, 6, 7, 0x00000002), (0x15, 0, 6, 0x00000001),
    (0x28, 0, 0, 0x00000014), (0x45, 4, 0, 0x00001fff), (0xb1, 0, 0, 0x0000000e), (0x50, 0, 0, 0x0000000e),
    (0x15, 0, 1, 0x00000008), (0x06, 0, 0, 0x00040000), (0x06, 0, 0, 0x00000000),
]
ESPERA_ACCEPT = 3.0  # s entre o SYN e a conferência de que algum IED aceitou a conexão


def _ip(b: bytes) -> str:
    return str(ipaddress.IPv4Address(b))


class NetMonitor:
    def __init__(self, mgr):
        self.mgr = mgr
        self.sock: socket.socket | None = None
        self.erro = ""
        self.desde = time.time()
        self.eventos: deque = deque(maxlen=400)
        self.tentativas: dict[tuple, dict] = {}
        self.aceitas: dict[tuple, float] = {}  # (ip, porta) do cliente -> quando o IED aceitou
        self.clientes: set[str] = set()
        self._locais: tuple[float, dict] = (0.0, {})
        self._env: deque = deque(maxlen=50)

    # ------------------------------------------------------------------ captura
    def start(self):
        try:
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        except PermissionError:
            self.erro = "sem permissão para capturar pacotes (precisa de CAP_NET_RAW)"
            return
        except (OSError, AttributeError) as e:
            self.erro = f"captura indisponível: {e}"
            return
        try:
            import ctypes
            prog = b"".join(struct.pack("HBBI", *ins) for ins in BPF)
            buf = ctypes.create_string_buffer(prog)
            s.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, struct.pack("HL", len(BPF), ctypes.addressof(buf)))
        except OSError as e:
            log.warning("filtro BPF recusado (%s); filtrando só em Python", e)
        s.setblocking(False)
        self.sock = s
        asyncio.get_running_loop().add_reader(s.fileno(), self._ler)
        log.info("monitor de conexões ativo")

    def stop(self):
        if self.sock is not None:
            try:
                asyncio.get_running_loop().remove_reader(self.sock.fileno())
            except (RuntimeError, ValueError):
                pass
            self.sock.close()
            self.sock = None

    def como_ativar(self) -> str:
        if not self.erro.startswith("sem permissão"):
            return ""
        if os.environ.get("INVOCATION_ID"):
            return "Rode de novo ./instalar.sh --servico (o serviço passa a ter CAP_NET_RAW) e reinicie o simied."
        return ("Dê a permissão ao Python do simulador: sudo setcap "
                "cap_net_raw,cap_net_bind_service,cap_net_admin=+ep $(readlink -f .venv/bin/python)")

    def _ler(self):
        for _ in range(500):
            try:
                data, addr = self.sock.recvfrom(256)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                log.warning("monitor de conexões: %s", e)
                return
            if addr[2] == PACKET_OUTGOING:
                continue
            try:
                self._pacote(addr[0], data)
            except Exception:  # noqa: BLE001
                log.exception("monitor de conexões: pacote inválido em %s", addr[0])

    def _pacote(self, iface: str, b: bytes):
        if len(b) < 34:
            return
        et = int.from_bytes(b[12:14], "big")
        if et == 0x0806 and len(b) >= 42 and int.from_bytes(b[20:22], "big") == 1:
            spa, tpa = _ip(b[28:32]), _ip(b[38:42])
            if spa != tpa and spa != "0.0.0.0":
                self._arp(iface, spa, tpa)
        elif et == 0x0800:
            ihl = (b[14] & 0x0F) * 4
            proto, src, dst = b[23], _ip(b[26:30]), _ip(b[30:34])
            o = 14 + ihl
            if proto == 6 and len(b) >= o + 14:
                flags = b[o + 13]
                if flags & 0x02 and not flags & 0x10:
                    self._syn(iface, src, int.from_bytes(b[o:o + 2], "big"), dst, int.from_bytes(b[o + 2:o + 4], "big"))
            elif proto == 1 and len(b) > o and b[o] == 8:
                self._ping(iface, src, dst)

    # ------------------------------------------------------------------ contexto
    def locais(self) -> dict:
        """ip -> interface, com cache de 5 s (list_interfaces chama o comando ip)."""
        ts, d = self._locais
        if time.time() - ts > 5:
            d = netif.local_ips()
            self._locais = (time.time(), d)
        return d

    def _ieds_ip(self, ip: str) -> list:
        return [i for i in self.mgr.ieds.values() if i.cfg.get("ip") == ip]

    def _ied_cid(self, ip: str):
        return next((i for i in self.mgr.ieds.values() if i.scl_ied.address.get("IP") == ip), None)

    def _portas(self) -> set[int]:
        return {102} | {int(i.cfg.get("porta") or 102) for i in self.mgr.ieds.values()}

    def _ifaces_ied(self) -> set[str]:
        loc = self.locais()
        return {loc[i.cfg["ip"]] for i in self.mgr.ieds.values() if i.cfg.get("ip") in loc}

    def _nesse_ip(self, ip: str) -> str:
        ieds = self._ieds_ip(ip)
        if not ieds:
            return "nenhum IED configurado neste IP"
        return "IEDs neste IP: " + ", ".join(f"porta {int(i.cfg.get('porta') or 102)} → {i.name} ({i.state})"
                                              for i in sorted(ieds, key=lambda i: int(i.cfg.get("porta") or 102)))

    # ------------------------------------------------------------------ registro
    def _evento(self, tipo: str, nivel: str, texto: str, origem="", destino="", iface="", ied=None):
        e = {"ts": time.time(), "tipo": tipo, "nivel": nivel, "texto": texto, "origem": origem, "destino": destino,
             "interface": iface, "ied": ied.name if ied else "", "ied_id": ied.id if ied else None}
        self.eventos.append(e)
        agora = time.time()
        while self._env and agora - self._env[0] > 1:
            self._env.popleft()
        if len(self._env) < 30:  # rajada de ARP não inunda as telas abertas
            self._env.append(agora)
            self.mgr.broadcast({"evento": "rede.monitor", "ied": None, "dados": e})
        return e

    def _tentativa(self, tipo, origem, destino, porta, iface, nivel, situacao, ied=None, sport=None) -> tuple[dict, bool]:
        k = (tipo, origem, destino, porta)
        t = self.tentativas.get(k)
        novo = t is None
        if novo:
            t = self.tentativas[k] = {"tipo": tipo, "origem": origem, "destino": destino, "porta": porta,
                                      "interface": iface, "primeira": time.time(), "vezes": 0, "repeticoes": 0}
        elif sport is not None and sport == t.get("sport"):
            t["repeticoes"] += 1  # retransmissão do mesmo SYN: ninguém respondeu
        t.update(ultima=time.time(), nivel=nivel, situacao=situacao, ied=ied.name if ied else "",
                 ied_id=ied.id if ied else None, interface=iface)
        t["vezes"] += 1
        if sport is not None:
            t["sport"] = sport
        if len(self.tentativas) > 2000:
            for kk, _ in sorted(self.tentativas.items(), key=lambda x: x[1]["ultima"])[:500]:
                del self.tentativas[kk]
        return t, novo

    def _syn(self, iface, src, sport, dst, dport):
        if dport not in self._portas():
            return
        self.clientes.add(src)
        ep = f"{dst}:{dport}"
        loc = self.locais()
        rod = next((i for i in self._ieds_ip(dst) if i.state == "rodando" and int(i.cfg.get("porta") or 102) == dport), None)
        cfg = next((i for i in self._ieds_ip(dst) if int(i.cfg.get("porta") or 102) == dport), None)
        if dst not in loc:
            real = self._ied_cid(dst)
            nivel, sit = "bad", (f"{dst} é o IP do relé real no CID de {real.name}; o simulador não usa esse IP. "
                                 f"Aponte o host_mms do SAGE para o IP do IED simulado." if real else
                                 f"o IP {dst} não existe nesta máquina. Crie o IP em Rede ou corrija o host_mms no SAGE.")
        elif rod is not None:
            nivel, sit = "info", f"chegou ao IED {rod.name}; conferindo se a conexão completou…"
            asyncio.get_running_loop().call_later(ESPERA_ACCEPT, self._conferir, iface, src, sport, dst, dport, rod)
        elif dst == self.mgr.admin_host:
            nivel, sit = "bad", (f"{dst} é o IP da interface web (DHCP), sem IED. {self._nesse_ip(dst)}. "
                                 "Use no host_mms do SAGE o IP escolhido no IED.")
        elif cfg is not None:
            nivel, sit = "bad", f"o IED {cfg.name} usa {ep} mas está {cfg.state}: a máquina recusou (RST). Inicie o IED."
        else:
            nivel, sit = "bad", (f"nenhum IED escuta em {ep}: a máquina recusou (RST). {self._nesse_ip(dst)}. "
                                 "Confira PORTA= no CNF.CONFIG do SAGE ou a porta do IED.")
        t, novo = self._tentativa("syn", src, dst, dport, iface, nivel, sit, rod or cfg, sport)
        if t["repeticoes"] == 0 or t["vezes"] == 1:
            self._evento("syn", nivel, f"TCP SYN {src}:{sport} → {ep}: {sit}", f"{src}:{sport}", ep, iface, rod or cfg)

    def _conferir(self, iface, src, sport, dst, dport, ied):
        t = self.tentativas.get(("syn", src, dst, dport))
        if t is None:
            return
        if (src, sport) in self.aceitas:
            t.update(nivel="ok", situacao=f"conexão aceita pelo IED {ied.name}")
            return
        sit = (f"o SYN chegou a {dst}:{dport} ({ied.name}), mas a conexão não completou em {ESPERA_ACCEPT:.0f} s: "
               f"firewall desta máquina (nftables/ufw) ou o SYN-ACK não volta ao cliente (rota de {src} por outra "
               f"interface?)")
        t.update(nivel="bad", situacao=sit)
        self._evento("syn", "bad", sit, f"{src}:{sport}", f"{dst}:{dport}", iface, ied)

    def _arp(self, iface, quem, alvo):
        loc = self.locais()
        ieds = self._ieds_ip(alvo)
        real = self._ied_cid(alvo)
        if not (ieds or real or iface in self._ifaces_ied() or quem in self.clientes):
            return  # ARP de outras máquinas da rede do DHCP: não interessa
        if alvo in loc:
            nivel = "ok" if ieds else "info"
            sit = f"{alvo} é desta máquina ({loc[alvo]}); {self._nesse_ip(alvo)}"
            ied = ieds[0] if ieds else None
        elif ieds:
            nivel, ied = "bad", ieds[0]
            sit = f"{alvo} é o IP do IED {ied.name}, mas não existe na máquina: crie o IP em Rede"
        elif real:
            nivel, ied = "bad", real
            sit = f"{alvo} é o IP do relé real no CID de {real.name}; o simulador não usa esse IP"
        else:
            nivel, ied = "warn", None
            sit = f"ninguém nesta máquina tem o IP {alvo}"
        t, novo = self._tentativa("arp", quem, alvo, None, iface, nivel, sit, ied)
        if novo or (nivel != "ok" and t["vezes"] % 20 == 1):
            self._evento("arp", nivel, f"ARP: {quem} procura {alvo}: {sit}", quem, alvo, iface, ied)

    def _ping(self, iface, src, dst):
        ieds = self._ieds_ip(dst)
        if not ieds and src not in self.clientes:
            return
        loc = self.locais()
        nivel, sit = ("ok", f"respondido ({self._nesse_ip(dst)})") if dst in loc else \
            ("bad", f"o IP {dst} não existe nesta máquina")
        t, novo = self._tentativa("ping", src, dst, None, iface, nivel, sit, ieds[0] if ieds else None)
        if novo:
            self._evento("ping", nivel, f"ping {src} → {dst}: {sit}", src, dst, iface, ieds[0] if ieds else None)

    # ------------------------------------------------------------------ eventos da aplicação
    def tcp_aceita(self, ied, a):
        self.clientes.add(a.peer[0])
        agora = time.time()
        self.aceitas[a.peer] = agora
        for k in [k for k, v in self.aceitas.items() if agora - v > 120]:
            del self.aceitas[k]
        t = self.tentativas.get(("syn", a.peer[0], a.local[0], a.local[1]))
        if t is not None:
            t.update(nivel="ok", situacao=f"conexão aceita pelo IED {ied.name}")
        self._evento("tcp", "info", f"TCP aceito em {a.local[0]}:{a.local[1]} ({ied.name}); aguardando a associação",
                     a.peer_str, f"{a.local[0]}:{a.local[1]}", "", ied)

    def app(self, ied, a, tipo: str, nivel: str, texto: str):
        self._evento(tipo, nivel, texto, a.peer_str if a else "", f"{a.local[0]}:{a.local[1]}" if a else "", "", ied)

    def dono_dominio(self, ied, dominio: str) -> str:
        """Dica quando o cliente pede um LD que não existe neste IED (SAGE conectado no IED errado)."""
        donos = [i for i in self.mgr.ieds.values() if i is not ied and dominio in i.domains]
        if donos:
            return " — esse LD é do IED " + ", ".join(
                f"{i.name} ({i.cfg.get('ip') or 'sem IP'}:{int(i.cfg.get('porta') or 102)}, {i.state})" for i in donos) + \
                ": o cliente conectou no IED errado. Confira host_mms e PORTA= no CNF do SAGE ou a porta dos IEDs"
        return (f" — nenhum IED carregado tem esse LD (este IED tem {', '.join(sorted(ied.domains)) or 'nenhum'}). "
                "Confira o NV1.CONFIG no SAGE e o CID")

    # ------------------------------------------------------------------ consulta
    def estado(self) -> dict:
        escutas = []
        for i in sorted(self.mgr.ieds.values(), key=lambda i: (i.cfg.get("ip") or "", int(i.cfg.get("porta") or 102))):
            ip, porta = i.cfg.get("ip") or "", int(i.cfg.get("porta") or 102)
            syns = [t for t in self.tentativas.values() if t["tipo"] == "syn" and t["destino"] == ip and t["porta"] == porta]
            assocs = i.server.associations() if i.server else []
            escutas.append({
                "ied_id": i.id, "ied": i.name, "ip": ip, "porta": porta, "estado": i.state,
                "ip_existe": ip in self.locais(), "ip_cid": i.scl_ied.address.get("IP", ""),
                "clientes": [{"cliente": a.peer_str, "inicio": a.started, "requisicoes": a.requests} for a in assocs],
                "syns": sum(t["vezes"] for t in syns), "ultimo_syn": max((t["ultima"] for t in syns), default=None),
                "origens": sorted({t["origem"] for t in syns}),
            })
        return {
            "ativo": self.sock is not None, "erro": self.erro, "como_ativar": self.como_ativar(),
            "desde": self.desde, "portas": sorted(self._portas()), "interfaces_ieds": sorted(self._ifaces_ied()),
            "escutas": escutas,
            "tentativas": sorted(self.tentativas.values(), key=lambda t: -t["ultima"])[:300],
            "eventos": list(self.eventos)[::-1],
        }

    def limpar(self):
        self.eventos.clear()
        self.tentativas.clear()
        self.desde = time.time()
