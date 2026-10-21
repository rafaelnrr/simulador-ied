#!/usr/bin/env bash
# Instala o simulador nesta máquina (Ubuntu). Uso: ./instalar.sh [--servico]
#   --servico  também instala e liga o serviço systemd "simied" (sobe no boot).
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "precisa de Python 3.10 ou mais novo"; exit 1
fi
if ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c 'import ensurepip' 2>/dev/null; then
  echo "instalando python3-venv (pede sudo)"; sudo apt-get update && sudo apt-get install -y python3-venv
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
[ -f simied.conf ] || cp simied.conf.exemplo simied.conf
chmod +x iniciar.sh
echo "dependências instaladas em $DIR/.venv"
echo "ajuste SIMIED_IFACE ou SIMIED_HOST em $DIR/simied.conf (interfaces: $(ip -br -4 addr | awk '$1 != "lo" {printf "%s %s  ", $1, $3}'))"

if [ "${1:-}" = "--servico" ]; then
  sudo tee /etc/systemd/system/simied.service >/dev/null <<UNIT
[Unit]
Description=Simulador de IEDs IEC 61850
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$DIR
ExecStart=$DIR/iniciar.sh
# always: o iniciar.sh encerra o simulador quando o DHCP troca o IP de SIMIED_IFACE
Restart=always
RestartSec=5
# porta 102 dos IEDs (RN-NET-05), criação de IPs secundários pela tela Rede (RN-NET-02) e monitor de
# conexões da tela Rede (vê ARP e SYN para IP/porta sem IED), só para este processo
AmbientCapabilities=CAP_NET_BIND_SERVICE CAP_NET_ADMIN CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_NET_ADMIN CAP_NET_RAW
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable --now simied
  echo "serviço simied ligado: systemctl status simied / journalctl -u simied -f"
fi
