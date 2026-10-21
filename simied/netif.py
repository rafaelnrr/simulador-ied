"""Interfaces de rede da máquina (RN-NET-02, RN-NET-03, RN-NET-12 a RN-NET-16)."""
from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
from pathlib import Path


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def list_interfaces(include_loopback: bool = False) -> list[dict]:
    try:
        out = subprocess.run(["ip", "-j", "addr", "show"], capture_output=True, text=True, timeout=5, check=True).stdout
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        data = []
    res = []
    for it in data:
        name = it.get("ifname", "")
        loop = "LOOPBACK" in it.get("flags", [])
        if loop and not include_loopback:
            continue
        base = Path("/sys/class/net") / name
        oper = it.get("operstate", "") or _read(base / "operstate").upper()
        up = "UP" in it.get("flags", []) and ("LOWER_UP" in it.get("flags", []) or oper in ("UP", "UNKNOWN"))
        speed = _read(base / "speed")
        ips = [{"ip": a["local"], "prefixo": a.get("prefixlen", 0),
                "mascara": str(ipaddress.IPv4Network(f"0.0.0.0/{a.get('prefixlen', 0)}").netmask),
                "rotulo": a.get("label", name)}
               for a in it.get("addr_info", []) if a.get("family") == "inet"]
        res.append({
            "nome": name,
            "mac": it.get("address", ""),
            "ips": ips,
            "ativa": up,
            "estado": "ativa" if up else "inativa",
            "velocidade_mbps": int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None,
            "loopback": loop,
            "selecionavel": up and bool(ips),
        })
    return res


def local_ips(include_loopback: bool = True) -> dict[str, str]:
    """ip -> interface."""
    out = {}
    for it in list_interfaces(include_loopback):
        for a in it["ips"]:
            out[a["ip"]] = it["nome"]
    return out


def iface_of(ip: str) -> dict | None:
    for it in list_interfaces(True):
        if any(a["ip"] == ip for a in it["ips"]):
            return it
    return None


def ip_in_use(ip: str, iface: str | None = None) -> tuple[bool, str]:
    """RN-NET-03: verifica se o IP já responde na rede antes de criá-lo como alias."""
    if shutil.which("arping") and iface:
        r = subprocess.run(["arping", "-D", "-c", "2", "-w", "2", "-I", iface, ip], capture_output=True, text=True)
        if r.returncode != 0:
            return True, f"arping: {ip} já responde na rede"
        return False, "arping: sem resposta"
    r = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, text=True)
    if r.returncode == 0:
        return True, f"ping: {ip} responde na rede"
    return False, "ping: sem resposta (ICMP pode estar bloqueado; prefira arping)"


def add_alias(iface: str, ip: str, prefix: int) -> tuple[bool, str]:
    """RN-NET-02: cria IP secundário quando há permissão (CAP_NET_ADMIN)."""
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        return False, "IP inválido"
    if ip in local_ips():
        return False, "IP já existe na máquina"
    used, why = ip_in_use(ip, iface)
    if used:
        return False, f"IP recusado (RN-NET-03): {why}"
    r = subprocess.run(["ip", "addr", "add", f"{ip}/{prefix}", "dev", iface, "label", f"{iface}:sim"[:15]],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, ("sem permissão para criar IP (precisa de root ou CAP_NET_ADMIN). "
                       f"Crie manualmente: sudo ip addr add {ip}/{prefix} dev {iface}. Detalhe: {r.stderr.strip()}")
    return True, "IP criado"


def del_alias(iface: str, ip: str, prefix: int) -> tuple[bool, str]:
    r = subprocess.run(["ip", "addr", "del", f"{ip}/{prefix}", "dev", iface], capture_output=True, text=True)
    return r.returncode == 0, r.stderr.strip() or "IP removido"
