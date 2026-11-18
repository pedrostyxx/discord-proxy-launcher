"""Modo B: baixa as listas públicas de SOCKS5, testa e mantém a fila de reserva."""
from __future__ import annotations

import bisect
import collections
import functools
import http.client
import ipaddress
import random
import re
import ssl
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator, NamedTuple

from . import config, socks5
from .log import log

Proxy = tuple[str, int]

_LINE = re.compile(r"^(?:socks5h?://)?(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$")
_USER_AGENT = f"DiscordProxyLauncher/{config.APP_VERSION}"
_DISCORD_HOST = "discord.com"
_DISCORD_REQUEST = (
    "GET /api/v9/gateway HTTP/1.1\r\n"
    f"Host: {_DISCORD_HOST}\r\n"
    f"User-Agent: {_USER_AGENT}\r\n"
    "Accept: application/json\r\n"
    "Connection: close\r\n\r\n"
).encode("ascii")


@functools.lru_cache(maxsize=1)
def tls_context() -> ssl.SSLContext:
    """Contexto TLS com verificação de certificado (raízes do Windows + certifi)."""
    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except Exception:  # sem certifi, ficam só as raízes do Windows
        log.debug("certifi indisponível", exc_info=True)
    return context


# --- Download das listas -----------------------------------------------------

def parse_proxy_list(text: str) -> list[Proxy]:
    """Extrai as linhas `IP:PORTA` válidas, sem repetir e sem endereços não públicos."""
    seen: set[Proxy] = set()
    proxies: list[Proxy] = []
    for raw in text.splitlines():
        match = _LINE.match(raw.strip())
        if not match:
            continue
        try:
            ip = ipaddress.IPv4Address(match.group(1))
        except ValueError:
            continue
        port = int(match.group(2))
        # Uma lista pública não tem por que apontar para a rede local do usuário.
        if not ip.is_global or not 0 < port < 65536:
            continue
        proxy = (str(ip), port)
        if proxy not in seen:
            seen.add(proxy)
            proxies.append(proxy)
    return proxies


def fetch_text(url: str, timeout: float = config.FETCH_TIMEOUT) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout, context=tls_context()) as response:
        return response.read(5_000_000).decode("utf-8", "replace")


def download_lists(stop: threading.Event) -> Iterator[tuple[str, list[Proxy]]]:
    """Percorre as fontes na ordem da especificação e produz as listas válidas."""
    for name, url in config.PROXY_SOURCES:
        if stop.is_set():
            return
        try:
            proxies = parse_proxy_list(fetch_text(url))
        except (OSError, ValueError, http.client.HTTPException) as exc:
            log.info("fonte %s falhou: %s", name, exc)
            continue
        log.info("fonte %s: %d proxies", name, len(proxies))
        if proxies:
            yield name, proxies


# --- Teste de um proxy -------------------------------------------------------

# Falhas que provam que a rede local está de pé: houve resposta do outro lado.
_REMOTE_ANSWERED = (
    ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, socks5.ProxyError, ssl.SSLError,
)


class CheckResult(NamedTuple):
    connect_ms: float   # tempo até o proxy completar o TCP com o destino
    total_ms: float     # tempo até a resposta HTTP chegar


def discord_check(proxy: Proxy, timeout: float = config.TEST_TIMEOUT) -> CheckResult:
    """Confere se o Discord funciona de verdade através do proxy.

    Abrir a porta do proxy não basta: muitos proxies gratuitos aceitam a conexão
    e não entregam nada, ou têm o IP barrado pelo Discord. Por isso o teste faz
    uma requisição HTTPS real (certificado verificado) à API pública do Discord
    e só aprova com HTTP 200. Lança OSError se o proxy não servir.
    """
    started = time.perf_counter()       # relógio de alta resolução para a latência
    deadline = time.monotonic() + timeout
    sock = socks5.connect(proxy, _DISCORD_HOST, 443, deadline=deadline)
    connect_ms = (time.perf_counter() - started) * 1000.0
    try:
        sock.settimeout(socks5.time_left(deadline))
        tls = tls_context().wrap_socket(sock, server_hostname=_DISCORD_HOST)
    except BaseException:
        sock.close()
        raise
    try:
        tls.settimeout(socks5.time_left(deadline))
        tls.sendall(_DISCORD_REQUEST)
        head = b""
        while b"\r\n" not in head and len(head) < 1024:
            tls.settimeout(socks5.time_left(deadline))
            chunk = tls.recv(1024)
            if not chunk:
                break
            head += chunk
    finally:
        tls.close()
    status = head.split(b"\r\n", 1)[0].split()
    if len(status) < 2 or not status[0].startswith(b"HTTP/") or status[1] != b"200":
        code = status[1].decode("ascii", "replace") if len(status) > 1 else "?"
        raise socks5.ProxyError(f"discord.com respondeu {code}")
    return CheckResult(connect_ms, (time.perf_counter() - started) * 1000.0)


# --- Fila de proxies ---------------------------------------------------------

@dataclass(order=True)
class Approved:
    total_ms: float
    proxy: Proxy = field(compare=False)
    connect_ms: float = field(compare=False)
    tested_at: float = field(compare=False)

    @property
    def address(self) -> str:
        return f"{self.proxy[0]}:{self.proxy[1]}"


