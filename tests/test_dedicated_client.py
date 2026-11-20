"""O lado do app do servidor dedicado: configuração, erros, frames, país de saída e o plano do relay."""
from __future__ import annotations

import os
import socket
import struct
import threading
import time
import unittest

import fakes
from launcher import config, dconnect, proxies, relay, socks5
from test_relay import EchoServer, FakeUpstream, approved, fakes_recv

VPS = dconnect.DedicatedServer("wss://dconnect.example.test/t/abcdef012345", "t" * 43)


def vps_entry():
    return proxies.Approved(40.0, (VPS.host, VPS.port), 40.0, time.monotonic(), "FR", dedicated=True)


class ConfigTest(unittest.TestCase):
    def test_valid_and_invalid(self):
        server = dconnect.parse_config({"url": "wss://dconnect.styxx.cloud/t/abc123def456", "token": "a" * 43})
        self.assertEqual((server.host, server.port, server.path, server.secure),
                         ("dconnect.styxx.cloud", 443, "/t/abc123def456", True))
        self.assertNotIn("a" * 43, repr(server))                    # o token nunca vai para o log
        for data in ({"url": "https://x.com/t", "token": "a" * 43},
                     {"url": "ws://dconnect.styxx.cloud/t", "token": "a" * 43},     # sem TLS fora daqui
                     {"url": "wss://x.com/t", "token": "curto"},
                     {"url": "wss://x.com/t", "token": "a" * 42 + " "},
                     {"token": "a" * 43}, [], "x"):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    dconnect.parse_config(data)
        self.assertTrue(dconnect.parse_config({"url": "ws://127.0.0.1:8080/t/x", "token": "a" * 43}))

    def test_missing_or_broken_file(self):
        self.assertIsNone(dconnect.load(os.path.join(fakes.ROOT, "nao-existe.json")))


class ErrorMappingTest(unittest.TestCase):
    def test_statuses(self):
        cases = {
            (401, b'{"error":"token_invalido"}'): dconnect.TokenRejected,
            (410, b'{"error":"token_aposentado"}'): dconnect.TokenRejected,
            (404, b'{"ok":false,"error":"nao_encontrado"}'): dconnect.TokenRejected,   # caminho mudou
            (404, b"<html>cloudflared</html>"): dconnect.ServerUnavailable,           # túnel mal configurado
            (403, b'{"error":"destino_nao_permitido"}'): dconnect.DestinationRefused,
            (403, b"<html>Cloudflare WAF</html>"): dconnect.ServerUnavailable,
            (429, b'{"error":"ritmo"}'): dconnect.ServerBusy,
            (503, b'{"error":"lotado"}'): dconnect.ServerBusy,
            (502, b'{"error":"destino_inalcancavel"}'): dconnect.DestinationUnreachable,
            (502, b"<html>bad gateway</html>"): dconnect.ServerUnavailable,           # dconnect fora do ar
            (503, b'{"error":"discord_bloqueado"}'): dconnect.ServerUnavailable,
            (530, b"<html>tunnel down</html>"): dconnect.ServerUnavailable,           # Cloudflare sem conector
        }
        for (status, body), kind in cases.items():
            with self.subTest(status=status, body=body):
                error = dconnect._error_for(status, body)
                self.assertIs(type(error), kind)
                self.assertEqual(error.status, status)

    def test_body_framing(self):
        for headers, payload, expected in (
            ({"content-length": "5"}, b"abcdefgh", b"abcde"),
            ({"transfer-encoding": "chunked"}, b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n", b"abcde"),
            ({}, b"ate-fechar", b"ate-fechar"),
        ):
            with self.subTest(headers=headers):
                left, right = socket.socketpair()
                with left, right:
                    right.sendall(payload)
                    right.close()
                    self.assertEqual(dconnect._read_body(left, headers, b"", time.monotonic() + 5), expected)

    def test_server_that_is_not_there(self):
        server = dconnect.DedicatedServer(f"ws://127.0.0.1:{fakes.free_port()}/t/x", "a" * 43)
        with self.assertRaises(dconnect.ServerUnavailable):
            dconnect.open_tunnel(server, "discord.com", 443, deadline=time.monotonic() + 3)

    def test_wrong_websocket_answer(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)

        def serve():
            client, _ = listener.accept()
            with client:
                client.recv(4096)
                client.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                               b"Sec-WebSocket-Accept: errado\r\n\r\n")
                time.sleep(0.5)

        threading.Thread(target=serve, daemon=True).start()
        server = dconnect.DedicatedServer(f"ws://127.0.0.1:{listener.getsockname()[1]}/t/x", "a" * 43)
        with self.assertRaises(dconnect.ServerUnavailable):
            dconnect.open_tunnel(server, "discord.com", 443, deadline=time.monotonic() + 3)


