"""Parâmetros do launcher — os números da especificação reunidos em um só lugar."""

APP_NAME = "Discord Proxy Launcher"
APP_VERSION = "1.0.0"

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
# As listas têm milhares de entradas; testar tudo a 10 por vez levaria vários
# minutos. O teste para assim que junta esta quantidade de aprovados e o resto
# da lista fica guardado para quando a fila de reserva esvaziar.
# Use None para testar a lista inteira antes de abrir o Discord.
TEST_ENOUGH_APPROVED = 6
RETEST_AFTER = 600.0                # só retesta um proxy reprovado após 10 min
STANDBY_FRESH_FOR = 30.0            # reserva testada há menos que isso não é revalidada
DISCORD_RESTART_PAUSE = 1.5         # pausa entre fechar e reabrir o Discord
