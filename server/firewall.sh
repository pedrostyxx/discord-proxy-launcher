#!/bin/sh
# Regras de saída do usuário "dconnect" (só ele; nada mais da máquina é afetado).
#
# O servidor só precisa de: respostas para o cloudflared (conexões já abertas),
# DNS nos resolvedores do sistema e HTTPS (443) para a internet. Fica proibido,
# no próprio kernel, qualquer outro destino: outras portas, a própria máquina
# (bancos, Redis, painéis), redes internas, Docker e VPN. É a segunda barreira,
# caso a validação do servidor falhe um dia.
#
# Uso:
#   firewall.sh            aplica (o dconnect.service roda isto antes de cada início)
#   firewall.sh --ensure   só confere; se alguém apagou as regras (um iptables-restore,
#                          um "flush ruleset"), reaplica. Se não conseguir, para o
#                          dconnect. Roda a cada 5 minutos (dconnect-firewall.timer).
#
# Idempotente: refaz a cadeia DCONNECT_OUT e deixa um único salto para ela, pelo uid
# numérico (apaga saltos antigos, inclusive de um uid anterior do usuário).
set -eu

CHAIN=DCONNECT_OUT
UID_NUM=$(id -u dconnect 2>/dev/null) || { echo "usuário dconnect não existe"; exit 1; }
IPT="iptables -w 10"                   # -w: espera a trava do xtables (Docker mexe junto)
IPT6=""
if command -v ip6tables >/dev/null 2>&1; then IPT6="ip6tables -w 10"; fi

nameservers() {
  # Sem o %zona de IPv6 com escopo (fe80::1%eth0), que o iptables não aceita.
  awk '/^nameserver/ {print $2}' /etc/resolv.conf | sed 's/%.*//'
}

installed() {
  # Pelo -S, não pelo -C: no iptables-nft o "-C DCONNECT_OUT -j REJECT" casa com a
  # regra "-d ::/96 -j REJECT" e diria que a cadeia está completa sem a regra final.
  $1 -S OUTPUT 2>/dev/null | grep -qx -- "-A OUTPUT -m owner --uid-owner $UID_NUM -j $CHAIN" &&
    $1 -S "$CHAIN" 2>/dev/null | tail -n 1 | grep -q -- "^-A $CHAIN -j REJECT"
}

if [ "${1:-}" = "--ensure" ]; then
  ok=1
  installed "$IPT" || ok=0
  if [ -n "$IPT6" ]; then installed "$IPT6" || ok=0; fi
  [ "$ok" = 0 ] || exit 0
  echo "regras de saída do dconnect ausentes; reaplicando"
  if ! "$0"; then
    echo "não foi possível reaplicar as regras de saída; parando o dconnect"
    systemctl stop dconnect.service || true
    exit 1
  fi
  exit 0
fi

prepare() {
  $1 -N "$CHAIN" 2>/dev/null || true
  $1 -F "$CHAIN"
  $1 -S OUTPUT | grep -- "-j $CHAIN\$" | sed 's/^-A //' | while read -r rule; do
    # shellcheck disable=SC2086
    $1 -D $rule
  done
  $1 -I OUTPUT 1 -m owner --uid-owner "$UID_NUM" -j "$CHAIN"
  $1 -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
}

finish() {
  $1 -A "$CHAIN" -o lo -j REJECT
  $1 -A "$CHAIN" -m addrtype --dst-type LOCAL -j REJECT
}

# --- IPv4
prepare "$IPT"
for ns in $(nameservers); do
  case "$ns" in
    *:*) ;;
    *) $IPT -A "$CHAIN" -d "$ns" -p udp --dport 53 -j ACCEPT
       $IPT -A "$CHAIN" -d "$ns" -p tcp --dport 53 -j ACCEPT ;;
  esac
done
finish "$IPT"
for net in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 \
           192.0.0.0/24 192.168.0.0/16 198.18.0.0/15 224.0.0.0/3; do
  $IPT -A "$CHAIN" -d "$net" -j REJECT
done
$IPT -A "$CHAIN" -p tcp --dport 443 -m conntrack --ctstate NEW -j ACCEPT
$IPT -A "$CHAIN" -j REJECT

# --- IPv6
if [ -n "$IPT6" ]; then
  prepare "$IPT6"
  for ns in $(nameservers); do
    case "$ns" in
      *:*) $IPT6 -A "$CHAIN" -d "$ns" -p udp --dport 53 -j ACCEPT
           $IPT6 -A "$CHAIN" -d "$ns" -p tcp --dport 53 -j ACCEPT ;;
    esac
  done
  finish "$IPT6"
  for net in ::/96 ::ffff:0:0/96 64:ff9b::/96 2001::/23 2002::/16 fc00::/7 fe80::/10 ff00::/8; do
    $IPT6 -A "$CHAIN" -d "$net" -j REJECT
  done
  $IPT6 -A "$CHAIN" -p tcp --dport 443 -m conntrack --ctstate NEW -j ACCEPT
  $IPT6 -A "$CHAIN" -j REJECT
fi
echo "regras de saída do dconnect aplicadas (uid $UID_NUM)"