def server_frame(opcode, payload=b"", fin=True):
    size = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    if size < 126:
        head += bytes([size])
    elif size < 65536:
        head += bytes([126]) + struct.pack(">H", size)
    else:
        head += bytes([127]) + struct.pack(">Q", size)
    return head + payload


class FramingTest(unittest.TestCase):
    def setUp(self):
        self.app_side, self.server_side = socket.socketpair()
        self.addCleanup(self.app_side.close)
        self.addCleanup(self.server_side.close)
        self.channel = dconnect.WebSocketChannel(self.app_side)
        self.server_side.settimeout(5)

    def read_client_frame(self):
        first, second = fakes_recv(self.server_side, 2)
        self.assertTrue(second & 0x80)                      # o app sempre mascara
        size = second & 0x7F
        if size == 126:
            size = struct.unpack(">H", fakes_recv(self.server_side, 2))[0]
        elif size == 127:
            size = struct.unpack(">Q", fakes_recv(self.server_side, 8))[0]
        mask = fakes_recv(self.server_side, 4)
        payload = fakes_recv(self.server_side, size) if size else b""
        return first & 0x0F, dconnect._xor(payload, mask)

    def test_data_ping_fragments_and_close(self):
        self.server_side.sendall(server_frame(0x9, b"oi") + server_frame(0x2, b"abc", fin=False)
                                 + server_frame(0x0, b"def") + server_frame(0x8, struct.pack(">H", 1000)))
        self.assertEqual(self.channel.recv(100), b"abc")
        self.assertEqual(self.read_client_frame(), (0xA, b"oi"))       # o ping foi respondido
        self.assertEqual(self.channel.recv(2), b"de")                   # o resto fica guardado
        self.assertEqual(self.channel.recv(100), b"f")
        self.assertEqual(self.channel.recv(100), b"")                   # close = fim
        self.assertEqual(self.read_client_frame()[0], 0x8)              # e devolve o close

    def test_large_writes_are_split_and_masked(self):
        data = os.urandom(200_000)
        threading.Thread(target=self.channel.sendall, args=(data,), daemon=True).start()
        received = b""
        while len(received) < len(data):
            opcode, payload = self.read_client_frame()
            self.assertEqual(opcode, 0x2)
            self.assertLessEqual(len(payload), 65536)
            received += payload
        self.assertEqual(received, data)

    def test_masked_server_frame_is_an_error(self):
        self.server_side.sendall(bytes([0x82, 0x83]) + b"\x00\x00\x00\x00abc")
        with self.assertRaises(ConnectionError):
            self.channel.recv(10)

    def test_shutdown_write_sends_close_once(self):
        self.channel.shutdown_write()
        self.channel.shutdown_write()
        self.assertEqual(self.read_client_frame()[0], 0x8)
        self.channel.close()
        self.server_side.settimeout(0.5)
        self.assertEqual(self.server_side.recv(10), b"")


