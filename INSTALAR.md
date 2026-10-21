# Instalação do SimIED em outra máquina Ubuntu

Requisitos: Ubuntu com Python 3.10 ou mais novo.

```bash
tar xzf simied-*.tar.gz
cd sim_ied
./instalar.sh --servico      # cria .venv, instala dependências e liga o serviço "simied"
```

Antes de ligar (ou depois, com `sudo systemctl restart simied`), ajuste `simied.conf`:

- `SIMIED_IFACE` — interface com DHCP; a interface web escuta no IP que o DHCP der a ela (`ip -br addr`
  lista as interfaces). Se o IP mudar com o serviço rodando, ele reinicia sozinho no IP novo.
- `SIMIED_HOST` — IP fixo para a interface web, se preferir; tem prioridade sobre `SIMIED_IFACE`.
  Não pode ser `0.0.0.0` e **não pode ser usado por IEDs** (RN-NET-08). Para os IEDs, reserve outro IP
  na mesma placa: `sudo ip addr add 192.168.74.X/23 dev <interface>` (para ficar permanente, coloque no netplan).

Na primeira instalação, o `instalar.sh` cria o usuário administrador (único usuário desta versão): usuário
`admin` e uma senha aleatória, mostrada no fim da instalação e gravada em `CREDENCIAIS_ADMIN.txt` (permissão 600,
fora do git). Para ver de novo: `cat CREDENCIAIS_ADMIN.txt`.

Acesse `http://<SIMIED_HOST>:8080/`, entre com essas credenciais e **troque a senha** em
**Configuração e minha conta** (ou com `.venv/bin/python -m simied usuario admin --papel administrador`).
Depois apague o `CREDENCIAIS_ADMIN.txt`.

## Sem o serviço systemd

`./instalar.sh` (sem `--servico`) e depois `./iniciar.sh`. Rodando assim, a porta 102 dos IEDs exige
permissão: o serviço já concede `CAP_NET_BIND_SERVICE` só ao simulador; rodando à mão, use
`sudo setcap cap_net_bind_service,cap_net_admin,cap_net_raw=+ep "$(readlink -f .venv/bin/python)"` (vale para todo
uso desse Python). `cap_net_raw` liga a captura do quadro "Quem está tentando conectar" da tela Rede, que mostra
pedidos ARP e TCP para IP ou porta onde nenhum IED escuta; o serviço criado por `./instalar.sh --servico` já a tem.

## Testes

`.venv/bin/python -m pytest -q`

## Firewall

Se o `ufw` estiver ativo: `sudo ufw allow 8080/tcp` (web) e `sudo ufw allow 102/tcp` (MMS dos IEDs).

## API

Referência em `http://<SIMIED_HOST>:8080/api/docs`; política de acesso por RCB e demais detalhes em `API.md`.
