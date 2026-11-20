"""Cliente do servidor dedicado (dconnect, em server/dconnect_server.py).

Cada conexão do Discord que vai pelo servidor dedicado vira um WebSocket por
HTTPS até o endereço do servidor (atrás de um Cloudflare Tunnel): o app manda
o token e o destino nos cabeçalhos, o servidor liga a conexão ao host do
Discord e devolve 101; daí em diante são só bytes, em frames binários.

O certificado do servidor é conferido como em qualquer site (raízes do Windows
+ certifi), com o nome do servidor: a Cloudflare troca os certificados de borda,
então não dá para fixá-los.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import NamedTuple
from urllib.parse import urlsplit

from . import config, proxies, socks5
from .log import log

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEAD = 16384
_CHUNK = 65536
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


# --- Configuração --------------------------------------------------------------------

@dataclass(frozen=True)
class DedicatedServer:
    url: str
    token: str = field(repr=False)              # nunca no log

    @property
    def _parts(self):
        return urlsplit(self.url)

    @property
    def secure(self) -> bool:
        return self._parts.scheme == "wss"

    @property
    def host(self) -> str:
        return self._parts.hostname or ""

    @property
    def port(self) -> int:
        return self._parts.port or (443 if self.secure else 80)

    @property
    def path(self) -> str:
        return self._parts.path or "/"

    @property
    def host_header(self) -> str:
        default = 443 if self.secure else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"


def parse_config(data) -> DedicatedServer:
    """Valida o conteúdo de dconnect.json. Lança ValueError com o motivo."""
    if not isinstance(data, dict):
        raise ValueError("esperava um objeto JSON")
    url, token = data.get("url"), data.get("token")
    if not isinstance(url, str) or not isinstance(token, str):
        raise ValueError("faltam 'url' e 'token'")
    parts = urlsplit(url)
    if parts.scheme not in ("wss", "ws") or not parts.hostname or not parts.path.startswith("/"):
        raise ValueError("'url' precisa ser wss://host/caminho")
    if parts.scheme == "ws" and parts.hostname not in _LOCAL_HOSTS:
        raise ValueError("ws:// (sem TLS) só é aceito para testes locais")
    if len(token) < 32 or not re.fullmatch(r"[A-Za-z0-9_\-]+", token):
        raise ValueError("'token' inválido")
    return DedicatedServer(url, token)


def load(path: str) -> DedicatedServer | None:
    """O servidor dedicado embutido no app, ou None se não houver (forks, código-fonte)."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(65536)
        # Salvo pelo PowerShell 5.1 ele pode vir em UTF-16 ou com BOM.
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
        data = json.loads(text)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("configuração do servidor dedicado ilegível: %s", exc)
        return None
    try:
        return parse_config(data)
    except ValueError as exc:
        log.warning("configuração do servidor dedicado inválida: %s", exc)
        return None


# --- Erros ------------------------------------------------------------------------------

