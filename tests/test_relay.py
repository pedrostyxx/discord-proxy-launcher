"""O proxy local do modo Proxy público (relay.py) com proxies SOCKS5 de verdade, locais.

Cada "proxy público" é um servidor SOCKS5 em 127.0.0.1 com um comportamento:
funciona, recusa, trava sem responder ou nem existe. O destino é um servidor de
eco local. O Discord é este teste, conectando ao relay como o Chromium faria.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
import unittest

import fakes
from launcher import config, proxies, relay, socks5


class EchoServer:
    """Destino final: devolve o que receber."""

    def __init__(self):
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(client,), daemon=True).start()

    @staticmethod
    def _echo(client):
        with client:
            while True:
                try:
                    data = client.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                client.sendall(data)

    def close(self):
        self._listener.close()


class FakeUpstream:
    """Um "proxy público" SOCKS5 local. mode: ok, refuse (resposta 1), unreachable (4) ou hang."""

    def __init__(self, mode="ok", delay=0.0, redirect=None):
        self.mode, self.delay = mode, delay
        self.redirect = redirect        # conecta aqui em vez do destino pedido (nada sai para a internet)
        self.requests = 0
        self._sockets = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self.proxy = ("127.0.0.1", self.port)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            self._sockets.append(client)
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        try:
            if self.mode == "hang":
                time.sleep(60)
                return
            version, count = client.recv(2)
            client.recv(count)
            client.sendall(b"\x05\x00")
            header = fakes_recv(client, 4)
            if header[3] == 1:
                host = socket.inet_ntoa(fakes_recv(client, 4))
            else:
                host = fakes_recv(client, fakes_recv(client, 1)[0]).decode()
            port = struct.unpack(">H", fakes_recv(client, 2))[0]
            self.requests += 1
            time.sleep(self.delay)
            if self.mode in ("refuse", "unreachable"):
                code = 1 if self.mode == "refuse" else 4
                client.sendall(b"\x05" + bytes([code]) + b"\x00\x01" + b"\x00" * 6)
                return
            target = socket.create_connection(self.redirect or (host, port), timeout=5)
            client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            threading.Thread(target=fakes._pump, args=(target.recv, client.sendall, lambda: None), daemon=True).start()
            fakes._pump(client.recv, target.sendall, lambda: None)
        except OSError:
            pass

    def close(self):
        self._listener.close()
        for sock in self._sockets:
            try:
                sock.close()
            except OSError:
                pass


def fakes_recv(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise OSError("fechou")
        data += chunk
    return data


def approved(upstream, latency=100.0):
    return proxies.Approved(latency, upstream.proxy, latency / 2, time.monotonic())


class RelayTest(unittest.TestCase):
    def setUp(self):
        for name, value in (("RELAY_STAGGER", 0.3), ("RELAY_ATTEMPT_TIMEOUT", 2.0), ("RELAY_CONNECT_TIMEOUT", 3.0)):
            original = getattr(config, name)
            setattr(config, name, value)
            self.addCleanup(setattr, config, name, original)
        self.echo = EchoServer()
        self.addCleanup(self.echo.close)
        self.pool = proxies.ProxyPool(check=lambda proxy: None)
        self.relay = relay.ProxyRelay(fakes.free_port(), self.pool)
        self.addCleanup(self.relay.close)

    def upstream(self, mode="ok", delay=0.0):
        item = FakeUpstream(mode, delay)
        self.addCleanup(item.close)
        return item

    def standby(self, *upstreams):
        for index, item in enumerate(upstreams):
            self.pool.put_back(approved(item, 200.0 + index))

    def connect(self):
        """Uma conexão do "Discord" pelo relay até o eco; devolve (socket, segundos)."""
        started = time.monotonic()
        sock = socks5.connect(("127.0.0.1", self.relay.port), "127.0.0.1", self.echo.port,
                              deadline=time.monotonic() + 10)
        return sock, time.monotonic() - started

    def assert_echo(self, sock):
        with sock:
            sock.settimeout(5)
            sock.sendall(b"ola discord")
            self.assertEqual(fakes_recv(sock, 11), b"ola discord")

    def test_goes_through_the_active_proxy(self):
        active, backup = self.upstream(), self.upstream()
        self.relay.set_active(approved(active))
        self.standby(backup)
        sock, _ = self.connect()
        self.assert_echo(sock)
        self.assertEqual((active.requests, backup.requests), (1, 0))
        self.assertFalse(self.relay.active_failing)

    def test_refused_connection_is_retried_on_the_standby_at_once(self):
        active, backup = self.upstream("refuse"), self.upstream()
        self.relay.set_active(approved(active))
        self.standby(backup)
        sock, took = self.connect()
        self.assert_echo(sock)
        self.assertLess(took, config.RELAY_STAGGER)        # a recusa não espera o prazo de corrida
        self.assertEqual((active.requests, backup.requests), (1, 1))

    def test_hung_proxy_races_the_next_after_the_stagger(self):
        active, backup = self.upstream("hang"), self.upstream()
        self.relay.set_active(approved(active))
        self.standby(backup)
        sock, took = self.connect()
        self.assert_echo(sock)
        self.assertGreaterEqual(took, config.RELAY_STAGGER - 0.05)
        self.assertLess(took, config.RELAY_ATTEMPT_TIMEOUT)

    def test_active_losing_repeatedly_is_flagged_for_the_session(self):
        active, backup = self.upstream("refuse"), self.upstream()
        self.relay.set_active(approved(active))
        self.standby(backup)
        for count in range(config.RELAY_MAX_STRIKES):
            self.assertFalse(self.relay.active_failing, count)
            self.assert_echo(self.connect()[0])
        self.assertTrue(self.relay.active_failing)
        # A sessão escolhe outro: o sinal some e o novo ativo começa do zero.
        self.relay.set_active(self.pool.pop_best())
        self.assertFalse(self.relay.active_failing)
        self.assert_echo(self.connect()[0])
        self.assertEqual(backup.requests, config.RELAY_MAX_STRIKES + 1)

    def test_a_win_resets_the_count(self):
        active, backup = self.upstream("refuse"), self.upstream()
        self.relay.set_active(approved(active))
        self.standby(backup)
        for _ in range(config.RELAY_MAX_STRIKES - 1):
            self.assert_echo(self.connect()[0])
        active.mode = "ok"
        self.assert_echo(self.connect()[0])                 # o ativo voltou a responder
        active.mode = "refuse"
        for _ in range(config.RELAY_MAX_STRIKES - 1):
            self.assert_echo(self.connect()[0])
        self.assertFalse(self.relay.active_failing)

    def test_standby_losing_repeatedly_leaves_the_queue(self):
        active, bad, good = self.upstream("refuse"), self.upstream("refuse"), self.upstream()
        self.relay.set_active(approved(active))
        self.standby(bad, good)
        for _ in range(config.RELAY_MAX_STRIKES):
            self.assert_echo(self.connect()[0])
        self.assertEqual([entry.proxy for entry in self.pool.peek(5)], [good.proxy])

    def test_when_every_proxy_fails_nobody_is_blamed(self):
        active, backup = self.upstream("unreachable"), self.upstream("unreachable")
        self.relay.set_active(approved(active))
        self.standby(backup)
        for _ in range(config.RELAY_MAX_STRIKES + 1):
            with self.assertRaises(socks5.ProxyError) as caught:
                self.connect()
            self.assertEqual(caught.exception.reply, 4)        # o Discord recebe "host inacessível"
        self.assertFalse(self.relay.active_failing)
        self.assertEqual(len(self.pool.peek(5)), 1)

    def test_demoted_active_is_still_the_last_resort(self):
        active = self.upstream()
        self.relay.set_active(approved(active))
        self.relay.demote_active()
        self.assertIsNone(self.relay.active)
        self.assert_echo(self.connect()[0])                 # sem reserva, ainda serve
        self.assertEqual(active.requests, 1)

    def test_without_any_proxy_the_connection_is_refused(self):
        with self.assertRaises(socks5.ProxyError):
            self.connect()

    def test_dead_proxy_address(self):
        active, backup = self.upstream(), self.upstream()
        dead = approved(active)
        active.close()                                       # porta fechada: conexão recusada
        self.relay.set_active(dead)
        self.standby(backup)
        self.assert_echo(self.connect()[0])
        self.assertEqual(backup.requests, 1)

    def test_many_parallel_connections(self):
        active, backup = self.upstream(delay=0.05), self.upstream()
        self.relay.set_active(approved(active))
        self.standby(backup)
        errors = []

        def one():
            try:
                self.assert_echo(self.connect()[0])
            except Exception as exc:          # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=one) for _ in range(25)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(active.requests, 25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
