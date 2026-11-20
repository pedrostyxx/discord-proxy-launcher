"""O servidor dedicado (server/dconnect_server.py) rodando aqui, com o cliente do app.

O "Discord" de destino é um servidor TLS local com certificado próprio para
echo.discord.com; o resolvedor do servidor é trocado para apontar esse nome para
127.0.0.1. Nada sai para a internet.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import os
import secrets
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
import unittest

import fakes  # noqa: F401  (ajusta o sys.path)
from launcher import dconnect, destinations

sys.path.insert(0, os.path.join(fakes.ROOT, "server"))
import dconnect_server  # noqa: E402

TARGET = "echo.discord.com"


def make_certificate(directory: str, name: str) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
    with open(cert_path, "wb") as handle:
        handle.write(certificate.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return cert_path, key_path


class TlsEcho:
    """O "host do Discord": TLS com certificado de echo.discord.com, devolve o que recebe."""

    def __init__(self, cert_path: str, key_path: str):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert_path, key_path)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.received = 0               # conexões que chegaram a mandar algum byte
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        try:
            client.settimeout(10)
            first = client.recv(1, socket.MSG_PEEK)
            if first:
                self.received += 1
            with self.context.wrap_socket(client, server_side=True) as tls:
                while True:
                    data = tls.recv(65536)
                    if not data:
                        return
                    tls.sendall(data)
        except (OSError, ssl.SSLError):
            pass

    def close(self):
        self.listener.close()


class ServerCase(unittest.TestCase):
    limits: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dpl-dconnect-")
        cls.cert_path, cls.key_path = make_certificate(cls.tmp, TARGET)

    def setUp(self):
        self.echo = TlsEcho(self.cert_path, self.key_path)
        self.addCleanup(self.echo.close)
        self.token = secrets.token_urlsafe(32)
        self.retired = secrets.token_urlsafe(32)
        self.path = "/t/" + secrets.token_hex(8)
        original_ports = dconnect_server.VPS_PORTS
        dconnect_server.VPS_PORTS = frozenset({443, self.echo.port})
        self.addCleanup(setattr, dconnect_server, "VPS_PORTS", original_ports)
        config = dconnect_server.Config({
            "listen_port": fakes.free_port(), "path": self.path, "canary": False,
            "tokens": [{"label": "teste", "token": self.token}],
            "retired_tokens": [{"label": "velho", "sha256": hashlib.sha256(self.retired.encode()).hexdigest()}],
            "limits": dict(self.limits),
        })
        self.resolved = {TARGET: ["127.0.0.1"]}

        async def resolver(host, port):
            return self.resolved.get(host, [])

        self.server = dconnect_server.Server(config, resolver=resolver, allow_private_for_tests=True)
        self.server.discord_state = "ok"
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.server.start())
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(10))
        self.addCleanup(self._stop)
        self.dedicated = dconnect.DedicatedServer(f"ws://127.0.0.1:{config.listen_port}{self.path}", self.token)

    def _stop(self):
        future = asyncio.run_coroutine_threadsafe(self.server.stop(drain=0.5), self.loop)
        try:
            future.result(10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)

    def tunnel(self, host=TARGET, port=None, server=None):
        return dconnect.open_tunnel(server or self.dedicated, host, port or self.echo.port,
                                    deadline=time.monotonic() + 10)

    def tls_through(self, channel, name=TARGET):
        tls = dconnect._TlsOverChannel(channel, name, time.monotonic() + 10)
        tls._tls = self.client_context().wrap_bio(tls._incoming, tls._outgoing, server_hostname=name)
        return tls

    def client_context(self):
        context = ssl.create_default_context(cafile=self.cert_path)
        return context

    def raw_request(self, head: bytes) -> bytes:
        sock = socket.create_connection(("127.0.0.1", self.server.config.listen_port), timeout=10)
        try:
            sock.sendall(head)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    return data
                data += chunk
        finally:
            sock.close()

    def raw_tunnel(self, host=TARGET, port=None, extra: str = "") -> tuple[bytes, socket.socket]:
        """Pede um túnel "na mão" (com cabeçalhos extras); devolve a linha de status e o socket aberto."""
        sock = socket.create_connection(("127.0.0.1", self.server.config.listen_port), timeout=10)
        self.addCleanup(sock.close)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {self.path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"Authorization: Bearer {self.token}\r\nX-DPL-Target: {host}:{port or self.echo.port}\r\n"
                      f"{extra}\r\n").encode())
        head = b""
        while b"\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        return head.split(b"\r\n", 1)[0], sock

    def wait_closed(self, channel, seconds=8.0) -> float:
        """Lê até o servidor fechar o túnel; devolve quanto demorou."""
        channel.settimeout(seconds + 2)
        started = time.monotonic()
        while channel.recv(65536):          # tickets de sessão do TLS 1.3 ou o eco ainda chegam
            pass
        return time.monotonic() - started


class AuthAndRoutesTest(ServerCase):
    def test_health(self):
        state = dconnect.health(self.dedicated)
        self.assertEqual(state.discord, "ok")

    def test_wrong_token_is_401_and_retired_is_410(self):
        with self.assertRaises(dconnect.TokenRejected) as caught:
            dconnect.health(dconnect.DedicatedServer(self.dedicated.url, "x" * 43))
        self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(dconnect.TokenRejected) as caught:
            self.tunnel(server=dconnect.DedicatedServer(self.dedicated.url, self.retired))
        self.assertEqual(caught.exception.status, 410)

    def test_anything_else_is_404(self):
        answer = self.raw_request(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(answer.startswith(b"HTTP/1.1 404"), answer[:40])
        answer = self.raw_request(f"GET {self.path}/x HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertTrue(answer.startswith(b"HTTP/1.1 404"), answer[:40])

    def test_huge_or_broken_requests(self):
        answer = self.raw_request(b"GET / HTTP/1.1\r\nX: " + b"a" * 70000 + b"\r\n\r\n")
        self.assertTrue(answer.startswith(b"HTTP/1.1 431") or answer == b"", answer[:40])
        answer = self.raw_request(b"\x16\x03\x01garbage\r\n\r\n")
        self.assertTrue(answer.startswith(b"HTTP/1.1 400"), answer[:40])

    def test_blocked_discord_makes_health_fail(self):
        self.server.discord_state = "bloqueado"
        with self.assertRaises(dconnect.ServerUnavailable) as caught:
            dconnect.health(self.dedicated)
        self.assertEqual(caught.exception.status, 503)


class TunnelTest(ServerCase):
    def test_tls_through_the_tunnel(self):
        channel = self.tunnel()
        try:
            tls = self.tls_through(channel)
            tls.handshake()
            tls.sendall(b"ola discord")
            self.assertEqual(tls.recv(100), b"ola discord")
        finally:
            channel.close()
        self.assertEqual(self.server.stats["tuneis"], 1)

    def test_sni_must_be_the_requested_host(self):
        self.resolved["other.discord.com"] = ["127.0.0.1"]
        channel = self.tunnel()                 # pede echo.discord.com...
        try:
            tls = dconnect._TlsOverChannel(channel, "other.discord.com", time.monotonic() + 10)
            with self.assertRaises((OSError, ssl.SSLError)):
                tls.handshake()                 # ...mas manda um ClientHello de outro nome
        finally:
            channel.close()
        time.sleep(0.2)
        self.assertEqual(self.echo.received, 0)          # nenhum byte chegou ao destino
        self.assertEqual(self.server.stats["sni_recusado"], 1)

    def test_not_tls_is_dropped(self):
        channel = self.tunnel()
        try:
            channel.sendall(b"GET / HTTP/1.1\r\nHost: evil.example\r\n\r\n")
            channel.settimeout(5)
            self.assertEqual(channel.recv(100), b"")
        finally:
            channel.close()
        time.sleep(0.2)
        self.assertEqual(self.echo.received, 0)

    def test_client_hello_split_across_frames(self):
        channel = self.tunnel()
        try:
            incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
            tls = self.client_context().wrap_bio(incoming, outgoing, server_hostname=TARGET)
            with self.assertRaises(ssl.SSLWantReadError):
                tls.do_handshake()
            hello = outgoing.read()
            step = len(hello) // 6 + 1                     # 6 pedaços (o limite é 16 frames)
            for start in range(0, len(hello), step):
                channel.sendall(hello[start:start + step])
            channel.settimeout(5)
            while True:
                try:
                    tls.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    pending = outgoing.read()
                    if pending:
                        channel.sendall(pending)
                    data = channel.recv(65536)
                    self.assertTrue(data)
                    incoming.write(data)
            pending = outgoing.read()
            if pending:
                channel.sendall(pending)
        finally:
            channel.close()

    def test_destinations_outside_the_list(self):
        for host, port in (("example.com", self.echo.port), ("evildiscord.com", 443),
                           ("discord.com.evil.com", 443), ("discord.com", 80),
                           ("x.com\x00.discord.com", 443), ("127.0.0.1", 443), ("support-dev.discord.com", 443),
                           ("abc.discordsays.com", 443)):
            with self.subTest(host=host, port=port):
                with self.assertRaises(dconnect.DestinationRefused):
                    self.tunnel(host, port)

    def test_private_addresses_are_never_destinations(self):
        self.server._allow_private = False
        self.server._dns_cache.clear()
        for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "100.100.100.200", "::1",
                        "64:ff9b::7f00:1", "::ffff:127.0.0.1", "fd00::1", "192.168.1.1"):
            with self.subTest(address=address):
                self.resolved[TARGET] = [address]
                self.server._dns_cache.clear()
                with self.assertRaises(dconnect.DestinationUnreachable):
                    self.tunnel()

    def test_idle_tunnels_are_closed(self):
        self.server.limits["idle_timeout"] = 1.0
        channel = self.tunnel()
        try:
            tls = self.tls_through(channel)
            tls.handshake()
            channel.settimeout(10)
            started = time.monotonic()
            while channel.recv(65536):          # os tickets de sessão do TLS 1.3 ainda chegam
                pass
            self.assertLess(time.monotonic() - started, 8)
        finally:
            channel.close()

    def test_unmasked_client_frame_closes_the_tunnel(self):
        channel = self.tunnel()
        try:
            channel._sock.sendall(dconnect_server.ws_frame(dconnect_server.OP_BINARY, b"\x16\x03\x01"))
            channel.settimeout(5)
            self.assertEqual(channel.recv(10), b"")
        finally:
            channel.close()

    def test_ping_is_answered(self):
        channel = self.tunnel()
        try:
            channel._send(0x9, b"oi")
            channel.settimeout(5)
            first, second = channel._sock.recv(2)
            self.assertEqual(first & 0x0F, 0xA)
        finally:
            channel.close()

    def client_hello_bytes(self, name=TARGET) -> bytes:
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = self.client_context().wrap_bio(incoming, outgoing, server_hostname=name)
        with self.assertRaises(ssl.SSLWantReadError):
            tls.do_handshake()
        return outgoing.read()

    def test_client_hello_in_many_tiny_frames_is_dropped(self):
        # Milhares de pedacinhos prenderiam o loop do servidor: passa de 16 frames, fecha.
        channel = self.tunnel()
        try:
            for byte in self.client_hello_bytes()[:40]:
                channel._send(0x2, bytes([byte]))
            self.assertLess(self.wait_closed(channel), 8)
        except OSError:
            pass                                     # o servidor já fechou no meio do envio
        finally:
            channel.close()
        time.sleep(0.2)
        self.assertEqual(self.echo.received, 0)
        self.assertEqual(self.server.stats["hello_invalido"], 1)

    def test_empty_frame_during_the_client_hello_is_dropped(self):
        channel = self.tunnel()
        try:
            channel._send(0x2, b"")
            self.assertLess(self.wait_closed(channel), 8)
        finally:
            channel.close()
        self.assertEqual(self.echo.received, 0)
        self.assertEqual(self.server.stats["hello_invalido"], 1)

    def test_frames_over_64k_close_the_tunnel(self):
        channel = self.tunnel()
        try:
            tls = self.tls_through(channel)
            tls.handshake()
            header = bytes([0x82, 0x80 | 127]) + struct.pack(">Q", 65537) + os.urandom(4)
            channel._sock.sendall(header)
            self.assertLess(self.wait_closed(channel), 8)
        finally:
            channel.close()

    def test_names_pointing_outside_the_list_are_refused(self):
        # O nome é do Discord, mas o CNAME leva a uma plataforma de terceiros.
        self.resolved["help.discord.com"] = ("discord.zendesk.com", ["127.0.0.1"])
        with self.assertRaises(dconnect.DestinationRefused):
            self.tunnel("help.discord.com")
        self.assertEqual(self.server.stats["cname_fora_da_lista"], 1)
        with self.assertRaises(dconnect.DestinationRefused):          # a recusa fica no cache
            self.tunnel("help.discord.com")
        self.assertEqual(self.server.stats["cname_fora_da_lista"], 1)
        # Um CNAME para outro nome da lista, ou para o próprio nome, continua valendo.
        self.resolved[TARGET] = ("edge.discordapp.net.", ["127.0.0.1"])
        self.server._dns_cache.clear()
        self.tunnel().close()
        self.resolved[TARGET] = (TARGET + ".", ["127.0.0.1"])
        self.server._dns_cache.clear()
        self.tunnel().close()

    def test_sources_exist_only_after_the_token(self):
        self.raw_request(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        self.raw_request(f"GET {self.path} HTTP/1.1\r\nAuthorization: Bearer {'y' * 43}\r\n\r\n".encode())
        with self.assertRaises(dconnect.TokenRejected):
            dconnect.health(dconnect.DedicatedServer(self.dedicated.url, "x" * 43))
        self.assertEqual(len(self.server.sources), 0)
        self.assertEqual(len(self.server.bad_tokens), 1)
        self.tunnel().close()
        self.assertEqual(list(self.server.sources), ["127.0.0.1"])


class FrameDeadlineTest(ServerCase):
    limits = {"frame_timeout": 1.0}

    def test_unfinished_frame_closes_the_tunnel(self):
        channel = self.tunnel()
        try:
            tls = self.tls_through(channel)
            tls.handshake()
            # Anuncia 1000 bytes e manda só 10: a memória do frame ficaria presa.
            channel._sock.sendall(bytes([0x82, 0x80 | 126]) + struct.pack(">H", 1000) + os.urandom(4) + b"x" * 10)
            self.assertLess(self.wait_closed(channel), 6)
        finally:
            channel.close()
        self.assertEqual(self.server.stats["frame_incompleto"], 1)


class GatewayLimitTest(ServerCase):
    limits = {"max_gateways_per_source": 1, "new_tunnels_burst": 50}

    def test_every_discord_gg_name_counts_as_gateway(self):
        for name in ("discord.gg", "gateway.discord.gg"):
            self.resolved[name] = ["127.0.0.1"]
        status, _ = self.raw_tunnel("discord.gg")
        self.assertIn(b" 101 ", status)
        status, _ = self.raw_tunnel("gateway.discord.gg")
        self.assertIn(b" 429 ", status)
        status, _ = self.raw_tunnel(TARGET)                    # o resto não conta
        self.assertIn(b" 101 ", status)


class Ipv6SourcesTest(ServerCase):
    limits = {"max_tunnels_per_source": 1, "ipv6_48_scale": 2, "new_tunnels_burst": 50}

    def test_ipv6_is_limited_by_64_and_by_48(self):
        def status_for(address):
            status, _ = self.raw_tunnel(extra=f"CF-Connecting-IP: {address}\r\n")
            return status.split(b" ")[1]

        self.assertEqual(status_for("2001:db8:0:100::1"), b"101")
        self.assertEqual(status_for("2001:db8:0:100:aaaa::2"), b"429")  # mesmo /64
        self.assertEqual(status_for("2001:db8:0:200::1"), b"101")       # outro /64, mesmo /48
        self.assertEqual(status_for("2001:db8:0:300::1"), b"429")       # o /48 inteiro chegou em 2
        self.assertEqual(status_for("2001:db8:1::1"), b"101")           # outro /48
        self.assertEqual(status_for("::ffff:203.0.113.9"), b"101")      # IPv4 dentro de IPv6
        self.assertEqual(status_for("203.0.113.9"), b"429")             # é a mesma origem


class QuotaTest(ServerCase):
    limits = {"bytes_per_day_per_source": 40000, "new_tunnels_burst": 50}

    def test_daily_quota_ends_open_tunnels(self):
        channel = self.tunnel()
        try:
            tls = self.tls_through(channel)
            tls.handshake()
            for _ in range(20):
                tls.sendall(b"x" * 4000)
                tls.recv(65536)
        except OSError:
            pass                                    # o servidor fechou: a cota acabou
        finally:
            channel.close()
        self.assertGreaterEqual(self.server.stats["cota_diaria_atingida"], 1)
        with self.assertRaises(dconnect.ServerBusy) as caught:
            self.tunnel()
        self.assertEqual(caught.exception.code, "cota_diaria")


class ByteRateTest(ServerCase):
    limits = {"bytes_per_second_per_source": 2000, "bytes_burst_per_source": 60000,
              "max_throttle_wait": 0.5, "new_tunnels_burst": 50}

    def test_a_source_cannot_go_past_its_byte_rate(self):
        channel = self.tunnel()
        try:
            tls = self.tls_through(channel)
            tls.handshake()
            tls.sendall(b"x" * 120000)
            self.assertLess(self.wait_closed(channel), 8)
        except OSError:
            pass
        finally:
            channel.close()
        self.assertGreaterEqual(self.server.stats["ritmo_excedido"], 1)


class LimitsTest(ServerCase):
    limits = {"max_tunnels_per_source": 2, "new_tunnels_burst": 50}

    def test_concurrent_limit_per_source(self):
        first, second = self.tunnel(), self.tunnel()
        try:
            with self.assertRaises(dconnect.ServerBusy):
                self.tunnel()
        finally:
            first.close()
            second.close()
        time.sleep(0.3)
        self.tunnel().close()                   # liberou: volta a aceitar


class RateTest(ServerCase):
    limits = {"new_tunnels_burst": 2, "new_tunnels_per_second": 0.01}

    def test_new_tunnel_rate(self):
        self.tunnel().close()
        self.tunnel().close()
        with self.assertRaises(dconnect.ServerBusy):
            self.tunnel()


class ParsingTest(unittest.TestCase):
    def test_client_and_server_lists_are_the_same(self):
        self.assertEqual(dconnect_server.VPS_SUFFIXES, destinations.VPS_SUFFIXES)
        self.assertEqual(dconnect_server.VPS_EXCLUDED, destinations.VPS_EXCLUDED)
        self.assertEqual(dconnect_server.VPS_PORTS, destinations.VPS_PORTS)

    def test_hostname_vectors(self):
        cases = {
            "discord.com": "discord.com", "DISCORD.COM.": "discord.com", "cdn.discordapp.com": "cdn.discordapp.com",
            "x.com\x00.discord.com": None, "a" * 64 + ".discord.com": None, "x..discord.com": None,
            "-x.discord.com": None, "x_y.discord.com": None, "discord .com": None, "127.0.0.1": None,
            "%2e.discord.com": None, "dísc.com": None, "": None, "com": None, "a." * 130 + "com": None,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(destinations.normalize_host(raw), expected)
                self.assertEqual(dconnect_server.normalize_host(raw), expected)

    def test_allowlist_vectors(self):
        cases = {
            ("discord.com", 443): True, ("gateway.discord.gg", 443): True, ("x.discord.media", 443): True,
            ("api.hcaptcha.com", 443): True, ("evildiscord.com", 443): False, ("discord.com.evil.com", 443): False,
            ("discord.com", 80): False, ("support-dev.discord.com", 443): False, ("a.discordsays.com", 443): False,
            ("discord-attachments-uploads-prd.storage.googleapis.com", 443): False, ("www.youtube.com", 443): False,
            ("creator-support.discord.com", 443): False, ("support-apps.discord.com", 443): False,
            ("status.hcaptcha.com", 443): False, ("support.discord.com", 443): True,
        }
        for (host, port), expected in cases.items():
            with self.subTest(host=host, port=port):
                self.assertEqual(destinations.goes_through_vps(host, port), expected)
                self.assertEqual(dconnect_server.goes_through_vps(host, port), expected)

    def test_ip_denylist(self):
        for address in ("10.1.2.3", "127.0.0.1", "169.254.169.254", "100.100.100.200", "::1", "64:ff9b::7f00:1",
                        "::127.0.0.1", "::ffff:8.8.8.8", "fe80::1", "fc00::1", "2001:db8::1", "0.0.0.0", "224.0.0.1"):
            with self.subTest(address=address):
                self.assertFalse(dconnect_server.ip_allowed(address))
        for address in ("162.159.135.232", "2606:4700::6810:1"):
            self.assertTrue(dconnect_server.ip_allowed(address))

    def client_hello(self, name: str | None) -> bytes:
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        tls = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname=name)
        with self.assertRaises(ssl.SSLWantReadError):
            tls.do_handshake()
        return outgoing.read()

    def test_client_hello_parser(self):
        hello = self.client_hello("gateway.discord.gg")
        self.assertEqual(dconnect_server.client_hello_sni(hello), ("ok", b"gateway.discord.gg"))
        self.assertEqual(dconnect_server.client_hello_sni(hello[:20])[0], "need")
        self.assertEqual(dconnect_server.client_hello_sni(b"GET / HTTP/1.1\r\n\r\n")[0], "bad")
        self.assertEqual(dconnect_server.client_hello_sni(self.client_hello(None))[0], "bad")    # sem SNI
        # O mesmo ClientHello partido em dois registros TLS continua válido.
        body = hello[5:]
        split = hello[:1] + hello[1:3] + struct.pack(">H", 50) + body[:50] + \
            hello[:1] + hello[1:3] + struct.pack(">H", len(body) - 50) + body[50:]
        self.assertEqual(dconnect_server.client_hello_sni(split), ("ok", b"gateway.discord.gg"))
        # Lixo dentro do tamanho declarado não derruba o parser.
        for size in range(6, min(len(hello), 300)):
            verdict, _ = dconnect_server.client_hello_sni(hello[:5] + os.urandom(size))
            self.assertIn(verdict, ("need", "bad"))

    def test_client_hello_in_more_than_four_records_is_refused(self):
        hello = self.client_hello("gateway.discord.gg")
        body = hello[5:]

        def in_records(count):
            size = len(body) // count + 1
            return b"".join(hello[:3] + struct.pack(">H", len(body[start:start + size])) + body[start:start + size]
                            for start in range(0, len(body), size))

        self.assertEqual(dconnect_server.client_hello_sni(in_records(4)), ("ok", b"gateway.discord.gg"))
        self.assertEqual(dconnect_server.client_hello_sni(in_records(5))[0], "bad")
        # O ataque da revisão: milhares de registros de 1 byte, sem nunca completar.
        tiny = hello[:3] + b"\x00\x01\x01" + b"".join(hello[:3] + b"\x00\x01" + bytes([b]) for b in b"\x00\x40\x00" * 900)
        self.assertEqual(dconnect_server.client_hello_sni(tiny)[0], "bad")

    def test_gateway_names(self):
        for name, expected in (("gateway.discord.gg", True), ("discord.gg", True), ("gateway-us-east1-b.discord.gg", True),
                               ("remote-auth-gateway.discord.gg", True), ("discord.com", False),
                               ("gateway.discord.com", False), ("cdn.discordapp.com", False)):
            with self.subTest(name=name):
                self.assertEqual(dconnect_server.is_gateway(name), expected)

    def test_source_keys(self):
        self.assertEqual(dconnect_server.source_keys("203.0.113.9"), [("203.0.113.9", 1)])
        self.assertEqual(dconnect_server.source_keys("::ffff:203.0.113.9"), [("203.0.113.9", 1)])
        self.assertEqual(dconnect_server.source_keys("2001:db8:aa:bbcc:1:2:3:4", 4),
                         [("2001:db8:aa:bbcc::/64", 1), ("2001:db8:aa::/48", 4)])
        self.assertEqual(dconnect_server.source_keys("fe80::1%eth0")[0], ("fe80::/64", 1))
        self.assertEqual(dconnect_server.source_keys("lixo"), [("?", 1)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