class ProxyPool:
    """Proxies aprovados (ordenados por latência) + os que ainda faltam testar."""

    def __init__(self, check: Callable[[Proxy], CheckResult] | None = None):
        self._check = check or discord_check
        self._lock = threading.Condition()
        self._slots = threading.BoundedSemaphore(config.TEST_CONCURRENCY)
        self._untested: collections.deque[Proxy] = collections.deque()
        self._approved: list[Approved] = []
        self._tested_at: dict[Proxy, float] = {}
        self._inflight = 0
        self._run = 0
        self._offline = False
        self.total = 0
        self.tested = 0

    def load(self, proxies: list[Proxy]) -> int:
        """Carrega uma lista nova para teste. Devolve quantos proxies entraram."""
        now = time.monotonic()
        with self._lock:
            standby = {entry.proxy for entry in self._approved}
            fresh = [
                proxy for proxy in proxies
                if proxy not in standby
                and now - self._tested_at.get(proxy, float("-inf")) > config.RETEST_AFTER
            ]
            # Ordem aleatória: espalha a carga em vez de todo mundo disputar os
            # primeiros proxies da lista.
            random.shuffle(fresh)
            self._untested = collections.deque(fresh)
            self.total = len(fresh)
            self.tested = 0
            return len(fresh)

    def has_untested(self) -> bool:
        with self._lock:
            return bool(self._untested)

    def standby_count(self) -> int:
        with self._lock:
            return len(self._approved)

    def pop_best(self) -> Approved | None:
        """Tira da fila o aprovado de menor latência."""
        with self._lock:
            return self._approved.pop(0) if self._approved else None

    def mark_failed(self, proxy: Proxy) -> None:
        """Um proxy que caiu em uso não volta para a fila tão cedo."""
        with self._lock:
            self._tested_at[proxy] = time.monotonic()
            self._approved = [entry for entry in self._approved if entry.proxy != proxy]

    def put_back(self, entry: Approved) -> None:
        """Devolve à reserva um aprovado que foi retirado mas não chegou a ser usado."""
        with self._lock:
            bisect.insort(self._approved, entry)

    def forget_failures(self) -> None:
        """Esquece as reprovações (elas aconteceram com a rede local fora do ar)."""
        with self._lock:
            self._tested_at.clear()

    def test_until(
        self,
        enough: int | None,
        stop: threading.Event,
        on_progress: Callable[[int, int, int], None] | None = None,
        is_offline: Callable[[float], bool] | None = None,
        on_offline: Callable[[], None] | None = None,
    ) -> None:
        """Testa os pendentes, no máximo TEST_CONCURRENCY por vez.

        Retorna quando houver `enough` aprovados (None = testar a lista toda),
        quando a lista acabar ou quando `stop` for acionado. `on_progress`
        recebe (testados, total, aprovados).

        `is_offline(instante_da_falha)` diz se a rede local caiu. Nesse caso os
        testes falham por culpa nossa, não dos proxies: eles voltam para a fila
        e o teste espera a rede voltar (`on_offline` avisa a interface) em vez
        de reprovar a lista inteira — o que, sem rota, levaria um segundo.
        """
        with self._lock:
            self._run += 1
            run = self._run
            self._offline = False
            workers = min(config.TEST_CONCURRENCY, len(self._untested))
        for index in range(workers):
            threading.Thread(
                target=self._worker,
                args=(run, enough, stop, on_progress, is_offline),
                name=f"proxy-test-{index}",
                daemon=True,
            ).start()
        announced = False
        with self._lock:
            while not stop.is_set():
                if enough is not None and len(self._approved) >= enough:
                    break
                if not self._untested and self._inflight == 0:
                    break
                if self._offline and not announced and on_offline is not None:
                    on_offline()
                announced = self._offline
                self._lock.wait(0.25)
            # Quem ainda está no meio de um teste termina em segundo plano (o
            # resultado entra na fila), mas não pega outro nem mexe no status.
            self._run += 1

    def _worker(self, run, enough, stop, on_progress, is_offline) -> None:
        while True:
            with self._lock:
                finished = (
                    stop.is_set()
                    or self._run != run
                    or not self._untested
                    or (enough is not None and len(self._approved) >= enough)
                )
                if finished:
                    self._lock.notify_all()
                    return
                proxy = self._untested.popleft()
                self._inflight += 1
            result = None
            answered = False                # alguém do outro lado respondeu, ainda que recusando?
            with self._slots:
                try:
                    result = self._check(proxy)
                except _REMOTE_ANSWERED:
                    answered = True
                except OSError:
                    pass
                except Exception:
                    answered = True
                    log.exception("teste do proxy %s:%s falhou de forma inesperada", *proxy)
            failed_at = time.perf_counter()
            # Sem resposta nenhuma (tempo esgotado, sem rota): antes de culpar o
            # proxy, confere se não foi a rede local que caiu.
            if result is None and not answered and is_offline is not None and is_offline(failed_at):
                with self._lock:
                    self._inflight -= 1
                    self._untested.appendleft(proxy)     # não foi testado de verdade
                    self._offline = True
                    self._lock.notify_all()
                stop.wait(2.0)
                continue
            with self._lock:
                now = time.monotonic()
                self._offline = False
                self._inflight -= 1
                self.tested += 1
                self._tested_at[proxy] = now
                if result is not None:
                    bisect.insort(
                        self._approved,
                        Approved(result.total_ms, proxy, result.connect_ms, now),
                    )
                # Dentro do lock: garante que nenhum progresso atrasado chegue
                # depois que test_until() já retornou.
                if on_progress is not None and self._run == run:
                    on_progress(self.tested, self.total, len(self._approved))
                self._lock.notify_all()
