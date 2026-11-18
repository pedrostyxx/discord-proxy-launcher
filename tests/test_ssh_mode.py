"""Modo A de ponta a ponta contra um servidor SSH local (127.0.0.1).

Usa chaves e known_hosts temporários, uma porta SOCKS aleatória e o Discord
falso: não lê as chaves do usuário, não usa a porta 1080 e não toca no Discord.
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest

import paramiko

import fakes
from launcher import config, discord, proxies, session, socks5, tunnel, win

HOST = "127.0.0.1"


class NoAgent:
    """Nos testes o agente SSH do usuário fica de fora."""

    def get_keys(self):
        return ()

    def close(self):
        pass


class Recorder:
    """Guarda os status que a sessão publica e deixa esperar por um deles."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self.finished = threading.Event()
        self._changed = threading.Condition()

    def on_status(self, color, text):
        with self._changed:
            self.events.append((color, text))
            self._changed.notify_all()

    def on_finish(self):
        self.finished.set()

    def wait_for(self, color, fragment="", timeout=20.0, start=0) -> int:
        """Espera um status (a partir do índice `start`); devolve o índice seguinte a ele."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                for index in range(start, len(self.events)):
                    if self.events[index][0] == color and fragment in self.events[index][1]:
                        return index + 1
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(
                        f"status [{color}] '{fragment}' não apareceu; recebidos desde {start}: {self.events[start:]}"
                    )
                self._changed.wait(left)

    def colors(self, start=0):
        return [color for color, _ in self.events[start:]]

    def texts(self):
        return " | ".join(text for _, text in self.events)


class SshCase(unittest.TestCase):
    """Chaves de teste, destino HTTP local e os ajustes comuns aos testes de SSH."""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="dpl-ssh-")
        cls.key_path = fakes.generate_client_key(os.path.join(cls.work, "good"))
        cls.key = paramiko.PKey.from_path(cls.key_path)
        cls.other_key_path = fakes.generate_client_key(os.path.join(cls.work, "other"))
        cls.locked_dir = os.path.join(cls.work, "locked")
        fakes.generate_client_key(cls.locked_dir, passphrase="frase-secreta")
        cls.empty_dir = os.path.join(cls.work, "empty")
        os.makedirs(cls.empty_dir)
        cls.ssh_exe = tunnel.find_ssh_exe()

    def setUp(self):
        self.patch(tunnel.paramiko, "Agent", NoAgent)
        tunnel._seen_host_keys.clear()             # cada teste começa sem servidores "já vistos"
        self.addCleanup(tunnel._seen_host_keys.clear)
        self.target = fakes.HttpTarget()
        self.addCleanup(self.target.close)
        self.patch(config, "HEALTH_TARGET", self.target.address)
        # A segunda linha da verificação de saúde pergunta ao Discord de verdade;
        # nos testes quem responde por ele é este interruptor.
        self.discord_reachable = False
        self.patch(proxies, "discord_check", self._discord_check)
        self.local_port = fakes.free_port()
        self._servers = []
        self._tunnels = []
        self.addCleanup(self._stop_everything)

    def _discord_check(self, proxy, timeout=None):
        if not self.discord_reachable:
            raise TimeoutError("Discord fora de alcance (teste)")
        return proxies.CheckResult(connect_ms=5.0, total_ms=10.0)

    def patch(self, owner, name, value):
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def _stop_everything(self):
        for item in self._tunnels:
            item.stop()
        for server in self._servers:
            server.stop()

    def server(self, **options) -> fakes.SshTestServer:
        options.setdefault("authorized_keys", [self.key])
        server = fakes.SshTestServer(**options).start()
        self._servers.append(server)
        return server

    def track(self, item):
        self._tunnels.append(item)
        return item

    def paramiko_tunnel(self, server, password=None, ssh_dir=None, host=HOST):
        return self.track(tunnel.ParamikoTunnel(
            host, server.port, server.user, password, self.local_port,
            ssh_dir=ssh_dir or os.path.dirname(self.key_path),
        ))

    def ssh_options(self, key_path=None, known_hosts=None):
        return (
            "-i", key_path or self.key_path,
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", f"UserKnownHostsFile={known_hosts or os.path.join(self.work, 'known_hosts_' + str(self.local_port))}",
        )

    def ssh_exe_tunnel(self, server, local_port=None, **options):
        if not self.ssh_exe:
            self.skipTest("ssh.exe não disponível")
        return self.track(tunnel.SshExeTunnel(
            self.ssh_exe, HOST, server.port, server.user, local_port or self.local_port,
            extra_options=self.ssh_options(**options),
        ))

    def probe(self, port=None):
        return socks5.probe((HOST, port or self.local_port), self.target.address, 5)

    def assert_logins(self, server, expected, at_least=False):
        """O servidor de teste conta os logins em outra thread; dá um instante para ele registrar."""
        fakes.wait_until(lambda: server.logins >= expected, 3)
        if at_least:
            self.assertGreaterEqual(server.logins, expected)
        else:
            self.assertEqual(server.logins, expected)

    def assert_port_free(self, port=None):
        self.assertTrue(fakes.wait_until(lambda: tunnel._can_bind(port or self.local_port), 5))

    def known_hosts_dir(self, lines) -> str:
        """Pasta .ssh com a chave do cliente e um known_hosts com as linhas dadas."""
        folder = tempfile.mkdtemp(prefix="kh-", dir=self.work)
        with open(self.key_path, "rb") as source, open(os.path.join(folder, "id_ed25519"), "wb") as copy:
            copy.write(source.read())
        with open(os.path.join(folder, "known_hosts"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return folder

    @staticmethod
    def known_line(name, key) -> str:
        return f"{name} {key.get_name()} {key.get_base64()}"


class ParamikoTunnelTest(SshCase):
    def test_password_login_forwards_traffic(self):
        server = self.server()
        item = self.paramiko_tunnel(server, password=server.password, ssh_dir=self.empty_dir)
        item.start(threading.Event())
        self.assertGreater(self.probe(), 0)
        self.assertEqual(server.forwarded, [self.target.address])
        self.assertTrue(item.alive())
        self.assertFalse(item.forwarding_refused())
        self.assertEqual(item.label, f"127.0.0.1:{server.port}")
        item.stop()
        self.assertFalse(item.alive())
        self.assert_port_free()

    def test_restart_after_stop(self):
        server = self.server()
        item = self.paramiko_tunnel(server, password=server.password)
        for _ in range(3):
            item.start(threading.Event())
            self.assertGreater(self.probe(), 0)
            item.stop()
        self.assert_logins(server, 3)

    def test_wrong_password(self):
        server = self.server()
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.paramiko_tunnel(server, password="errada").start(threading.Event())
        self.assertEqual(caught.exception.kind, "auth")
        self.assertEqual(str(caught.exception), "usuário ou senha SSH incorretos")
        self.assert_port_free()

    def test_server_without_password_login(self):
        server = self.server(allowed_auths=("publickey",))
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.paramiko_tunnel(server, password="qualquer").start(threading.Event())
        self.assertEqual(caught.exception.kind, "auth")
        self.assertIn("não aceita login por senha", str(caught.exception))

    def test_password_through_keyboard_interactive(self):
        # Servidores com PAM costumam oferecer a senha só por "keyboard-interactive".
        server = self.server(allowed_auths=("keyboard-interactive", "publickey"))
        self.paramiko_tunnel(server, password=server.password, ssh_dir=self.empty_dir).start(threading.Event())
        self.assertGreater(self.probe(), 0)

    def test_wrong_password_through_keyboard_interactive(self):
        server = self.server(allowed_auths=("keyboard-interactive", "publickey"))
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.paramiko_tunnel(server, password="errada", ssh_dir=self.empty_dir).start(threading.Event())
        self.assertEqual(str(caught.exception), "usuário ou senha SSH incorretos")

    def test_key_login(self):
        server = self.server()
        item = self.paramiko_tunnel(server)
        item.start(threading.Event())
        self.assertGreater(self.probe(), 0)
        self.assert_logins(server, 1)

    def test_key_not_authorized(self):
        server = self.server(authorized_keys=[])
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.paramiko_tunnel(server).start(threading.Event())
        self.assertEqual(caught.exception.kind, "auth")
        self.assertIn("recusou a chave SSH", str(caught.exception))

    def test_no_key_available(self):
        server = self.server()
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.paramiko_tunnel(server, ssh_dir=self.empty_dir).start(threading.Event())
        self.assertEqual(caught.exception.kind, "auth")
        self.assertIn("nenhuma chave SSH encontrada", str(caught.exception))

    def test_key_protected_by_passphrase(self):
        server = self.server()
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.paramiko_tunnel(server, ssh_dir=self.locked_dir).start(threading.Event())
        self.assertEqual(caught.exception.kind, "auth")
        self.assertIn("passphrase", str(caught.exception))

    # -- chave do servidor --

    def assert_host_key_refused(self, server, folder, host=HOST):
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.paramiko_tunnel(server, password=server.password, ssh_dir=folder, host=host).start(threading.Event())
        self.assertEqual(caught.exception.kind, "hostkey")
        self.assert_logins(server, 0)            # a senha nem chegou a ser enviada

    def test_known_host_with_same_key_is_accepted(self):
        server = self.server()
        folder = self.known_hosts_dir([self.known_line(f"[{HOST}]:{server.port}", server.host_key)])
        self.paramiko_tunnel(server, ssh_dir=folder).start(threading.Event())
        self.assertGreater(self.probe(), 0)

    def test_known_host_with_changed_key_is_refused(self):
        server = self.server()
        folder = self.known_hosts_dir([self.known_line(f"[{HOST}]:{server.port}", paramiko.ECDSAKey.generate())])
        self.assert_host_key_refused(server, folder)

    def test_known_host_presenting_another_key_type_is_refused(self):
        # O known_hosts só tem uma chave RSA; o servidor (ou um impostor) só oferece ECDSA.
        server = self.server()
        folder = self.known_hosts_dir([self.known_line(f"[{HOST}]:{server.port}", paramiko.RSAKey.generate(2048))])
        self.assert_host_key_refused(server, folder)

    def test_one_of_several_known_keys_is_enough(self):
        server = self.server()
        name = f"[{HOST}]:{server.port}"
        folder = self.known_hosts_dir([
            self.known_line(name, paramiko.ECDSAKey.generate()),      # chave antiga, do mesmo tipo
            self.known_line(name, server.host_key),
        ])
        self.paramiko_tunnel(server, ssh_dir=folder).start(threading.Event())
        self.assertGreater(self.probe(), 0)

    def test_unreadable_known_hosts_lines_do_not_disable_the_check(self):
        server = self.server()
        folder = self.known_hosts_dir([
            "# comentário",
            "@cert-authority *.exemplo.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFake",
            "servidor-quebrado ssh-ed25519 isto-não-é-base64!!!",
            "linha sem campos suficientes",
            self.known_line(f"[{HOST}]:{server.port}", paramiko.ECDSAKey.generate()),
        ])
        self.assert_host_key_refused(server, folder)

    def test_host_name_case_does_not_matter(self):
        server = self.server()
        folder = self.known_hosts_dir([self.known_line(f"[localhost]:{server.port}", paramiko.ECDSAKey.generate())])
        self.assert_host_key_refused(server, folder, host="LocalHost")

    def test_hashed_known_host_is_recognised(self):
        server = self.server()
        hashed = paramiko.HostKeys.hash_host(f"[{HOST}]:{server.port}")
        folder = self.known_hosts_dir([self.known_line(hashed, paramiko.ECDSAKey.generate())])
        self.assert_host_key_refused(server, folder)

    def test_other_hosts_in_known_hosts_do_not_interfere(self):
        server = self.server()
        folder = self.known_hosts_dir([
            self.known_line("outro.exemplo.com", paramiko.ECDSAKey.generate()),
            self.known_line(f"[{HOST}]:{server.port + 1}", paramiko.ECDSAKey.generate()),
        ])
        self.paramiko_tunnel(server, ssh_dir=folder).start(threading.Event())
        self.assertGreater(self.probe(), 0)

    def test_reconnection_refuses_a_server_with_another_key(self):
        # Servidor nunca visto (sem known_hosts): aceito no primeiro login, mas a
        # chave fica fixada na memória e uma reconexão para um impostor é recusada.
        server = self.server()
        item = self.paramiko_tunnel(server, password=server.password, ssh_dir=self.empty_dir)
        item.start(threading.Event())
        item.stop()
        item.start(threading.Event())                 # mesmo servidor: reconecta normalmente
        item.stop()
        self.assert_logins(server, 2)
        server.host_key = paramiko.ECDSAKey.generate()
        with self.assertRaises(tunnel.TunnelError) as caught:
            item.start(threading.Event())
        self.assertEqual(caught.exception.kind, "hostkey")
        self.assert_logins(server, 2)            # a senha não foi enviada ao impostor
        # Um túnel novo (Parar e Iniciar de novo, com o app aberto) também recusa.
        again = self.paramiko_tunnel(server, password=server.password, ssh_dir=self.empty_dir)
        with self.assertRaises(tunnel.TunnelError) as caught:
            again.start(threading.Event())
        self.assertEqual(caught.exception.kind, "hostkey")
        self.assert_logins(server, 2)

    def test_a_key_listed_in_known_hosts_is_accepted_after_another_type_was_seen(self):
        # Servidor com chaves ECDSA e RSA. O primeiro login (known_hosts vazio) negocia
        # ECDSA, que fica lembrada. Depois o known_hosts ganha só a RSA (um `ssh` no
        # terminal, por exemplo) e a reconexão passa a negociar RSA: a chave está no
        # known_hosts, então não pode ser recusada como "chave trocada".
        server = self.server()
        rsa = paramiko.RSAKey.generate(2048)
        server.extra_host_keys = [rsa]
        folder = self.known_hosts_dir([])
        item = self.paramiko_tunnel(server, password=server.password, ssh_dir=folder)
        item.start(threading.Event())
        self.assertEqual(item._transport.get_remote_server_key().get_name(), server.host_key.get_name())
        item.stop()
        with open(os.path.join(folder, "known_hosts"), "w", encoding="utf-8") as handle:
            handle.write(self.known_line(f"[{HOST}]:{server.port}", rsa) + "\n")
        item.start(threading.Event())
        self.assertEqual(item._transport.get_remote_server_key().asbytes(), rsa.asbytes())
        self.assertGreater(self.probe(), 0)
        self.assert_logins(server, 2)

    def test_a_server_that_gains_a_new_key_type_keeps_presenting_the_remembered_one(self):
        # Sem known_hosts: o login seguinte pede primeiro o tipo já lembrado, então um
        # servidor que passou a ter também outro tipo de chave continua sendo aceito.
        server = self.server()
        item = self.paramiko_tunnel(server, password=server.password, ssh_dir=self.empty_dir)
        item.start(threading.Event())
        item.stop()
        server.extra_host_keys = [paramiko.RSAKey.generate(2048)]
        item.start(threading.Event())
        self.assertEqual(item._transport.get_remote_server_key().asbytes(), server.host_key.asbytes())
        self.assert_logins(server, 2)

    def test_the_remembered_key_still_refuses_an_impostor_of_another_type(self):
        # A chave lembrada é ECDSA; quem responde agora só tem RSA. Aceitar "um tipo
        # novo" daria a senha a qualquer impostor que escolhesse outro tipo de chave.
        server = self.server()
        item = self.paramiko_tunnel(server, password=server.password, ssh_dir=self.empty_dir)
        item.start(threading.Event())
        item.stop()
        server.host_key = paramiko.RSAKey.generate(2048)
        with self.assertRaises(tunnel.TunnelError) as caught:
            item.start(threading.Event())
        self.assertEqual(caught.exception.kind, "hostkey")
        self.assert_logins(server, 1)            # a senha não foi enviada ao impostor

    # -- rede --

    def test_forwarding_disabled_on_the_server(self):
        server = self.server(allow_forwarding=False)
        item = self.paramiko_tunnel(server)
        item.start(threading.Event())
        with self.assertRaises(OSError):
            self.probe()
        self.assertTrue(item.forwarding_refused())

    def test_port_is_not_ssh(self):
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.track(tunnel.ParamikoTunnel(
                HOST, self.target.address[1], "tester", "x", self.local_port, ssh_dir=self.empty_dir
            )).start(threading.Event())
        self.assertEqual(caught.exception.kind, "network")
        self.assertIn("não respondeu como um servidor SSH", str(caught.exception))

    def test_silent_server_ends_in_a_timeout_within_the_budget(self):
        silent = fakes.HttpTarget(respond=False)      # aceita a conexão e nunca manda o banner SSH
        self.addCleanup(silent.close)
        self.patch(config, "SSH_START_TIMEOUT", 2.0)
        started = time.monotonic()
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.track(tunnel.ParamikoTunnel(
                HOST, silent.address[1], "tester", "x", self.local_port, ssh_dir=self.empty_dir
            )).start(threading.Event())
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertEqual(caught.exception.kind, "network")
        self.assertRegex(str(caught.exception), "tempo esgotado|não respondeu como um servidor SSH")

    def test_stop_event_cancels_the_connection(self):
        silent = fakes.HttpTarget(respond=False)
        self.addCleanup(silent.close)
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(tunnel.TunnelError):
            self.track(tunnel.ParamikoTunnel(
                HOST, silent.address[1], "tester", "x", self.local_port, ssh_dir=self.empty_dir
            )).start(stop)
        self.assertLess(time.monotonic() - started, 9.0)   # sem o Parar seriam os 10 s inteiros

    def test_connection_refused(self):
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.track(tunnel.ParamikoTunnel(
                HOST, fakes.free_port(), "tester", "x", self.local_port, ssh_dir=self.empty_dir
            )).start(threading.Event())
        self.assertEqual(caught.exception.kind, "network")
        self.assertIn("conexão recusada", str(caught.exception))

    def test_stop_is_honoured_while_the_tcp_connection_is_still_pending(self):
        # 192.0.2.1 é um endereço reservado para documentação: ninguém responde, o connect() fica pendurado.
        stop = threading.Event()
        threading.Timer(0.7, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.track(tunnel.ParamikoTunnel(
                "192.0.2.1", 22, "tester", "x", self.local_port, ssh_dir=self.empty_dir
            )).start(stop)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(caught.exception.kind, "network")

    def test_unreachable_server_times_out_within_the_budget(self):
        self.patch(config, "SSH_START_TIMEOUT", 2.0)
        started = time.monotonic()
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.track(tunnel.ParamikoTunnel(
                "192.0.2.1", 22, "tester", "x", self.local_port, ssh_dir=self.empty_dir
            )).start(threading.Event())
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual(caught.exception.kind, "network")

    def test_host_name_that_dns_cannot_even_encode(self):
        for host in ("meu..servidor.com", "a" * 70 + ".com"):
            with self.subTest(host=host):
                with self.assertRaises(tunnel.TunnelError) as caught:
                    self.track(tunnel.ParamikoTunnel(
                        host, 22, "tester", "x", self.local_port, ssh_dir=self.empty_dir
                    )).start(threading.Event())
                self.assertEqual(str(caught.exception), "servidor não encontrado (confira o IP/host)")

    def test_server_drop_is_noticed(self):
        server = self.server()
        item = self.paramiko_tunnel(server)
        item.start(threading.Event())
        server.stop()
        self.assertTrue(fakes.wait_until(lambda: not item.alive(), 5))
        with self.assertRaises(OSError):
            self.probe()

    def test_local_port_already_taken(self):
        server = self.server()
        blocker = socket.socket()
        self.addCleanup(blocker.close)
        blocker.bind((HOST, self.local_port))
        blocker.listen(1)
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.paramiko_tunnel(server).start(threading.Event())
        self.assertEqual(caught.exception.kind, "port")

    def test_many_parallel_connections(self):
        server = self.server()
        self.paramiko_tunnel(server).start(threading.Event())
        results = []

        def worker():
            try:
                results.append(self.probe() > 0)
            except OSError as exc:
                results.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(results, [True] * 40)

    def test_real_agent_lookup_does_not_hang(self):
        started = time.monotonic()
        try:
            agent = paramiko.agent.Agent()         # o de verdade, só para listar
            agent.get_keys()
            agent.close()
        except Exception:
            pass
        self.assertLess(time.monotonic() - started, 5)


class SshExeTunnelTest(SshCase):
    def test_only_a_protected_system_path_is_used(self):
        if not self.ssh_exe:
            self.skipTest("ssh.exe não disponível")
        self.assertTrue(os.path.isabs(self.ssh_exe))
        allowed = (win.system_directory().lower(), win.program_files().lower())
        self.assertTrue(self.ssh_exe.lower().startswith(allowed), self.ssh_exe)

    def test_a_planted_ssh_in_the_current_folder_is_ignored(self):
        folder = tempfile.mkdtemp(prefix="plantado-", dir=self.work)
        for name in ("ssh.exe", "ssh.bat", "ssh.cmd"):
            with open(os.path.join(folder, name), "w") as handle:
                handle.write("")
        previous = os.getcwd()
        os.chdir(folder)
        self.addCleanup(os.chdir, previous)
        self.assertEqual(tunnel.find_ssh_exe(), self.ssh_exe)

    def test_key_login_forwards_traffic(self):
        server = self.server()
        item = self.ssh_exe_tunnel(server)
        started = time.monotonic()
        item.start(threading.Event())
        self.assertLess(time.monotonic() - started, config.SSH_START_TIMEOUT)
        self.assertGreater(self.probe(), 0)
        self.assertEqual(server.forwarded, [self.target.address])
        owners = win.tcp_listener_pids(self.local_port)
        self.assertTrue(owners)
        self.assertTrue(all((win.process_name(pid) or "").lower() == "ssh.exe" for pid in owners))
        self.assertTrue(item.alive())
        item.stop()
        self.assertFalse(item.alive())
        self.assert_port_free()

    def test_command_line_follows_the_spec(self):
        command = tunnel.SshExeTunnel("ssh.exe", "203.0.113.9", 2222, "ana", 1080).command()
        # "-F none": o ssh roda elevado e não pode obedecer ao ~/.ssh/config, que qualquer programa altera.
        self.assertEqual(command[:6], ["ssh.exe", "-F", "none", "-D", "127.0.0.1:1080", "-N"])
        for option in ("StrictHostKeyChecking=no", "ExitOnForwardFailure=yes", "BatchMode=yes"):
            self.assertIn(option, command)
        self.assertEqual(command[-5:], ["-p", "2222", "-l", "ana", "203.0.113.9"])

    def test_ssh_runs_from_a_folder_only_the_administrator_can_change(self):
        self.assertEqual(
            os.path.normcase(tunnel.SshExeTunnel._working_directory()),
            os.path.normcase(win.system_directory()),
        )

    def test_ssh_variables_from_the_environment_do_not_reach_ssh(self):
        planted = {"SSH_ASKPASS": "C:\\plantado\\x.exe", "SSH_ASKPASS_REQUIRE": "force",
                   "SSH_SK_PROVIDER": "C:\\plantado\\x.dll", "SSH_AUTH_SOCK": "\\\\.\\pipe\\openssh-ssh-agent"}
        os.environ.update(planted)
        for name in planted:
            self.addCleanup(os.environ.pop, name, None)
        environment = tunnel.SshExeTunnel._environment()
        self.assertEqual({name for name in environment if name.upper().startswith("SSH_")}, {"SSH_AUTH_SOCK"})
        self.assertIn("SYSTEMROOT", {name.upper() for name in environment})

    def test_key_not_authorized(self):
        server = self.server()
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.ssh_exe_tunnel(server, key_path=self.other_key_path).start(threading.Event())
        self.assertEqual(caught.exception.kind, "auth")
        self.assertIn("recusou a chave SSH", str(caught.exception))
        self.assert_port_free()

    def test_connection_refused(self):
        if not self.ssh_exe:
            self.skipTest("ssh.exe não disponível")
        item = self.track(tunnel.SshExeTunnel(
            self.ssh_exe, HOST, fakes.free_port(), "tester", self.local_port, extra_options=self.ssh_options()
        ))
        with self.assertRaises(tunnel.TunnelError) as caught:
            item.start(threading.Event())
        self.assertEqual(caught.exception.kind, "network")
        self.assertIn("conexão recusada", str(caught.exception))

    def test_forwarding_disabled_on_the_server(self):
        server = self.server(allow_forwarding=False)
        item = self.ssh_exe_tunnel(server)
        item.start(threading.Event())
        # O ssh -D responde "conectado" antes de tentar o destino; a verificação
        # precisa perceber a falha mesmo assim.
        with self.assertRaises(OSError):
            self.probe()
        self.assertTrue(fakes.wait_until(item.forwarding_refused, 3))

    def test_changed_host_key_is_refused(self):
        server = self.server()
        known_hosts = os.path.join(self.work, f"known_hosts_changed_{self.local_port}")
        with open(known_hosts, "w", encoding="ascii") as handle:
            handle.write(self.known_line(f"[{HOST}]:{server.port}", paramiko.ECDSAKey.generate()) + "\n")
        with self.assertRaises(tunnel.TunnelError) as caught:
            self.ssh_exe_tunnel(server, known_hosts=known_hosts).start(threading.Event())
        self.assertEqual(caught.exception.kind, "hostkey")
        self.assert_port_free()

    def test_server_drop_ends_the_process(self):
        server = self.server()
        item = self.ssh_exe_tunnel(server)
        item.start(threading.Event())
        server.stop()
        self.assertTrue(fakes.wait_until(lambda: not item.alive(), 10))

    def test_stop_event_cancels_the_wait(self):
        silent = fakes.HttpTarget(respond=False)   # aceita a conexão e nunca responde como SSH
        self.addCleanup(silent.close)
        if not self.ssh_exe:
            self.skipTest("ssh.exe não disponível")
        item = self.track(tunnel.SshExeTunnel(
            self.ssh_exe, HOST, silent.address[1], "tester", self.local_port, extra_options=self.ssh_options()
        ))
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(tunnel.TunnelError):
            item.start(stop)
        self.assertLess(time.monotonic() - started, 3)
        self.assertFalse(item.alive())


class LocalPortTest(SshCase):
    def test_default_port_is_used_when_free(self):
        self.assertEqual(tunnel.pick_local_port(self.local_port), self.local_port)

    def test_another_port_is_chosen_when_the_default_is_taken(self):
        blocker = socket.socket()
        self.addCleanup(blocker.close)
        blocker.bind((HOST, self.local_port))
        blocker.listen(1)
        chosen = tunnel.pick_local_port(self.local_port)
        self.assertNotEqual(chosen, self.local_port)
        self.assertTrue(tunnel._can_bind(chosen))
        # Um programa qualquer na porta padrão nunca é encerrado.
        self.assertEqual(tunnel.close_stale_ssh(chosen, preferred=self.local_port), 0)
        self.assertIn(os.getpid(), win.tcp_listener_pids(self.local_port))

    def test_old_ssh_tunnel_survives_until_it_is_explicitly_replaced(self):
        server = self.server()
        old = self.ssh_exe_tunnel(server)
        old.start(threading.Event())
        chosen = tunnel.pick_local_port(self.local_port)       # escolher a porta não derruba nada
        self.assertNotEqual(chosen, self.local_port)
        self.assertTrue(old.alive())
        self.assertGreater(self.probe(), 0)
        self.assertEqual(tunnel.close_stale_ssh(chosen, preferred=self.local_port), 1)
        self.assertTrue(fakes.wait_until(lambda: not old.alive(), 5))

    def test_own_tunnel_on_the_default_port_is_not_closed(self):
        server = self.server()
        own = self.ssh_exe_tunnel(server)
        own.start(threading.Event())
        self.assertEqual(tunnel.close_stale_ssh(self.local_port, preferred=self.local_port), 0)
        self.assertTrue(own.alive())


class SshSessionTest(SshCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.root = fakes.build_fake_discord(tempfile.mkdtemp(prefix="dpl-fake-"))

    def setUp(self):
        super().setUp()
        fakes.reset_fakes(self.root)
        self.addCleanup(fakes.kill_fakes)
        self.online = True
        self.patch(config, "LOCAL_SOCKS_PORT", self.local_port)
        self.patch(config, "HEALTH_INTERVAL", 2.0)
        self.patch(config, "HEALTH_TIMEOUT", 1.5)
        self.patch(config, "SSH_RECONNECT_DELAYS", (0.0, 1.0, 1.0))
        self.patch(config, "DISCORD_RESTART_PAUSE", 0.2)
        self.patch(discord, "open_with_proxy", fakes.open_fake_with_proxy)
        self.patch(socks5, "direct_internet", lambda *args, **kwargs: self.online)
        # Pastas do "usuário" de mentira: a .ssh de teste no lugar da verdadeira.
        profile = tempfile.mkdtemp(prefix="perfil-", dir=self.work)
        self.ssh_dir = os.path.join(profile, ".ssh")
        os.makedirs(self.ssh_dir)
        self.patch(win, "user_dirs", lambda: (self.root, profile))
        self.patch(win, "desktop_user_is_other_account", lambda: False)
        test_case = self

        class TestSshExeTunnel(tunnel.SshExeTunnel):
            def __init__(self, *args, **kwargs):
                kwargs["extra_options"] = test_case.ssh_exe_options
                super().__init__(*args, **kwargs)

        self.ssh_exe_options = self.ssh_options()
        self.patch(tunnel, "SshExeTunnel", TestSshExeTunnel)
        self.recorder = Recorder()
        self._sessions = []
        self.addCleanup(self._stop_sessions)

    def _stop_sessions(self):
        for item in self._sessions:
            item.stop()
        for item in self._sessions:
            item.join(20)

    def install_key(self):
        with open(self.key_path, "rb") as source, open(os.path.join(self.ssh_dir, "id_ed25519"), "wb") as copy:
            copy.write(source.read())

    def start_session(self, server, password=None, port=None):
        item = session.SshSession(
            HOST, port or server.port, server.user, password,
            on_status=self.recorder.on_status, on_finish=self.recorder.on_finish,
            find_install=lambda: fakes.fake_install(self.root),
        )
        self._sessions.append(item)
        item.start()
        return item

    def launches(self):
        return fakes.read_launches(self.root)

    def wait_launches(self, count):
        """O Discord falso leva um instante para subir e anotar a abertura."""
        fakes.wait_until(lambda: len(self.launches()) >= count, 10)
        launches = self.launches()
        self.assertEqual(len(launches), count, launches)
        return launches

    def proxy_flag(self, port=None):
        return f"--proxy-server=socks5://{HOST}:{port or self.local_port}"

    def wait_proxied_launches(self, expected):
        """Espera as aberturas COM proxy, na ordem. (O intruso sem proxy pode ser
        encerrado pela vigia antes de conseguir anotar a própria abertura.)"""
        def proxied():
            return [entry for entry in self.launches() if entry["args"]]
        fakes.wait_until(lambda: len(proxied()) >= len(expected), 10)
        launches = proxied()
        self.assertEqual([entry["args"] for entry in launches], expected)
        fakes.wait_until(lambda: win.pids_by_name(fakes.FAKE_EXE) == [launches[-1]["pid"]], 5)
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [launches[-1]["pid"]])   # só o último, com proxy
        return launches

    @staticmethod
    def active(server):
        return f"Proxy ativo — 127.0.0.1:{server.port}"

    @staticmethod
    def reconnected(server):
        return f"Reconectado via 127.0.0.1:{server.port}"

    def old_discord(self):
        """Um "Discord" já aberto, sem proxy, como o usuário teria antes de usar o launcher."""
        install = fakes.fake_install(self.root)
        pid = win.spawn_detached(install.exe_path, fakes.fake_arguments(install))
        self.wait_launches(1)
        return pid

    def test_password_session_from_start_to_stop(self):
        server = self.server()
        item = self.start_session(server, password=server.password)
        after_green = self.recorder.wait_for(session.GREEN, self.active(server))
        self.assertEqual(self.recorder.events[0], (session.BLUE, "Conectando..."))
        self.assertRegex(self.recorder.events[after_green - 1][1],
                         rf"^Proxy ativo — 127\.0\.0\.1:{server.port} \(\d+ms\)$")
        launches = self.wait_launches(1)
        self.assertEqual(launches[0]["args"], self.proxy_flag())
        self.assertIn(launches[0]["pid"], win.pids_by_name(fakes.FAKE_EXE))
        # o monitor continua verificando a cada intervalo
        self.recorder.wait_for(session.GREEN, "Proxy ativo", start=after_green)
        self.assertGreaterEqual(self.target.hits, 2)

        item.stop()
        self.assertTrue(self.recorder.finished.wait(15))
        self.assertEqual(self.recorder.events[-1], (session.WHITE, "Desconectado"))
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [])      # o Discord aberto pelo launcher fecha
        self.assert_port_free()

    def test_running_discord_is_replaced_only_after_the_tunnel_is_up(self):
        server = self.server()
        old_pid = self.old_discord()
        self.start_session(server, password=server.password)
        self.recorder.wait_for(session.GREEN, "Proxy ativo")
        launches = self.wait_launches(2)                 # o antigo (sem proxy) e o aberto pelo launcher
        running = win.pids_by_name(fakes.FAKE_EXE)
        self.assertEqual(running, [launches[-1]["pid"]])
        self.assertNotEqual(launches[-1]["pid"], old_pid)
        self.assertEqual([entry["args"] for entry in launches], ["", self.proxy_flag()])

    def test_failed_login_leaves_discord_untouched(self):
        server = self.server()
        old_pid = self.old_discord()
        self.start_session(server, password="errada")
        self.assertTrue(self.recorder.finished.wait(15))
        self.assertEqual(self.recorder.events[-1], (session.RED, "Erro: usuário ou senha SSH incorretos"))
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [old_pid])
        self.assert_port_free()

    def test_failed_login_keeps_the_tunnel_that_was_already_working(self):
        # O cenário da especificação: Discord já funcionando por um `ssh -D` manual na porta padrão.
        server = self.server()
        manual = self.ssh_exe_tunnel(server)             # ocupa config.LOCAL_SOCKS_PORT
        manual.start(threading.Event())
        self.start_session(server, password="errada")
        self.assertTrue(self.recorder.finished.wait(15))
        self.assertEqual(self.recorder.events[-1], (session.RED, "Erro: usuário ou senha SSH incorretos"))
        self.assertTrue(manual.alive())                  # o túnel antigo continua de pé...
        self.assertGreater(self.probe(), 0)              # ...e encaminhando

    def test_successful_login_replaces_the_old_tunnel(self):
        server = self.server()
        manual = self.ssh_exe_tunnel(server)
        manual.start(threading.Event())
        self.start_session(server, password=server.password)
        self.recorder.wait_for(session.GREEN, self.active(server))
        self.assertTrue(fakes.wait_until(lambda: not manual.alive(), 5))   # o antigo sai depois que o novo sobe
        flag = self.wait_launches(1)[0]["args"]
        self.assertRegex(flag, r"^--proxy-server=socks5://127\.0\.0\.1:\d+$")
        new_port = int(flag.rsplit(":", 1)[1])
        self.assertNotEqual(new_port, self.local_port)   # a porta padrão estava ocupada na hora de subir
        self.assertGreater(self.probe(new_port), 0)

    def test_discord_not_installed(self):
        server = self.server()
        item = session.SshSession(
            HOST, server.port, server.user, server.password,
            on_status=self.recorder.on_status, on_finish=self.recorder.on_finish,
            find_install=lambda: None,
        )
        self._sessions.append(item)
        item.start()
        self.assertTrue(self.recorder.finished.wait(10))
        self.assertEqual(self.recorder.events[-1], (session.RED, "Erro: Discord não encontrado neste computador"))
        self.assert_logins(server, 0)

    def test_server_without_forwarding_is_reported(self):
        server = self.server(allow_forwarding=False)
        self.start_session(server, password=server.password)
        self.assertTrue(self.recorder.finished.wait(15))
        self.assertEqual(self.recorder.events[-1][0], session.RED)
        self.assertIn("AllowTcpForwarding", self.recorder.events[-1][1])
        self.assertEqual(self.launches(), [])
        self.assert_port_free()

    def test_tunnel_drop_reconnects_without_restarting_discord(self):
        server = self.server()
        self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        server.stop()
        mark = self.recorder.wait_for(session.ORANGE, "Instabilidade detectada", start=mark)
        server.start()
        mark = self.recorder.wait_for(session.GREEN, self.reconnected(server), start=mark)
        self.wait_launches(1)                                       # mesmo endereço de proxy: o Discord fica
        self.recorder.wait_for(session.GREEN, "Proxy ativo", start=mark)
        self.assert_logins(server, 2, at_least=True)

    def test_three_failed_reconnections_end_with_a_permanent_error(self):
        server = self.server()
        self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        self.wait_launches(1)
        server.stop()
        self.recorder.wait_for(session.ORANGE, "Instabilidade detectada", start=mark)
        self.assertTrue(self.recorder.finished.wait(60))
        self.assertEqual(
            self.recorder.events[-1],
            (session.RED, "Erro: o túnel SSH caiu e 3 tentativas de reconexão falharam — Discord fechado"),
        )
        # Sem túnel, o Discord aberto pelo launcher ficaria preso a um proxy morto.
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [])
        self.assert_port_free()

    def test_server_key_change_during_reconnection_is_reported_and_stops_the_session(self):
        server = self.server()
        self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        self.wait_launches(1)
        # A conexão cai e quem responde no mesmo endereço apresenta outra chave (um impostor, por exemplo).
        server.stop()
        server.host_key = paramiko.ECDSAKey.generate()
        server.start()
        self.assertTrue(self.recorder.finished.wait(30))
        self.assertEqual(self.recorder.events[-1], (
            session.RED,
            "Erro: a chave do servidor mudou desde o último login — reabra o launcher se o reinstalou — Discord fechado",
        ))
        self.assert_logins(server, 1)                    # a senha não foi reenviada, nem uma vez
        # Clicar em Iniciar de novo não "resolve": a chave continua recusada enquanto o app estiver aberto.
        self.recorder = Recorder()
        self.start_session(server, password=server.password)
        self.assertTrue(self.recorder.finished.wait(15))
        self.assertEqual(self.recorder.events[-1][0], session.RED)
        self.assertIn("a chave do servidor mudou", self.recorder.events[-1][1])
        self.assert_logins(server, 1)

    def test_wrong_password_during_reconnection_is_reported_at_once(self):
        server = self.server()
        self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        server.stop()
        server.password = "senha-nova-no-servidor"
        server.start()
        self.assertTrue(self.recorder.finished.wait(30))
        self.assertEqual(self.recorder.events[-1],
                         (session.RED, "Erro: usuário ou senha SSH incorretos — Discord fechado"))

    def test_ordinary_outage_keeps_the_local_network_guard_on(self):
        # Rede cai e volta: o servidor volta a responder junto com a internet. A proteção continua valendo.
        server = self.server()
        item = self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        self.patch(session.Session, "_backend_reachable", lambda _self: self.online)
        self.online = False
        self.target.respond = False
        mark = self.recorder.wait_for(session.ORANGE, "Sem internet", start=mark, timeout=30)
        time.sleep(1.0)
        self.target.respond = True
        self.online = True
        self.recorder.wait_for(session.GREEN, "Proxy ativo", start=mark, timeout=30)
        self.assertTrue(item._direct_internet_seen)
        self.assert_logins(server, 1)

    def test_one_failed_check_does_not_trigger_anything(self):
        server = self.server()
        self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        self.target.respond = False
        hits = self.target.hits
        self.assertTrue(fakes.wait_until(lambda: self.target.hits > hits, 10))   # uma verificação falha...
        self.target.respond = True
        self.recorder.wait_for(session.GREEN, "Proxy ativo", start=mark)        # ...e a seguinte passa
        self.assertNotIn(session.ORANGE, self.recorder.colors())
        self.assert_logins(server, 1)

    def test_two_failed_checks_trigger_a_reconnection(self):
        server = self.server()
        self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        self.target.respond = False
        mark = self.recorder.wait_for(session.ORANGE, "Instabilidade detectada", start=mark, timeout=30)
        self.target.respond = True
        self.recorder.wait_for(session.GREEN, self.reconnected(server), start=mark, timeout=30)
        self.assert_logins(server, 2, at_least=True)

    def test_discord_still_reachable_means_the_tunnel_is_healthy(self):
        # 1.1.1.1:80 fora do ar (ou bloqueado para o VPS), mas o Discord responde pelo túnel.
        server = self.server()
        self.discord_reachable = True
        self.target.respond = False
        self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        for _ in range(3):
            mark = self.recorder.wait_for(session.GREEN, "Proxy ativo", start=mark, timeout=30)
        self.assertNotIn(session.ORANGE, self.recorder.colors())
        self.assert_logins(server, 1)

    def test_local_outage_waits_instead_of_reconnecting(self):
        server = self.server()
        self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        self.patch(session.Session, "_backend_reachable", lambda _self: False)   # nada responde: sem rede mesmo
        self.online = False
        self.target.respond = False
        mark = self.recorder.wait_for(session.ORANGE, "Sem internet", start=mark, timeout=30)
        time.sleep(7)
        self.assert_logins(server, 1)                                    # nenhuma reconexão à toa
        self.target.respond = True
        self.online = True
        self.recorder.wait_for(session.GREEN, "Proxy ativo", start=mark, timeout=30)
        self.assert_logins(server, 1)
        self.assertNotIn("Instabilidade detectada", self.recorder.texts())

    def test_unreliable_direct_test_does_not_block_recovery_forever(self):
        # A rede passou a bloquear o teste de internet direta, mas o servidor SSH responde:
        # o app percebe que há rede e segue com a reconexão normal.
        server = self.server()
        self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        self.online = False
        self.target.respond = False
        mark = self.recorder.wait_for(session.ORANGE, "Sem internet", start=mark, timeout=30)
        mark = self.recorder.wait_for(session.ORANGE, "Instabilidade detectada", start=mark, timeout=30)
        self.target.respond = True
        self.recorder.wait_for(session.GREEN, self.reconnected(server), start=mark, timeout=30)

    def test_discord_restarting_without_the_proxy_is_reopened(self):
        server = self.server()
        self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        install = fakes.fake_install(self.root)
        first = self.wait_launches(1)[0]["pid"]
        # Simula a atualização do Discord: o processo aberto pelo launcher sai e outro aparece sem a flag.
        win.kill_pid(first)
        rogue = win.spawn_detached(install.exe_path, fakes.fake_arguments(install))
        mark = self.recorder.wait_for(session.ORANGE, "O Discord reiniciou sem o proxy", start=mark)
        self.recorder.wait_for(session.GREEN, "Proxy ativo", start=mark)
        launches = self.wait_proxied_launches([self.proxy_flag(), self.proxy_flag()])   # o original e o reaberto
        self.assertNotIn(rogue, win.pids_by_name(fakes.FAKE_EXE))                        # o intruso sem proxy saiu
        self.assertNotEqual(launches[0]["pid"], launches[1]["pid"])

    def test_discord_that_keeps_restarting_without_the_proxy_ends_the_session(self):
        server = self.server()
        self.start_session(server, password=server.password)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        install = fakes.fake_install(self.root)
        flags = [self.proxy_flag()]
        for _ in range(3):                               # três reaberturas são toleradas...
            win.kill_pid(self.wait_proxied_launches(flags)[-1]["pid"])
            win.spawn_detached(install.exe_path, fakes.fake_arguments(install))
            mark = self.recorder.wait_for(session.ORANGE, "O Discord reiniciou sem o proxy", start=mark)
            mark = self.recorder.wait_for(session.GREEN, "Proxy ativo", start=mark)
            flags.append(self.proxy_flag())
        win.kill_pid(self.wait_proxied_launches(flags)[-1]["pid"])   # ...na quarta, a sessão encerra com erro claro
        win.spawn_detached(install.exe_path, fakes.fake_arguments(install))
        self.assertTrue(self.recorder.finished.wait(30))
        self.assertEqual(
            self.recorder.events[-1],
            (session.RED, "Erro: o Discord insiste em reiniciar sem o proxy — Discord fechado"),
        )
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [])   # nada de Discord rodando por fora do proxy

    def test_discord_closed_by_the_user_stays_closed(self):
        server = self.server()
        self.start_session(server, password=server.password)
        self.recorder.wait_for(session.GREEN, "Proxy ativo")
        self.wait_launches(1)
        fakes.kill_fakes()
        time.sleep(7)                                   # várias rondas da vigia
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [])
        self.assertNotIn(session.ORANGE, self.recorder.colors())

    def test_key_session_uses_the_windows_ssh_client(self):
        if not self.ssh_exe:
            self.skipTest("ssh.exe não disponível")
        server = self.server()
        item = self.start_session(server)
        self.recorder.wait_for(session.GREEN, self.active(server))
        owners = win.tcp_listener_pids(self.local_port)
        self.assertTrue(owners)
        self.assertTrue(all((win.process_name(pid) or "").lower() == "ssh.exe" for pid in owners))
        self.assertEqual(self.wait_launches(1)[0]["args"], self.proxy_flag())
        item.stop()
        self.assertTrue(self.recorder.finished.wait(15))
        self.assertFalse(set(owners) & {pid for pid, _, _ in win.list_processes()})   # o ssh.exe saiu junto
        self.assert_port_free()

    def test_key_session_reconnects_after_the_ssh_process_dies(self):
        if not self.ssh_exe:
            self.skipTest("ssh.exe não disponível")
        server = self.server()
        self.start_session(server)
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        for pid in win.tcp_listener_pids(self.local_port):
            win.kill_pid(pid)
        mark = self.recorder.wait_for(session.ORANGE, "Instabilidade detectada", start=mark)
        self.recorder.wait_for(session.GREEN, self.reconnected(server), start=mark)
        self.assertGreater(self.probe(), 0)
        self.wait_launches(1)

    def test_key_session_falls_back_to_the_built_in_client(self):
        if not self.ssh_exe:
            self.skipTest("ssh.exe não disponível")
        server = self.server()
        self.install_key()                               # a chave certa está na .ssh do usuário...
        self.ssh_exe_options = self.ssh_options(key_path=self.other_key_path)   # ...mas o ssh.exe usa outra
        self.start_session(server)
        self.recorder.wait_for(session.GREEN, self.active(server))
        self.assertIn(os.getpid(), win.tcp_listener_pids(self.local_port))      # túnel embutido, não ssh.exe

    def test_key_session_falls_back_when_ssh_exe_fails_for_its_own_reasons(self):
        server = self.server()
        self.install_key()

        class BrokenSshExe(tunnel.SshExeTunnel):
            def start(self, stop):
                raise tunnel.TunnelError("não foi possível executar o ssh (bloqueado)")   # kind "other"

        self.patch(tunnel, "SshExeTunnel", BrokenSshExe)
        self.start_session(server)
        self.recorder.wait_for(session.GREEN, self.active(server))
        self.assertIn(os.getpid(), win.tcp_listener_pids(self.local_port))      # o cliente embutido assumiu

    def test_key_session_without_any_key_explains_what_to_do(self):
        server = self.server()
        self.ssh_exe_options = self.ssh_options(key_path=self.other_key_path)
        self.start_session(server)
        self.assertTrue(self.recorder.finished.wait(30))
        self.assertEqual(
            self.recorder.events[-1],
            (session.RED, "Erro: nenhuma chave SSH encontrada em .ssh — informe a senha"),
        )

    def test_stop_while_connecting_is_quick(self):
        silent = fakes.HttpTarget(respond=False)        # "servidor" que aceita e nunca responde
        self.addCleanup(silent.close)
        server = self.server()
        item = self.start_session(server, password="x", port=silent.address[1])
        time.sleep(0.5)
        started = time.monotonic()
        item.stop()
        self.assertTrue(self.recorder.finished.wait(15))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.recorder.events[-1], (session.WHITE, "Desconectado"))

    def test_abandon_closes_everything_from_another_thread(self):
        # O que a interface faz quando o app precisa fechar e a sessão não respondeu a tempo.
        server = self.server()
        item = self.start_session(server, password=server.password)
        self.recorder.wait_for(session.GREEN, "Proxy ativo")
        self.wait_launches(1)
        item.abandon()
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [])
        self.assert_port_free()


if __name__ == "__main__":
    unittest.main(verbosity=2)
