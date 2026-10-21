#!/usr/bin/env bash
# Sobe o simulador. Login por usuários locais (python -m simied usuario <nome>) e, se LDAP_URL
# estiver definido, também pelo LDAP da rede TOCOT.
# Ajuste em simied.conf (ao lado deste script); variáveis de ambiente têm prioridade.
cd "$(dirname "$0")"
[ -f simied.conf ] && . ./simied.conf

# IP recebido por DHCP na interface (ignora os IPs fixos que os IEDs põem na mesma placa)
ip_dhcp() { ip -4 -o addr show dev "$1" dynamic 2>/dev/null | awk '{split($4, a, "/"); print a[1]; exit}'; }

if [ -z "${SIMIED_HOST:-}" ] && [ -n "${SIMIED_IFACE:-}" ]; then
  for _ in $(seq 60); do
    SIMIED_HOST="$(ip_dhcp "$SIMIED_IFACE")"
    [ -n "$SIMIED_HOST" ] && break
    sleep 1
  done
  [ -n "$SIMIED_HOST" ] || { echo "interface $SIMIED_IFACE sem IP do DHCP" >&2; exit 1; }
  echo "interface web em http://$SIMIED_HOST:${SIMIED_PORTA:-8080}/ (IP do DHCP em $SIMIED_IFACE)"
  # Sob o systemd: se o DHCP trocar o IP, encerra para o serviço subir de novo no IP novo.
  # Após o exec abaixo, $$ é o próprio simulador.
  if [ -n "${INVOCATION_ID:-}" ]; then
    (
      while sleep 10 && kill -0 $$ 2>/dev/null; do
        atual="$(ip_dhcp "$SIMIED_IFACE")"
        if [ -n "$atual" ] && [ "$atual" != "$SIMIED_HOST" ]; then
          echo "IP de $SIMIED_IFACE mudou ($SIMIED_HOST -> $atual), reiniciando" >&2
          kill $$; exit
        fi
      done
    ) &
  fi
fi
: "${SIMIED_HOST:?defina SIMIED_HOST ou SIMIED_IFACE em simied.conf}"
LDAP_ARGS=()
if [ -n "${LDAP_URL:-}" ]; then
  LDAP_ARGS=(--ldap-url "$LDAP_URL" --ldap-starttls --ldap-ca "${LDAP_CA:-data/ldap-ca.pem}"
    --ldap-tls-nome "${LDAP_TLS_NOME:-srv-radius-01}" --ldap-base "${LDAP_BASE:-dc=redetocot,dc=local}"
    --ldap-grupo-admin simied_admin --ldap-grupo-operacao simied_operacao --ldap-grupo-leitura simied_leitura)
fi
exec .venv/bin/python -m simied serve --admin-host "$SIMIED_HOST" --admin-port "${SIMIED_PORTA:-8080}" \
  "${LDAP_ARGS[@]}" "$@"
