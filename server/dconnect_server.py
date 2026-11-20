#!/usr/bin/env python3
"""dconnect — o "servidor dedicado" do Discord Proxy Launcher.

Roda numa VPS atrás de um Cloudflare Tunnel e só atende o app: cada conexão que
o Discord abre vira um WebSocket (wss://dconnect.../caminho) que este servidor
liga a um host do próprio Discord, na porta 443. Nada além disso.

Proteções (o token vai dentro de cada .exe e pode ser extraído, então todas
valem também para quem não é o app):
- token obrigatório (Authorization: Bearer); caminho aleatório; o resto é 404;
- só domínios do Discord (lista fixa, mesma do app), só a porta 443, nome
  validado byte a byte (um NUL no nome não engana o getaddrinfo);
- o nome também não pode apontar (CNAME) para fora da lista: alguns nomes do
  Discord levam a plataformas de terceiros (Zendesk, Statuspage), onde o mesmo
  IP atende outros clientes;
- o primeiro pacote do cliente precisa ser um TLS ClientHello cujo SNI é
  exatamente o destino pedido: os IPs do Discord são da Cloudflare e serviriam
  qualquer outro site da Cloudflare pelo SNI;
- nenhum IP interno, da própria VPS ou de faixas especiais como destino;
- limites por origem (conexões novas, simultâneas, de gateway, bytes por
  segundo e por dia; IPv6 por /64 e por /48), limite global, frames pequenos
  e prazo para cada frame, desligamento por inatividade.
Não grava destinos nem conteúdo; o log só tem números agregados.

Python 3.9+ (Debian 11), só biblioteca padrão. Uso:
  dconnect_server.py --config /etc/dconnect/config.json             (rodar)
  dconnect_server.py --config ... --init --public-host HOST          (criar a configuração uma vez)
  dconnect_server.py --config ... --client-config                    (JSON para compilar o app)
  dconnect_server.py --config ... --check                            (teste de ponta a ponta local)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.0.1"
log = logging.getLogger("dconnect")

# --- Destinos permitidos (cópia de launcher/destinations.py; os testes conferem) ---------

VPS_SUFFIXES = (
    "discord.com",
    "discord.gg",
    "discordapp.com",
    "discordapp.net",
    "discord.media",
    "hcaptcha.com",
)
VPS_EXCLUDED = frozenset({
    # Apontam (CNAME) para plataformas de terceiros que atendem outros clientes no mesmo IP.
    "support-dev.discord.com",          # Zendesk
    "creator-support.discord.com",      # Zendesk
    "support-apps.discord.com",         # Zendesk
    "status.hcaptcha.com",              # Atlassian Statuspage (CloudFront)
})
VPS_PORTS = frozenset({443})

_LABEL = rb"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rb"(?:" + _LABEL + rb"\.)+" + _LABEL)


def normalize_host(raw):
    if isinstance(raw, str):
        try:
            raw = raw.encode("ascii")
        except UnicodeEncodeError:
            return None
    if not isinstance(raw, (bytes, bytearray)):
        return None
    raw = bytes(raw)
    if raw.endswith(b"."):
        raw = raw[:-1]
    if not 1 <= len(raw) <= 253:
        return None
    raw = raw.lower()
    if not _HOSTNAME.fullmatch(raw):
        return None
    name = raw.decode("ascii")
    if name.rsplit(".", 1)[1].isdigit():
        return None
    return name


def goes_through_vps(host, port):
    name = normalize_host(host)
    if name is None or port not in VPS_PORTS or name in VPS_EXCLUDED:
        return False
    return any(name == suffix or name.endswith("." + suffix) for suffix in VPS_SUFFIXES)


def is_gateway(name):
    """Conexões de gateway: uma ou duas por cliente de verdade, muitas numa fazenda de bots.

    Conta tudo em discord.gg, não só os nomes "gateway...": dentro do TLS o
    cliente escolhe o Host, e a Cloudflare atende qualquer nome da zona
    discord.gg na mesma conexão (o app não usa discord.gg para outra coisa)."""
    return name == "discord.gg" or name.endswith(".discord.gg")


# --- IPs que nunca são destino ------------------------------------------------------------

# A lista do ipaddress muda entre versões do Python e deixa passar faixas como
# NAT64 e o metadata de algumas nuvens; por isso a lista explícita.
_DENIED = [ipaddress.ip_network(net) for net in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/3",
    "::/96", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64",
    "2001::/23", "2001:db8::/32", "2002::/16", "fc00::/7", "fe80::/10", "ff00::/8",
)]


def ip_allowed(address, extra_denied=()):
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    for net in list(_DENIED) + list(extra_denied):
        if ip.version == net.version and ip in net:
            return False
    return True


# --- TLS ClientHello: o SNI -------------------------------------------------------------

MAX_CLIENT_HELLO = 16384
MAX_HELLO_RECORDS = 4               # navegadores mandam o ClientHello num registro só


def client_hello_sni(data):
    """('ok', nome) | ('need', None) se faltam bytes | ('bad', motivo)."""
    try:
        position, handshake, records = 0, bytearray(), 0
        while True:
            if len(data) < position + 5:
                return "need", None
            if data[position] != 22 or data[position + 1] != 3:
                return "bad", "não é TLS"
            length = int.from_bytes(data[position + 3:position + 5], "big")
            if not 0 < length <= MAX_CLIENT_HELLO:
                return "bad", "registro TLS inválido"
            records += 1
            if records > MAX_HELLO_RECORDS:
                return "bad", "registros demais"
            if len(data) < position + 5 + length:
                return ("need", None) if position + 5 + length <= MAX_CLIENT_HELLO + 5 * 4 else ("bad", "grande demais")
            handshake += data[position + 5:position + 5 + length]
            position += 5 + length
            if len(handshake) >= 4:
                if handshake[0] != 1:
                    return "bad", "não é ClientHello"
                size = int.from_bytes(handshake[1:4], "big")
                if size > MAX_CLIENT_HELLO:
                    return "bad", "grande demais"
                if len(handshake) >= 4 + size:
                    body = handshake[4:4 + size]
                    break
            if position >= MAX_CLIENT_HELLO + 5 * 4:
                return "bad", "grande demais"

        cursor = 2 + 32                                      # versão + random
        cursor += 1 + body[cursor]                           # session id
        cursor += 2 + int.from_bytes(body[cursor:cursor + 2], "big")   # cipher suites
        cursor += 1 + body[cursor]                           # compressão
        if cursor + 2 > len(body):
            return "bad", "sem extensões"
        end = cursor + 2 + int.from_bytes(body[cursor:cursor + 2], "big")
        if end != len(body):
            return "bad", "extensões malformadas"
        cursor += 2
        names = []
        seen_sni = False
        while cursor < end:
            if cursor + 4 > end:
                return "bad", "extensão malformada"
            kind = int.from_bytes(body[cursor:cursor + 2], "big")
            size = int.from_bytes(body[cursor + 2:cursor + 4], "big")
            cursor += 4
            if cursor + size > end:
                return "bad", "extensão malformada"
            payload = body[cursor:cursor + size]
            cursor += size
            if kind != 0:
                continue
            if seen_sni:
                return "bad", "SNI repetido"
            seen_sni = True
            if len(payload) < 2 or int.from_bytes(payload[:2], "big") != len(payload) - 2:
                return "bad", "SNI malformado"
            inner = 2
            while inner < len(payload):
                if inner + 3 > len(payload):
                    return "bad", "SNI malformado"
                name_type = payload[inner]
                name_size = int.from_bytes(payload[inner + 1:inner + 3], "big")
                inner += 3
                if inner + name_size > len(payload):
                    return "bad", "SNI malformado"
                if name_type == 0:
                    names.append(payload[inner:inner + name_size])
                inner += name_size
        if len(names) != 1:
            return "bad", "sem SNI" if not names else "mais de um SNI"
        return "ok", bytes(names[0])
    except (IndexError, ValueError):
        return "bad", "ClientHello malformado"


# --- WebSocket (RFC 6455), o mínimo necessário ----------------------------------------------

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONTINUATION, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_FRAME = 65536                   # o app manda no máximo 64 KiB por frame (launcher/dconnect.py)
FRAME_COST = 4096                   # cada frame do cliente pesa ao menos isto no limite de bytes


class ProtocolError(Exception):
    pass


def ws_accept(key):
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + WS_GUID).digest()).decode("ascii")


def ws_frame(opcode, payload=b"", mask=None):
    header = bytes([0x80 | opcode])
    size = len(payload)
    mask_bit = 0x80 if mask is not None else 0
    if size < 126:
        header += bytes([mask_bit | size])
    elif size < 65536:
        header += bytes([mask_bit | 126]) + struct.pack(">H", size)
    else:
        header += bytes([mask_bit | 127]) + struct.pack(">Q", size)
    if mask is None:
        return header + payload
    return header + mask + _xor(payload, mask)


def _xor(payload, mask):
    if not payload:
        return b""
    repeated = (mask * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(payload), "big")


async def ws_read_frame(reader, progress=None):
    """(fin, opcode, payload) de um frame do cliente, que precisa vir mascarado.

    Com `progress`, marca em progress["frame_since"] quando um frame começou a
    chegar e ainda não terminou: quem vigia o túnel encerra frames que não acabam."""
    first, second = await reader.readexactly(2)
    if progress is not None:
        progress["frame_since"] = time.monotonic()
    if first & 0x70:
        raise ProtocolError("bits reservados")
    if not second & 0x80:
        raise ProtocolError("frame do cliente sem máscara")
    size = second & 0x7F
    if size == 126:
        size = struct.unpack(">H", await reader.readexactly(2))[0]
    elif size == 127:
        size = struct.unpack(">Q", await reader.readexactly(8))[0]
    opcode = first & 0x0F
    if opcode >= 0x8 and (size > 125 or not first & 0x80):
        raise ProtocolError("frame de controle inválido")
    if size > MAX_FRAME:
        raise ProtocolError("frame grande demais")
    mask = await reader.readexactly(4)
    payload = await reader.readexactly(size) if size else b""
    if progress is not None:
        progress["frame_since"] = None
    return bool(first & 0x80), opcode, _xor(payload, mask)


# --- Configuração -----------------------------------------------------------------------

DEFAULT_LIMITS = {
    "new_tunnels_burst": 120,          # conexões novas por origem: rajada...
    "new_tunnels_per_second": 2.0,     # ...e ritmo sustentado (o Discord abre ~30 ao carregar)
    "max_tunnels_per_source": 256,     # simultâneas por origem (CGNAT: muita gente num IP)
    "max_gateways_per_source": 24,     # conexões de gateway simultâneas por origem
    "bytes_per_second_per_source": 8 * 1024 * 1024,
    "bytes_burst_per_source": 32 * 1024 * 1024,
    "bytes_per_day_per_source": 40 * 1024 ** 3,
    "ipv6_48_scale": 4,                # o /48 inteiro tem 4x os limites de um /64
    "max_tunnels_total": 3000,         # ~0,5 MiB de buffers no pior caso cada: cabe em MemoryMax=2G
    "idle_timeout": 180.0,             # o gateway do Discord manda heartbeat a cada ~41 s
    "frame_timeout": 30.0,             # um frame começado precisa terminar (64 KiB a 17 kbit/s)
    "max_throttle_wait": 30.0,         # acima disso o túnel é encerrado em vez de esperar
    "connect_timeout": 6.0,            # menor que o prazo do app (10 s): o app recebe 502, não um tempo esgotado
    "request_timeout": 10.0,
    "client_hello_timeout": 15.0,
    "max_hello_frames": 16,            # o app manda o ClientHello em 1 ou 2 frames
    "bad_token_burst": 20,
    "bad_token_per_second": 0.05,
}
MAX_SOURCES = 50000                    # origens guardadas (as paradas mais antigas saem primeiro)
MAX_BAD_TOKEN_KEYS = 50000


class Config:
    def __init__(self, data):
        self.listen_host = data.get("listen_host", "127.0.0.1")
        self.listen_port = int(data.get("listen_port", 47787))
        self.path = data["path"]
        if not re.fullmatch(r"/[A-Za-z0-9/_-]{8,200}", self.path):
            raise ValueError("caminho inválido na configuração")
        self.public_url = data.get("public_url", "")
        self.tokens = {}
        for entry in data.get("tokens", []):
            token = entry["token"]
            if len(token) < 32:
                raise ValueError("token curto demais")
            self.tokens[token] = entry.get("label", "")
        if not self.tokens:
            raise ValueError("nenhum token na configuração")
        self.retired = {entry["sha256"]: entry.get("label", "") for entry in data.get("retired_tokens", [])}
        self.limits = dict(DEFAULT_LIMITS)
        self.limits.update(data.get("limits", {}))
        self.deny_networks = [ipaddress.ip_network(net, strict=False) for net in data.get("deny_networks", [])]
        self.canary = bool(data.get("canary", True))
        self.trust_cf_connecting_ip = bool(data.get("trust_cf_connecting_ip", True))

    @staticmethod
    def load(path):
        with open(path, encoding="utf-8") as handle:
            return Config(json.load(handle))


# --- Limites por origem -----------------------------------------------------------------

class Bucket:
    __slots__ = ("rate", "burst", "tokens", "stamp")

    def __init__(self, rate, burst):
        self.rate, self.burst = float(rate), float(burst)
        self.tokens = float(burst)
        self.stamp = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now

    def has(self, amount=1.0):
        self._refill()
        return self.tokens >= amount

    def take(self, amount=1.0):
        self._refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def wait_time(self, amount):
        """Reserva `amount` (pode ficar negativo) e diz quanto esperar para não passar do ritmo.

        Quem espera o tempo todo mantém o total no ritmo configurado, por mais
        túneis que a mesma origem tenha ao mesmo tempo."""
        self._refill()
        self.tokens -= amount
        return 0.0 if self.tokens >= 0 else -self.tokens / self.rate

    def refund(self, amount):
        self.tokens = min(self.burst, self.tokens + amount)


DAY = 86400.0


class Source:
    """Contadores de uma origem. `scale` > 1 no agregado de um IPv6 /48."""
    __slots__ = ("scale", "new_tunnels", "bytes", "tunnels", "gateways", "day_start", "day_bytes", "last_seen")

    def __init__(self, limits, scale=1):
        self.scale = scale
        self.new_tunnels = Bucket(limits["new_tunnels_per_second"] * scale, limits["new_tunnels_burst"] * scale)
        self.bytes = Bucket(limits["bytes_per_second_per_source"] * scale, limits["bytes_burst_per_source"] * scale)
        self.tunnels = 0
        self.gateways = 0
        self.day_start = time.monotonic()
        self.day_bytes = 0
        self.last_seen = time.monotonic()

    def _new_day(self):
        now = time.monotonic()
        if now - self.day_start >= DAY:
            self.day_start, self.day_bytes = now, 0

    def count_bytes(self, amount):
        self._new_day()
        self.day_bytes += amount

    def over_quota(self, limits):
        self._new_day()
        return self.day_bytes >= limits["bytes_per_day_per_source"] * self.scale

    def worth_keeping(self, now):
        """Túnel aberto ou cota do dia em andamento: apagar zeraria os limites."""
        return self.tunnels > 0 or (self.day_bytes > 0 and now - self.day_start < DAY)


def source_keys(address, wide_scale=4):
    """Chaves de limite de um endereço, como [(chave, escala)].

    IPv4: o próprio IP (um CGNAT inteiro divide a mesma chave). IPv6: o /64 (uma
    rede de casa, ou um celular: operadoras móveis dão um /64 por aparelho) e, por
    cima, o /48 inteiro com limites `wide_scale` vezes maiores. Sem o /48, quem
    tem um (de graça em túneis IPv6) viraria milhares de origens e multiplicaria
    todos os limites."""
    try:
        ip = ipaddress.ip_address(address)
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.version == 4:
            return [(str(ip), 1)]
        return [(str(ipaddress.IPv6Network((int(ip), 64), strict=False)), 1),     # int(): sem %zona
                (str(ipaddress.IPv6Network((int(ip), 48), strict=False)), wide_scale)]
    except ValueError:
        return [("?", 1)]


# --- O servidor -------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message


class TunnelLimit(Exception):
    """Encerra um túnel já aberto (cota do dia, ritmo impossível de cumprir)."""


REASONS = {101: "Switching Protocols", 200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
           404: "Not Found", 405: "Method Not Allowed", 410: "Gone", 429: "Too Many Requests",
           431: "Request Header Fields Too Large", 502: "Bad Gateway", 503: "Service Unavailable"}


class Server:
    def __init__(self, config, resolver=None, allow_private_for_tests=False):
        # `resolver` e `allow_private_for_tests` existem só para os testes, que usam
        # destinos em 127.0.0.1; a linha de comando nunca os liga.
        self.config = config
        self._allow_private = allow_private_for_tests
        self.limits = config.limits
        self.sources = collections.OrderedDict()        # só de quem passou pelo token
        self.bad_tokens = collections.OrderedDict()     # tentativas com token errado, por origem
        self.tunnels = 0
        self.stats = collections.Counter()
        self.discord_state = "desconhecido"
        self.exit_country = ""
        self._canary_failures = 0
        self._resolver = resolver
        self._dns_cache = {}
        self._dns_slots = None
        self._server = None
        self._tasks = set()
        self._stopping = False
        self._max_total = int(self.limits["max_tunnels_total"])

    # -- ciclo de vida --

    async def start(self):
        self._dns_slots = asyncio.Semaphore(32)
        self._apply_fd_limit()
        self._server = await asyncio.start_server(
            self._handle, self.config.listen_host, self.config.listen_port, limit=65536, backlog=512,
        )
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(self._loop_exception)
        for coroutine in (self._housekeeping(), self._canary()):
            task = asyncio.ensure_future(coroutine)
            self._tasks.add(task)
        log.info("dconnect %s escutando em %s:%d (até %d túneis)", VERSION,
                 self.config.listen_host, self.config.listen_port, self._max_total)
        return self._server

    def _apply_fd_limit(self):
        try:
            import resource
        except ImportError:                 # Windows (testes)
            return
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        try:
            if soft < hard:
                resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
                soft = hard
        except (ValueError, OSError):
            pass
        self._max_total = max(16, min(self._max_total, (soft - 256) // 2))

    async def stop(self, drain=20.0):
        self._stopping = True
        if self._server is not None:
            self._server.close()            # para de aceitar; os túneis abertos seguem até acabar
        deadline = time.monotonic() + drain
        while self.tunnels and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        for task in list(self._tasks):
            task.cancel()
        if self._server is not None:
            try:
                # No 3.12+ wait_closed() espera todas as conexões; não deixa travar o desligamento.
                await asyncio.wait_for(self._server.wait_closed(), 1)
            except (asyncio.TimeoutError, Exception):
                pass

    def _loop_exception(self, loop, context):
        # Nada de traceback por conexão: só conta.
        self.stats["erro_interno"] += 1
        exception = context.get("exception")
        if exception is not None and not isinstance(exception, (OSError, asyncio.IncompleteReadError)):
            log.warning("erro interno: %s", type(exception).__name__)

    async def _housekeeping(self):
        while True:
            await asyncio.sleep(600)
            now = time.monotonic()
            for key in [key for key, source in self.sources.items()
                        if not source.worth_keeping(now) and now - source.last_seen > 3600]:
                del self.sources[key]
            for key in [key for key, bucket in self.bad_tokens.items() if now - bucket.stamp > 3600]:
                del self.bad_tokens[key]
            for key in [key for key, (expires, _) in self._dns_cache.items() if expires < now]:
                del self._dns_cache[key]
            summary = ", ".join(f"{name}={count}" for name, count in sorted(self.stats.items()))
            log.info("estatísticas (10 min): túneis ativos=%d, origens=%d, discord=%s; %s",
                     self.tunnels, len(self.sources), self.discord_state, summary or "sem movimento")
            self.stats.clear()

    async def _canary(self):
        """Uma vez por minuto, a VPS confere se o Discord ainda a atende (429/403 = IP bloqueado)."""
        if not self.config.canary:
            self.discord_state = "ok"
            return
        last_trace = 0.0
        while True:
            status = await asyncio.get_running_loop().run_in_executor(None, _http_status,
                                                                      "https://discord.com/api/v9/gateway")
            if status == 200:
                self._canary_failures = 0
                if self.discord_state != "ok":
                    log.info("o Discord responde a esta VPS")
                self.discord_state = "ok"
            else:
                self._canary_failures += 1
                if self._canary_failures >= 2 and self.discord_state != "bloqueado":
                    log.warning("o Discord não está atendendo esta VPS (resposta %s)", status)
                    self.discord_state = "bloqueado"
            if time.monotonic() - last_trace > 3600 or not self.exit_country:
                trace = await asyncio.get_running_loop().run_in_executor(None, _cloudflare_trace)
                if trace.get("loc"):
                    self.exit_country = trace["loc"]
                    last_trace = time.monotonic()
            await asyncio.sleep(60)

    # -- requisições --

    def _client_address(self, headers, peer):
        address = peer[0] if peer else "?"
        if self.config.trust_cf_connecting_ip and headers.get("cf-connecting-ip"):
            address = headers["cf-connecting-ip"].strip()
        return address

    def _keys(self, address):
        return source_keys(address, self.limits["ipv6_48_scale"])

    def _sources_for(self, address):
        """As origens (IPv4; ou IPv6 /64 e /48) de um pedido já autorizado."""
        now = time.monotonic()
        found = []
        for key, scale in self._keys(address):
            source = self.sources.get(key)
            if source is None:
                self._make_room()
                source = self.sources[key] = Source(self.limits, scale)
            else:
                self.sources.move_to_end(key)
            source.last_seen = now
            found.append(source)
        return found

    def _make_room(self):
        """Tabela cheia: tira as origens paradas há mais tempo. Uma com túnel aberto
        nunca sai, senão a próxima conexão dela começaria com os limites zerados."""
        if len(self.sources) < MAX_SOURCES:
            return
        keep = MAX_SOURCES * 9 // 10
        for key in [key for key, source in self.sources.items() if source.tunnels == 0]:
            if len(self.sources) <= keep:
                break
            del self.sources[key]

    def _bad_token_allowed(self, address):
        key = self._keys(address)[-1][0]          # a chave mais larga (o /48 no IPv6)
        bucket = self.bad_tokens.get(key)
        if bucket is None:
            while len(self.bad_tokens) >= MAX_BAD_TOKEN_KEYS:
                self.bad_tokens.popitem(last=False)
            bucket = self.bad_tokens[key] = Bucket(self.limits["bad_token_per_second"],
                                                   self.limits["bad_token_burst"])
        else:
            self.bad_tokens.move_to_end(key)
        return bucket.take()

    async def _handle(self, reader, writer):
        try:
            await self._serve(reader, writer)
        except Exception as exc:           # nada escapa: sem traceback, só a contagem
            self.stats["erro_" + type(exc).__name__] += 1
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _serve(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.limits["request_timeout"])
        except (asyncio.LimitOverrunError, ValueError):
            await self._respond(writer, HttpError(431, "cabecalho_grande"))
            return
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            self.stats["requisicao_incompleta"] += 1
            return
        try:
            method, path, headers = parse_request(head)
        except HttpError as error:
            await self._respond(writer, error)
            return
        address = self._client_address(headers, writer.get_extra_info("peername"))
        try:
            if path == self.config.path + "/health":
                self._authorize(headers, address)
                await self._health(writer)
            elif path == self.config.path:
                if method != "GET":
                    raise HttpError(405, "metodo")
                label = self._authorize(headers, address)
                await self._tunnel(reader, writer, headers, self._sources_for(address), label)
            else:
                raise HttpError(404, "nao_encontrado")
        except HttpError as error:
            self.stats["recusa_" + error.code] += 1
            await self._respond(writer, error)

    def _authorize(self, headers, address):
        value = headers.get("authorization", "")
        token = value[7:].strip() if value[:7].lower() == "bearer " else ""
        label = None
        for known, known_label in self.config.tokens.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                label = known_label
        if label is not None:
            return label
        digest = hashlib.sha256(token.encode()).hexdigest()
        if any(hmac.compare_digest(digest, retired) for retired in self.config.retired):
            raise HttpError(410, "token_aposentado", "esta versão do app não é mais aceita; atualize")
        if not self._bad_token_allowed(address):
            raise HttpError(429, "token_invalido_demais")
        raise HttpError(401, "token_invalido")

    async def _health(self, writer):
        if self._stopping:
            raise HttpError(503, "encerrando")
        if self.discord_state == "bloqueado":
            raise HttpError(503, "discord_bloqueado", "o Discord não está atendendo o servidor")
        body = {"ok": True, "version": VERSION, "discord": self.discord_state, "loc": self.exit_country}
        await self._respond(writer, None, 200, body)

    async def _respond(self, writer, error, status=None, body=None):
        if error is not None:
            status = error.status
            body = {"ok": False, "error": error.code}
            if error.message:
                body["message"] = error.message
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        head = (f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\n"
                "Content-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode("ascii")
        try:
            writer.write(head + payload)
            await asyncio.wait_for(writer.drain(), 5)
        except Exception:
            pass

    # -- túnel --

    async def _tunnel(self, reader, writer, headers, sources, label):
        if self._stopping:
            raise HttpError(503, "encerrando")
        if "websocket" not in headers.get("upgrade", "").lower() or "upgrade" not in headers.get("connection", "").lower():
            raise HttpError(400, "sem_upgrade")
        if headers.get("sec-websocket-version") != "13":
            raise HttpError(400, "versao_websocket")
        key = headers.get("sec-websocket-key", "")
        try:
            if len(base64.b64decode(key, validate=True)) != 16:
                raise ValueError
        except ValueError:
            raise HttpError(400, "chave_websocket") from None
        host, port = parse_target(headers.get("x-dpl-target", ""))
        if not goes_through_vps(host, port):
            raise HttpError(403, "destino_nao_permitido")
        if self.discord_state == "bloqueado":
            raise HttpError(503, "discord_bloqueado")
        gateway = is_gateway(host)
        if self.tunnels >= self._max_total:
            raise HttpError(503, "lotado")
        for source in sources:
            if source.tunnels >= self.limits["max_tunnels_per_source"] * source.scale:
                raise HttpError(429, "simultaneas")
            if gateway and source.gateways >= self.limits["max_gateways_per_source"] * source.scale:
                raise HttpError(429, "gateways")
            if source.over_quota(self.limits):
                raise HttpError(429, "cota_diaria")
        if not all(source.new_tunnels.has() for source in sources):
            raise HttpError(429, "ritmo")
        for source in sources:
            source.new_tunnels.take()

        self.tunnels += 1
        for source in sources:
            source.tunnels += 1
            if gateway:
                source.gateways += 1
        target_writer = None
        try:
            target_reader, target_writer = await self._connect(host, port)
            writer.write((
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {ws_accept(key)}\r\n\r\n"
            ).encode("ascii"))
            await asyncio.wait_for(writer.drain(), 5)
            first = await self._client_hello(reader, writer, host)
            if first is None:
                return
            self.stats["tuneis"] += 1
            target_writer.write(first)
            await self._pump(reader, writer, target_reader, target_writer, sources, len(first))
        finally:
            self.tunnels -= 1
            for source in sources:
                source.tunnels -= 1
                if gateway:
                    source.gateways -= 1
            if target_writer is not None:
                try:
                    target_writer.close()
                except Exception:
                    pass

    async def _connect(self, host, port):
        addresses = await self._resolve(host, port)
        if addresses is None:
            raise HttpError(403, "destino_nao_permitido")
        if not addresses:
            raise HttpError(502, "destino_sem_endereco")
        last = None
        deadline = time.monotonic() + self.limits["connect_timeout"]
        for address in addresses[:4]:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                return await asyncio.wait_for(asyncio.open_connection(address, port), left)
            except (OSError, asyncio.TimeoutError) as exc:
                last = exc
        raise HttpError(502, "destino_inalcancavel", type(last).__name__ if last else "")

    async def _resolve(self, host, port):
        """Endereços permitidos de `host` ([] se nenhum), ou None se o nome aponta
        (CNAME) para fora da lista: ex. support-apps.discord.com -> ...zendesk.com,
        uma plataforma que atende outros clientes no mesmo IP."""
        now = time.monotonic()
        cached = self._dns_cache.get(host)
        if cached and cached[0] > now:
            return cached[1]
        canonical = ""
        async with self._dns_slots:
            try:
                if self._resolver is not None:
                    answer = await self._resolver(host, port)
                    canonical, raw = answer if isinstance(answer, tuple) else ("", answer)
                else:
                    infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                        host + ".", port, type=socket.SOCK_STREAM, flags=socket.AI_CANONNAME), 5)
                    canonical = next((info[3] for info in infos if info[3]), "")
                    raw = [info[4][0] for info in infos]
            except (OSError, UnicodeError, asyncio.TimeoutError):
                raw = []
        if canonical and normalize_host(canonical) != host and not goes_through_vps(canonical, 443):
            self.stats["cname_fora_da_lista"] += 1
            allowed = None
        else:
            allowed = []
            for address in raw:
                if address not in allowed and (self._allow_private or ip_allowed(address, self.config.deny_networks)):
                    allowed.append(address)
            # IPv4 primeiro: os hosts do Discord quase não têm IPv6, e a VPS pode ter IPv6 sem rota.
            allowed.sort(key=lambda address: ":" in address)
        self._dns_cache[host] = (now + (60 if allowed else 20), allowed)
        if len(self._dns_cache) > 5000:
            self._dns_cache.clear()
        return allowed

    async def _client_hello(self, reader, writer, host):
        """Lê o ClientHello do cliente e confere o SNI antes de repassar qualquer byte.

        Poucos frames, nenhum vazio, e a vez do loop é devolvida a cada frame: sem
        isso um cliente com o token prenderia o servidor inteiro mandando o
        ClientHello em milhares de pedacinhos."""
        buffered = bytearray()
        deadline = time.monotonic() + self.limits["client_hello_timeout"]
        for _ in range(int(self.limits["max_hello_frames"])):
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                fin, opcode, payload = await asyncio.wait_for(ws_read_frame(reader), left)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ProtocolError, ConnectionError):
                self.stats["sni_tempo"] += 1
                return None
            await asyncio.sleep(0)          # no 3.12+, frames já no buffer não soltam o loop
            if opcode == OP_CLOSE:
                return None
            if opcode == OP_PING:
                await self._ws_send(writer, OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode not in (OP_BINARY, OP_CONTINUATION):
                await self._ws_close(writer, 1003)
                return None
            if not payload:
                self.stats["hello_invalido"] += 1
                await self._ws_close(writer, 1008)
                return None
            buffered += payload
            verdict, name = client_hello_sni(buffered)
            if verdict == "need":
                continue
            if verdict == "ok" and normalize_host(name) == host:
                return bytes(buffered)
            self.stats["sni_recusado"] += 1
            await self._ws_close(writer, 1008)
            return None
        else:
            self.stats["hello_invalido"] += 1          # frames demais
            await self._ws_close(writer, 1008)
            return None
        self.stats["sni_tempo"] += 1
        await self._ws_close(writer, 1008)
        return None

    async def _pump(self, reader, writer, target_reader, target_writer, sources, sent):
        state = {"last": time.monotonic(), "frame_since": None}
        for source in sources:
            source.count_bytes(sent)
        idle = self.limits["idle_timeout"]
        frame_timeout = self.limits["frame_timeout"]

        async def upstream():                  # app -> Discord
            frames = 0
            while True:
                fin, opcode, payload = await ws_read_frame(reader, state)
                frames += 1
                if frames % 16 == 0:
                    await asyncio.sleep(0)     # no 3.12+, frames já no buffer não soltam o loop
                # Todo frame pesa no limite de bytes, até os vazios e os de controle:
                # sem isso, frames minúsculos gastariam CPU sem limite nenhum.
                if opcode == OP_CLOSE:
                    return
                if opcode == OP_PING:
                    await self._throttle(sources, 0, FRAME_COST)
                    await self._ws_send(writer, OP_PONG, payload)
                    continue
                if opcode not in (OP_BINARY, OP_CONTINUATION, OP_PONG):
                    raise ProtocolError("tipo de frame")
                await self._throttle(sources, len(payload), max(len(payload), FRAME_COST))
                if payload and opcode != OP_PONG:
                    state["last"] = time.monotonic()
                    target_writer.write(payload)
                    await target_writer.drain()

        async def downstream():                # Discord -> app
            while True:
                data = await target_reader.read(65536)
                if not data:
                    return
                state["last"] = time.monotonic()
                await self._throttle(sources, len(data))
                writer.write(ws_frame(OP_BINARY, data))
                await writer.drain()

        async def watchdog():                  # sem bytes em nenhum sentido (inclui drain travado)
            step = max(0.2, min(5.0, idle / 3, frame_timeout / 3))
            while True:
                await asyncio.sleep(step)
                now = time.monotonic()
                if now - state["last"] > idle:
                    self.stats["inativo"] += 1
                    return
                started = state["frame_since"]
                if started is not None and now - started > frame_timeout:
                    self.stats["frame_incompleto"] += 1      # começou um frame e não terminou
                    return

        tasks = [asyncio.ensure_future(job()) for job in (upstream, downstream, watchdog)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except BaseException:
                    pass
            await self._ws_close(writer, 1000)

    async def _throttle(self, sources, amount, cost=None):
        """Conta `amount` bytes na cota do dia e espera o necessário para o ritmo.

        A espera é a inteira: ela já reserva os bytes, então os túneis de uma
        mesma origem fazem fila e o total fica no limite. Quem precisaria esperar
        demais, ou passou da cota do dia, tem o túnel encerrado."""
        cost = amount if cost is None else cost
        self.stats["bytes"] += amount
        wait = 0.0
        for source in sources:
            source.count_bytes(amount)
            if source.over_quota(self.limits):
                self.stats["cota_diaria_atingida"] += 1
                raise TunnelLimit("cota do dia")
            wait = max(wait, source.bytes.wait_time(cost))
        if wait > self.limits["max_throttle_wait"]:
            for source in sources:
                source.bytes.refund(cost)
            self.stats["ritmo_excedido"] += 1
            raise TunnelLimit("ritmo")
        if wait > 0:
            await asyncio.sleep(wait)

    async def _ws_send(self, writer, opcode, payload=b""):
        try:
            writer.write(ws_frame(opcode, payload))
            await asyncio.wait_for(writer.drain(), 5)
        except Exception:
            pass

    async def _ws_close(self, writer, code):
        await self._ws_send(writer, OP_CLOSE, struct.pack(">H", code))


def parse_request(head):
    try:
        text = head.decode("latin-1")
    except Exception:
        raise HttpError(400, "requisicao") from None
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise HttpError(400, "requisicao")
    method, path = parts[0], parts[1].split("?", 1)[0]
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator or not name or name != name.strip():
            raise HttpError(400, "cabecalho")
        headers.setdefault(name.lower(), value.strip())
    return method, path, headers


def parse_target(value):
    host, separator, port = value.rpartition(":")
    if not separator or not port.isdigit() or len(port) > 5:
        raise HttpError(400, "destino")
    name = normalize_host(host)
    if name is None:
        raise HttpError(403, "destino_nao_permitido")
    return name, int(port)


def _http_status(url, timeout=10):
    request = urllib.request.Request(url, headers={"User-Agent": f"dconnect/{VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except Exception as error:
        return type(error).__name__


def _cloudflare_trace():
    request = urllib.request.Request("https://gateway.discord.gg/cdn-cgi/trace",
                                     headers={"User-Agent": f"dconnect/{VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=10, context=ssl.create_default_context()) as response:
            lines = response.read(4096).decode("ascii", "replace").splitlines()
    except Exception:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line)


# --- Linha de comando ---------------------------------------------------------------------

def init_config(path, public_host, port):
    """Cria a configuração só se ela não existir: trocar o token ou o caminho
    desligaria o servidor para todo app já distribuído."""
    if os.path.exists(path):
        print(f"{path} já existe; mantida sem alterações")
        return
    host = normalize_host(public_host)
    if host is None:
        raise SystemExit("--public-host inválido")
    data = {
        "listen_host": "127.0.0.1",
        "listen_port": port,
        "path": "/t/" + secrets.token_hex(12),
        "public_url": f"wss://{host}",
        "tokens": [{"label": "app", "token": secrets.token_urlsafe(32)}],
        "retired_tokens": [],
        "deny_networks": [],
        "limits": {},
        "canary": True,
    }
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    print(f"{path} criado")


def client_config(config):
    token = next(iter(config.tokens))
    return {"url": config.public_url.rstrip("/") + config.path, "token": token}


def local_check(config):
    """Do próprio servidor: health + um túnel até o Discord com TLS de verdade."""
    base = f"http://127.0.0.1:{config.listen_port}{config.path}"
    token = next(iter(config.tokens))
    request = urllib.request.Request(base + "/health", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        health = json.loads(response.read())
    print("health:", health)
    sock = socket.create_connection(("127.0.0.1", config.listen_port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f"GET {config.path} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer {token}\r\n"
                  "X-DPL-Target: gateway.discord.gg:443\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise SystemExit("o servidor fechou a conexão")
        head += chunk
    head, rest = head.split(b"\r\n\r\n", 1)
    if not head.startswith(b"HTTP/1.1 101"):
        raise SystemExit("túnel recusado: " + head.split(b"\r\n", 1)[0].decode())
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname="gateway.discord.gg")
    buffer = bytearray(rest)

    def send(data):
        sock.sendall(ws_frame(OP_BINARY, data, mask=os.urandom(4)))

    def receive():
        nonlocal buffer
        while True:
            if len(buffer) >= 2:
                size = buffer[1] & 0x7F
                offset = 2
                if size == 126:
                    size, offset = struct.unpack(">H", bytes(buffer[2:4]))[0], 4
                elif size == 127:
                    size, offset = struct.unpack(">Q", bytes(buffer[2:10]))[0], 10
                if len(buffer) >= offset + size:
                    payload = bytes(buffer[offset:offset + size])
                    del buffer[:offset + size]
                    return payload
            chunk = sock.recv(65536)
            if not chunk:
                return b""
            buffer += chunk

    def pump(operation, *args):
        while True:
            try:
                result = operation(*args)
                break
            except ssl.SSLWantReadError:
                pending = outgoing.read()
                if pending:
                    send(pending)
                data = receive()
                if not data:
                    raise SystemExit("o túnel fechou durante o TLS")
                incoming.write(data)
        pending = outgoing.read()
        if pending:
            send(pending)
        return result

    pump(tls.do_handshake)
    pump(tls.write, b"GET /cdn-cgi/trace HTTP/1.1\r\nHost: gateway.discord.gg\r\nConnection: close\r\n\r\n")
    answer = b""
    while b"loc=" not in answer:
        try:
            answer += pump(tls.read, 4096)
        except (ssl.SSLZeroReturnError, SystemExit):
            break
    sock.close()
    loc = re.search(rb"\nloc=([A-Z]{2})", answer)
    print("túnel até o Discord: ok, saída", loc.group(1).decode() if loc else "?")


def main(argv=None):
    parser = argparse.ArgumentParser(description="dconnect: servidor dedicado do Discord Proxy Launcher")
    parser.add_argument("--config", required=True)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--public-host")
    parser.add_argument("--port", type=int, default=47787)
    parser.add_argument("--client-config", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    if args.init:
        if not args.public_host:
            raise SystemExit("--init precisa de --public-host")
        init_config(args.config, args.public_host, args.port)
        return 0
    config = Config.load(args.config)
    if args.client_config:
        print(json.dumps(client_config(config), indent=2))
        return 0
    if args.check:
        local_check(config)
        return 0
    return asyncio.run(_run(config))


async def _run(config):
    server = Server(config)
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            try:
                loop.add_signal_handler(getattr(signal, name), stop.set)
            except NotImplementedError:
                pass
    await stop.wait()
    # Espera curta: túneis do Discord duram horas e cairiam de qualquer jeito. Com o
    # serviço fora do ar só ~4 s, um reinício (atualização) fica abaixo do tempo em que
    # o app conta falhas contra o servidor (VPS_SUSPECT_SPAN = 10 s) e ninguém troca de rota.
    log.info("encerrando: esperando os túneis ativos (até 3 s)")
    await server.stop(drain=3.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
