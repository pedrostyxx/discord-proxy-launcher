"""Cliente SOCKS5 mínimo (RFC 1928, sem autenticação, comando CONNECT).

É só o que o app precisa para testar um proxy: abrir uma conexão TCP através
dele e medir quanto tempo leva.
"""
from __future__ import annotations

import ipaddress
import socket
import struct
import time

_REPLIES = {
    1: "falha geral",
    2: "conexão não permitida",
    3: "rede inacessível",
    4: "host inacessível",
    5: "conexão recusada",
    6: "TTL expirado",
    7: "comando não suportado",
    8: "tipo de endereço não suportado",
}


class ProxyError(OSError):
    """O proxy respondeu, mas recusou ou não fala SOCKS5."""

    def __init__(self, message: str, reply: int | None = None):
        super().__init__(message)
        self.reply = reply


def time_left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("tempo esgotado")
    return left


def _recv_exact(sock: socket.socket, count: int, deadline: float) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        sock.settimeout(time_left(deadline))
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ProxyError("o proxy fechou a conexão")
        buf += chunk
    return bytes(buf)


def connect(proxy: tuple[str, int], host: str, port: int, *, deadline: float) -> socket.socket:
    """Abre uma conexão TCP para host:port através do proxy.

    Nomes de host são enviados ao proxy sem resolver (DNS remoto), como o
    Chromium faz com `socks5://`. Devolve o socket já pronto para uso.
    """
    sock = socket.create_connection(proxy, timeout=time_left(deadline))
    try:
        sock.settimeout(time_left(deadline))
        sock.sendall(b"\x05\x01\x00")                       # v5, 1 método: sem autenticação
        if _recv_exact(sock, 2, deadline) != b"\x05\x00":
            raise ProxyError("o proxy não aceita SOCKS5 sem autenticação")

        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            name = host.encode("idna")
            if not 0 < len(name) < 256:
                raise ProxyError("nome de host inválido") from None
            address = b"\x03" + bytes([len(name)]) + name
        else:
            address = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
        sock.settimeout(time_left(deadline))
        sock.sendall(b"\x05\x01\x00" + address + struct.pack(">H", port))

        version, reply, _, bound_type = _recv_exact(sock, 4, deadline)
        if version != 5:
            raise ProxyError("resposta inválida do proxy")
        if reply != 0:
            raise ProxyError(_REPLIES.get(reply, f"erro {reply}"), reply)
        # Descarta o endereço "bound" que acompanha a resposta.
        if bound_type == 1:
            size = 4
        elif bound_type == 4:
            size = 16
        elif bound_type == 3:
            size = _recv_exact(sock, 1, deadline)[0]
        else:
            raise ProxyError("resposta inválida do proxy")
        _recv_exact(sock, size + 2, deadline)
        return sock
    except BaseException:
        sock.close()
        raise


def probe(proxy: tuple[str, int], target: tuple[str, int], timeout: float) -> float:
    """Verificação de saúde: TCP até `target` (uma porta HTTP) através do proxy.

    Devolve a latência em ms ou lança OSError. Além de conectar, espera o começo
    de uma resposta HTTP: o `ssh -D` (e alguns proxies) respondem "conectado"
    antes de a conexão com o destino existir, então só o aceite do proxy não
    prova que o caminho está funcionando.
    """
    started = time.perf_counter()       # time.monotonic() no Windows só anda de ~16 em 16 ms
    deadline = time.monotonic() + timeout
    sock = connect(proxy, target[0], target[1], deadline=deadline)
    try:
        sock.settimeout(time_left(deadline))
        sock.sendall(f"HEAD / HTTP/1.1\r\nHost: {target[0]}\r\nConnection: close\r\n\r\n".encode("ascii"))
        if _recv_exact(sock, 5, deadline) != b"HTTP/":
            raise ProxyError("resposta inesperada do destino")
    finally:
        sock.close()
    return (time.perf_counter() - started) * 1000.0


def direct_internet(targets=(("1.1.1.1", 443), ("8.8.8.8", 443)), timeout: float = 3.0) -> bool:
    """True se o computador alcança a internet sem passar pelo proxy."""
    for target in targets:
        try:
            socket.create_connection(target, timeout=timeout).close()
            return True
        except OSError:
            continue
    return False
