#!/usr/bin/env bash
# Instala ou atualiza o dconnect (servidor dedicado do Discord Proxy Launcher).
# Debian/Ubuntu com systemd e Python 3.9+.
#
# Primeira vez:  sudo bash install.sh --public-host dconnect.exemplo.com [--port 47787]
# Atualizar:     sudo bash install.sh
#
# Rodar de novo só atualiza o código, as regras e as unidades. O token e o
# caminho, que vão dentro de cada .exe distribuído, são criados uma única vez:
# trocá-los desligaria o servidor dedicado para todo app já instalado.
# O Cloudflare Tunnel que publica o servidor é configurado à parte (veja server/README.md).
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "rode como root (sudo)"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
PUBLIC_HOST=""
PORT=47787
while [ $# -gt 0 ]; do
  case "$1" in
    --public-host) PUBLIC_HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "opção desconhecida: $1"; exit 1 ;;
  esac
done

python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "precisa de Python 3.9 ou mais novo"; exit 1; }
command -v iptables >/dev/null || { echo "precisa do iptables"; exit 1; }

# 1. Usuário do serviço, sem login e sem pasta pessoal, com um uid fixo e incomum.
#    As regras de saída valem para o uid numérico: o automático (perto de 999) é o
#    mesmo de imagens Docker comuns (postgres, redis, mysql...), e um contêiner desses
#    com network_mode host cairia nas regras do dconnect.
DCONNECT_UID=47787
uid_free() { ! getent passwd "$DCONNECT_UID" >/dev/null && ! getent group "$DCONNECT_UID" >/dev/null; }
if ! id -u dconnect >/dev/null 2>&1; then
  if uid_free; then
    useradd --system --uid "$DCONNECT_UID" --user-group --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin dconnect
  else
    echo "aviso: uid $DCONNECT_UID ocupado; usando um uid automático"
    useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin dconnect
  fi
elif [ "$(id -u dconnect)" != "$DCONNECT_UID" ] && uid_free; then
  echo "movendo o usuário dconnect do uid $(id -u dconnect) para $DCONNECT_UID"
  systemctl stop dconnect.service 2>/dev/null || true
  usermod -u "$DCONNECT_UID" dconnect
  if [ "$(id -gn dconnect)" = dconnect ]; then groupmod -g "$DCONNECT_UID" dconnect; fi
fi

# 2. Código.
install -d -m 0755 /opt/dconnect
install -m 0644 "$HERE/dconnect_server.py" /opt/dconnect/dconnect_server.py
install -m 0755 "$HERE/firewall.sh" /opt/dconnect/firewall.sh

# 3. Configuração: criada uma vez, nunca sobrescrita.
install -d -m 0750 -o root -g dconnect /etc/dconnect
if [ ! -s /etc/dconnect/config.json ]; then
  [ -n "$PUBLIC_HOST" ] || { echo "primeira instalação: informe --public-host"; exit 1; }
  if ss -Hltn "sport = :$PORT" | grep -q .; then
    echo "a porta $PORT já está em uso nesta máquina; escolha outra com --port"; exit 1
  fi
  python3 -I /opt/dconnect/dconnect_server.py --config /etc/dconnect/config.json --init \
    --public-host "$PUBLIC_HOST" --port "$PORT"
fi
chown root:dconnect /etc/dconnect/config.json
chmod 0640 /etc/dconnect/config.json
install -d -m 0700 /var/backups/dconnect
cp -a /etc/dconnect/config.json "/var/backups/dconnect/config-$(date +%Y%m%d-%H%M%S).json"

# 4. Unidades do systemd. As regras de saída são aplicadas pelo próprio
#    dconnect.service antes de cada início e conferidas a cada 5 minutos pelo timer.
install -m 0644 "$HERE/dconnect.service" /etc/systemd/system/dconnect.service
install -m 0644 "$HERE/dconnect-firewall.service" /etc/systemd/system/dconnect-firewall.service
install -m 0644 "$HERE/dconnect-firewall.timer" /etc/systemd/system/dconnect-firewall.timer
rm -f /etc/systemd/system/multi-user.target.wants/dconnect-firewall.service   # da versão anterior
# Resolvedores DNS do sistema liberados no IPAddressDeny da unidade (refeito sempre:
# o resolv.conf pode mudar). IPv6 com escopo perde o %zona.
install -d -m 0755 /etc/systemd/system/dconnect.service.d
{
  echo "# Gerado pelo install.sh do dconnect: libera os resolvedores do /etc/resolv.conf."
  echo "[Service]"
  for ns in $(awk '/^nameserver/ {print $2}' /etc/resolv.conf | sed 's/%.*//'); do
    case "$ns" in
      *:*) echo "IPAddressAllow=$ns/128" ;;
      *) echo "IPAddressAllow=$ns/32" ;;
    esac
  done
} > /etc/systemd/system/dconnect.service.d/resolvers.conf
systemctl daemon-reload
systemctl stop dconnect-firewall.service 2>/dev/null || true   # o oneshot antigo ficava "active (exited)"
systemctl enable dconnect.service dconnect-firewall.timer >/dev/null
systemctl restart dconnect.service
systemctl restart dconnect-firewall.timer
sleep 2
if ! systemctl is-active --quiet dconnect.service; then
  journalctl -u dconnect -n 30 --no-pager
  exit 1
fi

# 5. Teste de ponta a ponta local: health + um túnel até o Discord com TLS de verdade.
python3 -I /opt/dconnect/dconnect_server.py --config /etc/dconnect/config.json --check
echo
echo "dconnect instalado. Para o build do app, gere o dconnect.json com:"
echo "  python3 -I /opt/dconnect/dconnect_server.py --config /etc/dconnect/config.json --client-config"