class ExitCountryTest(unittest.TestCase):
    API_OK = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}"

    def patch_answers(self, *answers):
        calls = []

        def fake(proxy, host, request, deadline, until=None):
            calls.append(host)
            return answers[len(calls) - 1], 12.0

        original = proxies._https_get
        proxies._https_get = fake
        self.addCleanup(setattr, proxies, "_https_get", original)
        return calls

    @staticmethod
    def trace(status, loc):
        return f"HTTP/1.1 {status} OK\r\nContent-Type: text/plain\r\n\r\nfl=1\nh=gateway.discord.gg\nloc={loc}\ntls=TLSv1.3\n".encode()

    def test_country_comes_from_the_trace(self):
        calls = self.patch_answers(self.API_OK, self.trace(200, "US"))
        result = proxies.discord_check(("198.51.100.1", 1080))
        self.assertEqual(result.country, "US")
        self.assertEqual(calls, ["discord.com", proxies.TRACE_HOST])        # a API do Discord vem primeiro

    def test_exit_in_brazil_is_refused(self):
        self.patch_answers(self.API_OK, self.trace(200, "BR"))
        with self.assertRaises(socks5.ProxyError):
            proxies.discord_check(("198.51.100.1", 1080))

    def test_banned_by_discord_is_refused(self):
        calls = self.patch_answers(b"HTTP/1.1 429 Too Many Requests\r\n\r\n")
        with self.assertRaises(socks5.ProxyError):
            proxies.discord_check(("198.51.100.1", 1080))
        self.assertEqual(calls, ["discord.com"])

    def test_country_from_the_fallback_trace(self):
        calls = self.patch_answers(self.API_OK, b"HTTP/1.1 403 Forbidden\r\n\r\n", self.trace(200, "DE"))
        self.assertEqual(proxies.discord_check(("198.51.100.1", 1080)).country, "DE")
        self.assertEqual(calls, ["discord.com", proxies.TRACE_HOST, proxies.FALLBACK_TRACE_HOST])

    def test_unknown_country_is_refused(self):
        self.patch_answers(self.API_OK, b"HTTP/1.1 403 Forbidden\r\n\r\n", b"HTTP/1.1 403 Forbidden\r\n\r\n")
        with self.assertRaises(socks5.ProxyError):
            proxies.discord_check(("198.51.100.1", 1080))

    def test_own_server_in_brazil_is_fine(self):
        # O modo Meu servidor passa reject_countries vazio: o servidor do usuário pode estar no Brasil.
        calls = self.patch_answers(self.API_OK)
        self.assertEqual(proxies.discord_check(("198.51.100.1", 1080), reject_countries=frozenset()).country, "")
        self.assertEqual(calls, ["discord.com"])

    def test_a_cut_loc_line_is_not_read_as_a_country(self):
        partial = self.trace(200, "BR").split(b"loc=")[0] + b"loc=B"
        self.assertFalse(proxies._trace_done(partial))
        self.assertEqual(proxies.parse_trace(partial)[1], "")

    def test_parse_trace(self):
        self.assertEqual(proxies.parse_trace(self.trace(200, "fr")), (200, "FR"))
        self.assertEqual(proxies.parse_trace(b"lixo"), ("?", ""))


class RelayPlanTest(unittest.TestCase):
    """Que caminho cada conexão segue, em cada estado."""

    def setUp(self):
        self.pool = proxies.ProxyPool(check=lambda proxy: None)
        self.relay = relay.ProxyRelay(fakes.free_port(), self.pool, VPS)
        self.addCleanup(self.relay.close)
        self.free = proxies.Approved(200.0, ("198.51.100.9", 1080), 100.0, time.monotonic(), "US")
        self.pool.put_back(self.free)

    def kinds(self, host, port=443):
        return [candidate.kind for candidate in self.relay._plan(host, port)]

    def test_on_the_vps(self):
        self.relay.set_active(vps_entry())
        self.assertEqual(self.kinds("gateway.discord.gg"), [relay.VPS, relay.PROXY])
        self.assertEqual(self.kinds("www.youtube.com"), [relay.DIRECT])
        self.assertEqual(self.kinds("discord.com", 80), [relay.DIRECT])

    def test_while_recovering_from_the_vps(self):
        self.relay.set_active(vps_entry())
        self.relay.demote_active()
        self.assertEqual(self.kinds("gateway.discord.gg"), [relay.PROXY, relay.VPS])
        self.assertEqual(self.kinds("www.youtube.com"), [relay.DIRECT])

    def test_on_a_free_proxy(self):
        other = proxies.Approved(300.0, ("198.51.100.10", 1080), 150.0, time.monotonic(), "DE")
        self.relay.set_active(other)
        self.assertEqual(self.kinds("gateway.discord.gg"), [relay.PROXY, relay.PROXY, relay.VPS])
        self.assertEqual(self.kinds("www.youtube.com"), [relay.PROXY, relay.PROXY])

    def test_rejected_vps_is_never_used(self):
        self.relay.set_active(vps_entry())
        self.relay._vps_rejected = "token_aposentado"
        self.assertEqual(self.kinds("gateway.discord.gg"), [relay.PROXY])
        self.assertNotIn(relay.DIRECT, self.kinds("gateway.discord.gg"))     # Discord nunca sai direto


