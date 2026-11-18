"""Mede o Modo B contra as listas reais: python tests/probe_public.py [aprovados] [max_testes]

Não abre nem fecha o Discord — só baixa a lista e testa proxies.
"""
from __future__ import annotations

import os
import socket
import ssl
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from launcher import config, proxies, socks5  # noqa: E402


def direct_request() -> str:
    """A mesma requisição do teste, sem proxy, para validar o formato."""
    raw = socket.create_connection(("discord.com", 443), timeout=10)
    with proxies.tls_context().wrap_socket(raw, server_hostname="discord.com") as tls:
        tls.sendall(proxies._DISCORD_REQUEST)
        data = b""
        while chunk := tls.recv(4096):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return f"{head.splitlines()[0].decode()} | corpo: {body[:120]!r}"


def main() -> None:
    enough = int(sys.argv[1]) if len(sys.argv) > 1 else config.TEST_ENOUGH_APPROVED
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None

    try:
        print("sem proxy:", direct_request())
    except (OSError, ssl.SSLError) as exc:
        print("sem proxy: FALHOU", repr(exc))

    stop = threading.Event()
    reasons: dict[str, int] = {}

    def check(proxy):
        try:
            return proxies.discord_check(proxy)
        except OSError as exc:
            if isinstance(exc, socks5.ProxyError):
                key = f"proxy: {exc}"
            elif isinstance(exc, ssl.SSLError):
                key = f"tls: {exc.reason or type(exc).__name__}"
            elif isinstance(exc, TimeoutError):
                key = "timeout"
            else:
                key = type(exc).__name__
            reasons[key] = reasons.get(key, 0) + 1
            raise

    pool = proxies.ProxyPool(check)
    for name, found in proxies.download_lists(stop):
        if limit:
            found = found[:limit]
        print(f"fonte: {name} — {len(found)} proxies")
        pool.load(found)
        break
    else:
        print("nenhuma fonte respondeu")
        return

    start = time.monotonic()
    marks = {}

    def progress(tested, total, approved):
        if approved and approved not in marks:
            marks[approved] = (tested, time.monotonic() - start)

    pool.test_until(enough, stop, progress)
    elapsed = time.monotonic() - start
    print(f"testados {pool.tested}/{pool.total} em {elapsed:.1f}s — aprovados na fila: {pool.standby_count()}")
    for approved, (tested, at) in sorted(marks.items()):
        print(f"  {approved}º aprovado após {tested} testes, {at:.1f}s")
    time.sleep(config.TEST_TIMEOUT + 0.5)  # deixa os testes em andamento terminarem
    print("fila final (latência total / conexão):")
    while (entry := pool.pop_best()) is not None:
        print(f"  {entry.address:<22} {entry.total_ms:6.0f}ms / {entry.connect_ms:5.0f}ms")
    print("motivos de reprovação:")
    for key, count in sorted(reasons.items(), key=lambda item: -item[1])[:12]:
        print(f"  {count:4d}  {key}")


if __name__ == "__main__":
    main()
