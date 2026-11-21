"""Parâmetros do launcher — os números da especificação reunidos em um só lugar."""
from __future__ import annotations

import os
import sys

APP_NAME = "Discord Proxy Launcher"
APP_VERSION = "1.1.1"
AUTHOR = "pedrostyxx"
WINDOW_TITLE = f"{APP_NAME} - By: {AUTHOR}"     # só a barra de título; mensagens usam APP_NAME

# --- Modo A: túnel SSH -----------------------------------------------------
LOCAL_SOCKS_HOST = "127.0.0.1"
LOCAL_SOCKS_PORT = 1080
SSH_DEFAULT_PORT = 22
SSH_DEFAULT_USER = "root"
SSH_START_TIMEOUT = 10.0            # segundos para o túnel ficar pronto
SSH_RECONNECT_ATTEMPTS = 3          # tentativas antes do erro permanente
SSH_RECONNECT_DELAYS = (0.0, 5.0, 10.0)  # espera antes de cada tentativa

# --- Monitor de saúde (ambos os modos) --------------------------------------
HEALTH_INTERVAL = 30.0              # segundos entre verificações
HEALTH_TIMEOUT = 5.0
HEALTH_TARGET = ("1.1.1.1", 80)
HEALTH_MAX_FAILURES = 2             # falhas consecutivas que disparam a troca

# --- Modo B: proxies públicos -----------------------------------------------
PROXY_SOURCES = (
    ("ProxyScrape",
     "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies"
     "&protocol=socks5&timeout=5000&country=all&ssl=all&anonymity=all"),
    ("TheSpeedX (GitHub)",
     "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("TheSpeedX (jsDelivr)",
     "https://cdn.jsdelivr.net/gh/TheSpeedX/PROXY-List@master/socks5.txt"),
)
FETCH_TIMEOUT = 10.0
TEST_CONCURRENCY = 10               # testes simultâneos (máximo)
TEST_TIMEOUT = 5.0                  # segundos por proxy
TRACE_TIMEOUT = 4.0                 # depois do teste, para descobrir o país de saída do proxy
# As listas têm milhares de entradas; testar tudo a 10 por vez levaria vários
# minutos. O teste para assim que junta esta quantidade de aprovados e o resto
# da lista fica guardado para quando a fila de reserva esvaziar.
# Use None para testar a lista inteira antes de abrir o Discord.
TEST_ENOUGH_APPROVED = 6
RETEST_AFTER = 600.0                # só retesta um proxy reprovado após 10 min
STANDBY_FRESH_FOR = 30.0            # reserva testada há menos que isso não é revalidada
DISCORD_RESTART_PAUSE = 1.5         # pausa entre fechar e reabrir o Discord

# O Discord fala com um proxy local (relay.py), que repassa cada conexão ao
# proxy público ativo e, se ele falhar, tenta a mesma conexão na reserva.
RELAY_BACKUPS = 3                   # proxies da reserva tentados por conexão, além do ativo
RELAY_STAGGER = 2.5                 # sem resposta em tanto tempo, o próximo proxy corre junto
RELAY_ATTEMPT_TIMEOUT = 10.0        # prazo de cada tentativa (proxy + destino)
RELAY_CONNECT_TIMEOUT = 15.0        # prazo total de uma conexão do Discord
RELAY_MAX_STRIKES = 3               # derrotas seguidas para a reserva que tiram um proxy de uso

# O proxy precisa sair fora do Brasil: proxies públicos que saem por aqui são recusados.
BLOCKED_EXIT_COUNTRIES = frozenset({"BR"})

# --- Servidor dedicado (o "dconnect", oferecido por pedrostyxx) ---------------
DEDICATED_OWNER = AUTHOR
INSTAGRAM_URL = "https://www.instagram.com/pedrostyxx/"
RELEASES_URL = "https://github.com/pedrostyxx/discord-proxy-launcher/releases/latest"
DEDICATED_CONFIG_FILE = "dconnect.json"     # fica fora do Git; o build.ps1 embute no .exe
VPS_CHECK_TIMEOUT = 10.0            # verificação completa (servidor + túnel até o Discord)
VPS_RETRY_INITIAL = 120.0           # fora do ar: tenta voltar para ele depois disso...
VPS_RETRY_MAX = 1800.0              # ...dobrando a cada falha, até este teto
VPS_RETURN_CONFIRM = 30.0           # volta só depois de duas verificações com este intervalo
VPS_STABLE_AFTER = 1800.0           # tanto tempo bem no servidor zera o recuo
VPS_RELAPSE_WINDOW = 600.0          # cair de novo antes disso dobra o recuo
VPS_GRACE_DELAYS = (2.0, 4.0, 8.0)  # um reinício rápido do servidor não manda todo mundo para a lista
VPS_SUSPECT_FAILURES = 3            # falhas do servidor nas conexões do Discord...
VPS_SUSPECT_SPAN = 10.0             # ...espalhadas por pelo menos este tempo
VPS_REFUSING_COUNT = 5              # recusas respondidas (limites) sem nenhum sucesso...
VPS_REFUSING_SPAN = 20.0            # ...por pelo menos este tempo: o servidor não está servindo


COUNTRY_NAMES = {
    "US": "EUA", "CA": "Canadá", "MX": "México", "AR": "Argentina", "CL": "Chile", "CO": "Colômbia",
    "PE": "Peru", "UY": "Uruguai", "PY": "Paraguai", "FR": "França", "DE": "Alemanha", "NL": "Holanda",
    "GB": "Reino Unido", "ES": "Espanha", "PT": "Portugal", "IT": "Itália", "PL": "Polônia", "SE": "Suécia",
    "FI": "Finlândia", "CH": "Suíça", "RU": "Rússia", "UA": "Ucrânia", "TR": "Turquia", "IN": "Índia",
    "SG": "Singapura", "JP": "Japão", "KR": "Coreia do Sul", "CN": "China", "HK": "Hong Kong",
    "ID": "Indonésia", "VN": "Vietnã", "TH": "Tailândia", "AU": "Austrália", "ZA": "África do Sul",
    "BR": "Brasil",
}


def country_name(code: str) -> str:
    return COUNTRY_NAMES.get(code.upper(), code.upper()) if code else ""


def bundled_file(name: str) -> str:
    """Um arquivo empacotado junto do app (ou na raiz do projeto, rodando do código)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, name)


def load_dedicated():
    """O servidor dedicado embutido neste build, ou None."""
    from . import dconnect

    return dconnect.load(bundled_file(DEDICATED_CONFIG_FILE))