class RelayVpsTest(unittest.TestCase):
    """O relay com um servidor dedicado de mentira (open_tunnel trocado)."""

    def setUp(self):
        for name, value in (("RELAY_STAGGER", 0.3), ("RELAY_ATTEMPT_TIMEOUT", 2.0), ("RELAY_CONNECT_TIMEOUT", 3.0),
                            ("VPS_SUSPECT_SPAN", 0.2)):
            original = getattr(config, name)
            setattr(config, name, value)
            self.addCleanup(setattr, config, name, original)
        self.echo = EchoServer()
        self.addCleanup(self.echo.close)
        self.pool = proxies.ProxyPool(check=lambda proxy: None)
        self.relay = relay.ProxyRelay(fakes.free_port(), self.pool, VPS)
        self.addCleanup(self.relay.close)
        self.behavior = "ok"
        self.calls = []

        def fake_open_tunnel(server, host, port, *, deadline):
            self.calls.append((host, port))
            if self.behavior == "ok":
                sock = socket.create_connection(("127.0.0.1", self.echo.port), timeout=5)
                sock.settimeout(None)
                return relay._SocketChannel(sock)
            raise self.behavior

        original = relay.dconnect.open_tunnel
        relay.dconnect.open_tunnel = fake_open_tunnel
        self.addCleanup(setattr, relay.dconnect, "open_tunnel", original)
        self.entry = vps_entry()
        self.relay.set_active(self.entry)

    def connect(self, host="gateway.discord.gg", port=443):
        return socks5.connect(("127.0.0.1", self.relay.port), host, port, deadline=time.monotonic() + 10)

    def assert_echo(self, sock):
        with sock:
            sock.settimeout(5)
            sock.sendall(b"ping")
            self.assertEqual(fakes_recv(sock, 4), b"ping")

    def test_discord_goes_through_the_vps(self):
        self.assert_echo(self.connect())
        self.assertEqual(self.calls, [("gateway.discord.gg", 443)])

    def test_other_hosts_go_direct(self):
        self.assert_echo(self.connect("localhost", self.echo.port))
        self.assertEqual(self.calls, [])

    def test_vps_failure_uses_a_free_standby(self):
        upstream = FakeUpstream(redirect=("127.0.0.1", self.echo.port))
        self.addCleanup(upstream.close)
        self.pool.put_back(approved(upstream, 200.0))
        self.behavior = dconnect.ServerUnavailable("fora do ar")
        self.assert_echo(self.connect())                    # gateway.discord.gg, pela reserva
        self.assertEqual(upstream.requests, 1)

    def test_repeated_hard_failures_raise_suspicion(self):
        upstream = FakeUpstream("unreachable")
        self.addCleanup(upstream.close)
        self.pool.put_back(approved(upstream, 200.0))
        self.behavior = dconnect.ServerUnavailable("fora do ar")
        for _ in range(config.VPS_SUSPECT_FAILURES):
            with self.assertRaises(socks5.ProxyError):
                self.connect()          # a reserva também falha; as falhas do servidor contam
            time.sleep(0.15)
        self.assertTrue(self.relay.take_events().vps_suspect)
        self.assertFalse(self.relay.take_events().vps_suspect)           # lido uma vez só

    def test_answered_refusals_are_not_failures(self):
        for error in (dconnect.ServerBusy("limite", 429), dconnect.DestinationUnreachable("x", 502)):
            self.behavior = error
            for _ in range(config.VPS_SUSPECT_FAILURES + 1):
                with self.assertRaises(socks5.ProxyError):
                    self.connect()
                time.sleep(0.1)
        self.assertFalse(self.relay.take_events().vps_suspect)

    def test_refusing_everything_for_a_while_is_reported(self):
        original = (config.VPS_REFUSING_COUNT, config.VPS_REFUSING_SPAN)
        config.VPS_REFUSING_COUNT, config.VPS_REFUSING_SPAN = 3, 0.2
        self.addCleanup(setattr, config, "VPS_REFUSING_COUNT", original[0])
        self.addCleanup(setattr, config, "VPS_REFUSING_SPAN", original[1])
        self.behavior = dconnect.ServerBusy("cota", 429, "cota_diaria")
        for _ in range(3):
            with self.assertRaises(socks5.ProxyError):
                self.connect()
            time.sleep(0.12)
        events = self.relay.take_events()
        self.assertTrue(events.vps_refusing)
        self.assertFalse(events.vps_suspect)

    def test_rejected_token_switches_the_vps_off(self):
        self.behavior = dconnect.TokenRejected("aposentado", 410, "token_aposentado")
        with self.assertRaises(socks5.ProxyError):
            self.connect()
        self.assertEqual(self.relay.take_events().vps_rejected, "token_aposentado")
        calls = len(self.calls)
        with self.assertRaises(socks5.ProxyError):
            self.connect()
        self.assertEqual(len(self.calls), calls)            # nem tentou de novo

    def test_refused_discord_host_is_reported_not_sent_direct(self):
        self.behavior = dconnect.DestinationRefused("fora da lista", 403)
        with self.assertRaises(socks5.ProxyError) as caught:
            self.connect()
        self.assertEqual(caught.exception.reply, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