class ServerError(OSError):
    """O servidor dedicado não abriu o túnel. `status`: o código HTTP, se houve resposta."""

    def __init__(self, message: str, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


class TokenRejected(ServerError):
    """Esta versão do app não é mais aceita (token trocado ou servidor reinstalado)."""


class DestinationRefused(ServerError):
    """O servidor não atende este destino (fora da lista dele)."""


class ServerBusy(ServerError):
    """Limite de uso atingido: o servidor está vivo, só não aceitou esta conexão agora."""


class DestinationUnreachable(ServerError):
    """O servidor não conseguiu chegar ao host do Discord."""


class ServerUnavailable(ServerError):
    """Sem resposta útil do servidor: fora do ar, túnel caído, rede, tempo esgotado."""


_TOKEN_CODES = frozenset({"token_invalido", "token_aposentado", "nao_encontrado", "token_invalido_demais"})
_REFUSED_CODES = frozenset({"destino_nao_permitido", "destino"})
_UNREACHABLE_CODES = frozenset({"destino_inalcancavel", "destino_sem_endereco"})
_BUSY_CODES = frozenset({"ritmo", "simultaneas", "gateways", "cota_diaria", "lotado"})


def _error_for(status: int, body: bytes) -> ServerError:
    """Classifica pela resposta do próprio dconnect (o campo "error" do JSON), não só pelo
    status: um 404 ou 502 da Cloudflare/cloudflared (túnel mal configurado, dconnect fora
    do ar) não pode virar "versão recusada" nem "servidor vivo"."""
    try:
        detail = json.loads(body.decode("utf-8", "replace") or "{}")
    except ValueError:
        detail = {}
    code = detail.get("error", "") if isinstance(detail, dict) else ""
    code = code if isinstance(code, str) else ""
    message = f"servidor dedicado respondeu {status}" + (f" ({code})" if code else "")
    if code in _TOKEN_CODES:
        return TokenRejected(message, status, code)
    if code in _REFUSED_CODES:
        return DestinationRefused(message, status, code)
    if code in _UNREACHABLE_CODES:
        return DestinationUnreachable(message, status, code)
    if code in _BUSY_CODES:
        return ServerBusy(message, status, code)
    return ServerUnavailable(message, status, code)


# --- Conexão até o servidor ---------------------------------------------------------------

def _connect(server: DedicatedServer, deadline: float):
    """TCP (+ TLS com certificado conferido) até o servidor."""
    try:
        infos = socket.getaddrinfo(server.host, server.port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ServerUnavailable(f"servidor dedicado: DNS falhou ({exc})") from exc
    addresses = []
    for info in infos:
        if info[4] not in [item[4] for item in addresses]:
            addresses.append(info)
    addresses.sort(key=lambda info: info[0] != socket.AF_INET)         # IPv4 primeiro
    sock = None
    last: OSError | None = None
    for index, (family, kind, proto, _, address) in enumerate(addresses):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        # Divide o prazo entre os endereços: um IPv6 que não responde não gasta tudo.
        share = left / max(1, min(3, len(addresses) - index))
        candidate = socket.socket(family, kind, proto)
        try:
            candidate.settimeout(share)
            candidate.connect(address)
            sock = candidate
            break
        except OSError as exc:
            candidate.close()
            last = exc
    if sock is None:
        raise ServerUnavailable(f"servidor dedicado inalcançável ({last or 'tempo esgotado'})")
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if server.secure:
            sock.settimeout(socks5.time_left(deadline))
            context = proxies.tls_context()
            sock = context.wrap_socket(sock, server_hostname=server.host)
        return sock
    except ssl.SSLCertVerificationError as exc:
        sock.close()
        raise ServerUnavailable(f"certificado do servidor dedicado não confere ({exc.verify_message})") from exc
    except (OSError, socks5.ProxyError) as exc:
        sock.close()
        raise ServerUnavailable(f"servidor dedicado: TLS falhou ({exc})") from exc


def _read_head(sock, deadline: float) -> tuple[int, dict, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > _MAX_HEAD:
            raise ServerUnavailable("resposta do servidor dedicado grande demais")
        sock.settimeout(socks5.time_left(deadline))
        chunk = sock.recv(4096)
        if not chunk:
            raise ServerUnavailable("o servidor dedicado fechou a conexão")
        data += chunk
    head, rest = data.split(b"\r\n\r\n", 1)
    lines = head.decode("latin-1").split("\r\n")
    match = re.match(r"HTTP/1\.[01] (\d{3})", lines[0])
    if not match:
        raise ServerUnavailable("resposta inválida do servidor dedicado")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers.setdefault(name.strip().lower(), value.strip())
    return int(match.group(1)), headers, rest


def _read_body(sock, headers: dict, rest: bytes, deadline: float, limit: int = 65536) -> bytes:
    """O corpo da resposta: por Content-Length, em pedaços (chunked) ou até o fim da conexão."""
    data = rest

    def more() -> bool:
        nonlocal data
        sock.settimeout(socks5.time_left(deadline))
        chunk = sock.recv(4096)
        data += chunk
        return bool(chunk)

    try:
        if "chunked" in headers.get("transfer-encoding", "").lower():
            body = b""
            while len(body) < limit:
                while b"\r\n" not in data:
                    if not more():
                        return body
                line, data = data.split(b"\r\n", 1)
                try:
                    size = int(line.split(b";")[0].strip() or b"0", 16)
                except ValueError:
                    return body
                if size == 0:
                    return body
                while len(data) < size + 2:
                    if not more():
                        return body + data[:size]
                body += data[:size]
                data = data[size + 2:]
            return body[:limit]
        if "content-length" in headers:
            try:
                size = min(int(headers["content-length"]), limit)
            except ValueError:
                size = 0
            while len(data) < size and more():
                pass
            return data[:size]
        while len(data) < limit and more():        # sem tamanho: até a conexão fechar
            pass
        return data[:limit]
    except (OSError, socks5.ProxyError):
        return data[:limit]


def _request(server: DedicatedServer, path: str, extra: dict[str, str]) -> bytes:
    lines = [f"GET {path} HTTP/1.1", f"Host: {server.host_header}",
             f"User-Agent: DiscordProxyLauncher/{config.APP_VERSION}",
             f"Authorization: Bearer {server.token}"]
    lines += [f"{name}: {value}" for name, value in extra.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


# --- O túnel ---------------------------------------------------------------------------

def open_tunnel(server: DedicatedServer, host: str, port: int, *, deadline: float) -> "WebSocketChannel":
    """Abre um túnel até host:port do Discord. Lança uma das ServerError."""
    sock = _connect(server, deadline)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock.settimeout(socks5.time_left(deadline))
        sock.sendall(_request(server, server.path, {
            "Upgrade": "websocket", "Connection": "Upgrade",
            "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13",
            "X-DPL-Target": f"{host}:{port}",
        }))
        status, headers, rest = _read_head(sock, deadline)
        if status != 101:
            raise _error_for(status, _read_body(sock, headers, rest, deadline, 2048))
        expected = base64.b64encode(hashlib.sha1(key.encode("ascii") + _WS_GUID).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected or headers.get("upgrade", "").lower() != "websocket":
            raise ServerUnavailable("resposta de WebSocket inválida do servidor dedicado")
        sock.settimeout(None)
        return WebSocketChannel(sock, rest)
    except TimeoutError as exc:
        sock.close()
        raise ServerUnavailable("servidor dedicado: tempo esgotado") from exc
    except ServerError:
        sock.close()
        raise
    except OSError as exc:
        sock.close()
        raise ServerUnavailable(f"servidor dedicado: {exc}") from exc


class WebSocketChannel:
    """O túnel com a interface de canal que o proxy local usa (recv/sendall/close)."""

    def __init__(self, sock, leftover: bytes = b""):
        self._sock = sock
        self._buffer = bytearray(leftover)
        self._pending = b""                  # dados de um frame já lido e ainda não entregues
        self._send_lock = threading.Lock()
        self._closed = False
        self._close_sent = False

    def settimeout(self, value) -> None:
        self._sock.settimeout(value)

    def _fill(self, needed: int) -> bool:
        while len(self._buffer) < needed:
            chunk = self._sock.recv(_CHUNK)
            if not chunk:
                return False
            self._buffer += chunk
        return True

    def _next_frame(self):
        """(opcode, payload) ou None no fim. O servidor nunca mascara os frames."""
        if not self._fill(2):
            return None
        first, second = self._buffer[0], self._buffer[1]
        if second & 0x80 or first & 0x70:
            raise ConnectionError("frame inválido do servidor dedicado")
        size, offset = second & 0x7F, 2
        if size == 126:
            if not self._fill(4):
                return None
            size, offset = struct.unpack(">H", bytes(self._buffer[2:4]))[0], 4
        elif size == 127:
            if not self._fill(10):
                return None
            size, offset = struct.unpack(">Q", bytes(self._buffer[2:10]))[0], 10
        if size > (1 << 22):
            raise ConnectionError("frame grande demais do servidor dedicado")
        if not self._fill(offset + size):
            return None
        payload = bytes(self._buffer[offset:offset + size])
        del self._buffer[:offset + size]
        return first & 0x0F, payload

    def recv(self, count: int) -> bytes:
        if self._pending:
            data, self._pending = self._pending[:count], self._pending[count:]
            return data
        while not self._closed:
            frame = self._next_frame()
            if frame is None:
                self._closed = True
                return b""
            opcode, payload = frame
            if opcode == 0x8:                               # close
                self._closed = True
                self._send_close()
                return b""
            if opcode == 0x9:                               # ping
                self._send(0xA, payload)
                continue
            if opcode in (0x0, 0x2) and payload:
                data, self._pending = payload[:count], payload[count:]
                return data
        return b""

    def _send(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = bytes([0x80 | opcode, 0x80 | size])
        elif size < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", size)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", size)
        with self._send_lock:
            self._sock.sendall(header + mask + _xor(payload, mask))

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        for start in range(0, len(view), _CHUNK):
            self._send(0x2, bytes(view[start:start + _CHUNK]))

    def _send_close(self, code: int = 1000) -> None:
        if self._close_sent:
            return
        self._close_sent = True
        try:
            self._send(0x8, struct.pack(">H", code))
        except OSError:
            pass

    def shutdown_write(self) -> None:
        # WebSocket não tem meio-fechamento. O Chromium só termina conexões TLS por
        # inteiro, então o fim do envio é o fim da conexão: avisa o servidor, que fecha
        # o lado do Discord (e não um SSLSocket.shutdown, que estragaria a leitura).
        self._send_close()

    def close(self) -> None:
        self._send_close()
        try:
            self._sock.close()
        except OSError:
            pass


def _xor(payload: bytes, mask: bytes) -> bytes:
    if not payload:
        return b""
    repeated = (mask * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(payload), "big")


# --- Verificações --------------------------------------------------------------------------

class Health(NamedTuple):
    latency_ms: float
    discord: str
    country: str


def health(server: DedicatedServer, timeout: float = config.HEALTH_TIMEOUT) -> Health:
    """O servidor responde por si (não toca no Discord a cada verificação de cada usuário)."""
    started = time.perf_counter()
    deadline = time.monotonic() + timeout
    sock = _connect(server, deadline)
    try:
        sock.settimeout(socks5.time_left(deadline))
        sock.sendall(_request(server, server.path + "/health", {"Connection": "close"}))
        status, headers, rest = _read_head(sock, deadline)
        body = _read_body(sock, headers, rest, deadline, 4096)
    except TimeoutError as exc:
        raise ServerUnavailable("servidor dedicado: tempo esgotado") from exc
    except ServerError:
        raise
    except OSError as exc:
        raise ServerUnavailable(f"servidor dedicado: {exc}") from exc
    finally:
        sock.close()
    if status != 200:
        raise _error_for(status, body)
    try:
        detail = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise ServerUnavailable("resposta inválida do servidor dedicado") from exc
    return Health((time.perf_counter() - started) * 1000.0, str(detail.get("discord", "")),
                  str(detail.get("loc", ""))[:2].upper())


class _TlsOverChannel:
    """TLS por cima de um canal qualquer (aqui, o túnel, que já roda dentro de outro TLS)."""

    def __init__(self, channel, server_hostname: str, deadline: float):
        self._channel, self._deadline = channel, deadline
        self._incoming, self._outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self._tls = proxies.tls_context().wrap_bio(self._incoming, self._outgoing, server_hostname=server_hostname)

    def _run(self, operation, *args):
        while True:
            try:
                result = operation(*args)
                break
            except ssl.SSLWantReadError:
                self._flush()
                self._channel.settimeout(socks5.time_left(self._deadline))
                data = self._channel.recv(_CHUNK)
                if not data:
                    raise socks5.ProxyError("o túnel fechou durante o TLS")
                self._incoming.write(data)
        self._flush()
        return result

    def _flush(self):
        pending = self._outgoing.read()
        if pending:
            self._channel.sendall(pending)

    def handshake(self):
        self._run(self._tls.do_handshake)

    def sendall(self, data: bytes):
        self._run(self._tls.write, data)

    def recv(self, count: int) -> bytes:
        try:
            return self._run(self._tls.read, count)
        except ssl.SSLZeroReturnError:
            return b""


class Check(NamedTuple):
    latency_ms: float        # ida e volta de um túnel novo até o Discord
    country: str             # país por onde o tráfego sai, segundo a Cloudflare


def check(server: DedicatedServer, timeout: float = config.VPS_CHECK_TIMEOUT) -> Check:
    """Verificação completa: o servidor responde, abre túnel e o Discord atende por ele."""
    state = health(server, min(timeout, config.HEALTH_TIMEOUT))
    if state.discord == "bloqueado":
        raise ServerUnavailable("o Discord não está atendendo o servidor dedicado")
    deadline = time.monotonic() + timeout
    started = time.perf_counter()
    channel = open_tunnel(server, proxies.TRACE_HOST, 443, deadline=deadline)
    latency = (time.perf_counter() - started) * 1000.0
    try:
        tls = _TlsOverChannel(channel, proxies.TRACE_HOST, deadline)
        tls.handshake()
        tls.sendall(proxies.TRACE_REQUEST)
        answer = b""
        while not proxies._trace_done(answer):
            chunk = tls.recv(4096)
            if not chunk:
                break
            answer += chunk
            if len(answer) > 65536:
                break
    except ssl.SSLCertVerificationError as exc:
        raise ServerUnavailable("o túnel não chegou ao Discord de verdade (certificado)") from exc
    except (OSError, ssl.SSLError) as exc:
        raise ServerUnavailable(f"o túnel não funcionou ({exc})") from exc
    finally:
        channel.close()
    status, country = proxies.parse_trace(answer)
    if status != 200:
        raise ServerUnavailable(f"o Discord respondeu {status} pelo servidor dedicado")
    return Check(latency, country or state.country)
