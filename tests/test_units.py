"""Testes unitários. Rodar da raiz do projeto: python -m unittest discover -s tests -v"""
from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import fakes
from launcher import config, discord, proxies, socks5, tunnel, win
from launcher.target import parse_target


class ParseProxyListTest(unittest.TestCase):
    def test_keeps_only_valid_public_entries(self):
        text = "\n".join([
            "1.2.3.4:1080",
            "socks5://5.6.7.8:9050",
            "1.2.3.4:1080",            # repetido
            "10.0.0.1:1080",           # rede privada
            "127.0.0.1:9050",          # loopback
            "192.168.1.1:1080",
            "999.1.1.1:80",            # octeto inválido
            "01.2.3.4:80",             # zero à esquerda
            "8.8.4.4:0",               # porta inválida
            "8.8.4.4:70000",
            "http://9.9.9.9:80",       # outro protocolo
            "texto qualquer",
            "",
            "  8.8.8.8:53  \r",
        ])
        self.assertEqual(
            proxies.parse_proxy_list(text),
            [("1.2.3.4", 1080), ("5.6.7.8", 9050), ("8.8.8.8", 53)],
        )

    def test_empty(self):
        self.assertEqual(proxies.parse_proxy_list("<html>erro</html>"), [])


class ParseTargetTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(parse_target("203.0.113.9"), ("203.0.113.9", 22, "root"))
        self.assertEqual(parse_target(" vps.exemplo.com ", " 2222 ", " ana "), ("vps.exemplo.com", 2222, "ana"))

    def test_pasted_forms(self):
        self.assertEqual(parse_target("ana@203.0.113.9"), ("203.0.113.9", 22, "ana"))
        self.assertEqual(parse_target("ana@203.0.113.9:2200", "22", "root"), ("203.0.113.9", 2200, "ana"))
        self.assertEqual(parse_target("2001:db8::1"), ("2001:db8::1", 22, "root"))
        self.assertEqual(parse_target("[2001:db8::1]"), ("2001:db8::1", 22, "root"))
        self.assertEqual(parse_target("[2001:db8::1]:2222"), ("2001:db8::1", 2222, "root"))
        self.assertEqual(parse_target("ana@[2001:db8::1]:2222", "22", "root"), ("2001:db8::1", 2222, "ana"))

    def test_rejects_bad_input(self):
        for host, port, user in [
            ("", "", ""),
            ("-oProxyCommand=calc", "", ""),      # viraria uma opção do ssh.exe
            ("host com espaco", "", ""),
            ("host;calc", "", ""),
            ("203.0.113.9", "0", ""),
            ("203.0.113.9", "65536", ""),
            ("203.0.113.9", "abc", ""),
            ("203.0.113.9", "", "-l"),
            ("203.0.113.9", "", "a b"),
            ("meu..servidor.com", "", ""),        # rótulo vazio: o DNS nem tenta resolver
            ("a" * 64 + ".com", "", ""),          # rótulo longo demais
            ("203.0..113.9", "", ""),
            ("2001:db8::zz", "", ""),             # tem ":" mas não é um IPv6
            ("[2001:db8::1", "", ""),
        ]:
            with self.subTest(host=host, port=port, user=user):
                with self.assertRaises(ValueError):
                    parse_target(host, port, user)


class ProxyPoolTest(unittest.TestCase):
    def make_pool(self, good, delay=0.03):
        state = {"now": 0, "max": 0, "calls": 0}
        lock = threading.Lock()

        def check(proxy):
            with lock:
                state["now"] += 1
                state["calls"] += 1
                state["max"] = max(state["max"], state["now"])
            try:
                time.sleep(delay)
                if proxy not in good:
                    raise TimeoutError("morto")
                return proxies.CheckResult(connect_ms=good[proxy] / 2, total_ms=good[proxy])
            finally:
                with lock:
                    state["now"] -= 1

        return proxies.ProxyPool(check), state

    @staticmethod
    def addresses(count):
        return [(f"198.51.{index // 250}.{index % 250 + 1}", 1080) for index in range(count)]

    def test_tests_everything_with_at_most_ten_at_a_time(self):
        everyone = self.addresses(60)
        good = {proxy: 100.0 + index for index, proxy in enumerate(everyone) if index % 6 == 0}
        pool, state = self.make_pool(good)
        self.assertEqual(pool.load(everyone), 60)
        progress = []
        pool.test_until(None, threading.Event(), lambda *numbers: progress.append(numbers))
        self.assertEqual(pool.tested, 60)
        self.assertEqual(state["max"], config.TEST_CONCURRENCY)
        self.assertEqual(progress[-1], (60, 60, 10))
        self.assertEqual([p[0] for p in progress], list(range(1, 61)))
        ordered = []
        while (entry := pool.pop_best()) is not None:
            ordered.append(entry.total_ms)
        self.assertEqual(ordered, sorted(good.values()))
        self.assertFalse(pool.has_untested())

    def test_stops_early_and_keeps_the_rest_for_later(self):
        everyone = self.addresses(300)
        good = {proxy: 50.0 for index, proxy in enumerate(everyone) if index % 3 == 0}
        pool, state = self.make_pool(good)
        pool.load(everyone)
        progress = []
        started = time.monotonic()
        pool.test_until(5, threading.Event(), lambda *numbers: progress.append((time.monotonic(), numbers)))
        returned = time.monotonic()
        self.assertGreaterEqual(pool.standby_count(), 5)
        self.assertLess(state["calls"], 80)
        self.assertTrue(pool.has_untested())
        self.assertLess(returned - started, 3)
        time.sleep(0.3)                      # os testes em andamento terminam em segundo plano...
        self.assertTrue(all(moment <= returned for moment, _ in progress))   # ...sem mexer no status
        calls = state["calls"]
        target = pool.standby_count() + 6
        pool.test_until(target, threading.Event())   # continua de onde parou
        self.assertGreaterEqual(pool.standby_count(), target)
        self.assertGreater(state["calls"], calls)

    def test_stop_event_interrupts(self):
        pool, state = self.make_pool({}, delay=0.2)
        pool.load(self.addresses(500))
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        started = time.monotonic()
        pool.test_until(None, stop)
        self.assertLess(time.monotonic() - started, 2)
        self.assertLess(state["calls"], 60)

    def test_local_outage_pauses_instead_of_burning_the_list(self):
        # Rede local fora do ar: toda conexão falha na hora, e a culpa não é dos proxies.
        everyone = self.addresses(50)
        state = {"offline": True, "tested_while_offline": None}

        def check(proxy):
            if state["offline"]:
                raise OSError(10051, "rede inacessível")
            if proxy != everyone[10]:
                raise TimeoutError("morto")
            return proxies.CheckResult(connect_ms=50.0, total_ms=100.0)

        pool = proxies.ProxyPool(check)
        pool.load(everyone)
        announced = []

        def network_returns():
            state["tested_while_offline"] = pool.tested
            state["offline"] = False

        threading.Timer(1.0, network_returns).start()
        pool.test_until(
            None, threading.Event(),
            is_offline=lambda failed_at: state["offline"], on_offline=lambda: announced.append("sem rede"),
        )
        self.assertEqual(state["tested_while_offline"], 0)     # nada foi reprovado durante a queda
        self.assertEqual(announced, ["sem rede"])              # a interface foi avisada uma vez
        self.assertEqual(pool.tested, 50)                      # e tudo foi testado quando a rede voltou
        self.assertEqual(pool.pop_best().proxy, everyone[10])

    def test_a_proxy_that_answers_is_never_blamed_on_the_local_network(self):
        everyone = self.addresses(30)
        asked = []

        def check(proxy):
            raise ConnectionRefusedError("proxy fechado")      # recusou: logo, a rede está de pé

        pool = proxies.ProxyPool(check)
        pool.load(everyone)
        pool.test_until(None, threading.Event(), is_offline=lambda failed_at: asked.append(failed_at) or True)
        self.assertEqual(asked, [])                            # nem foi preciso perguntar
        self.assertEqual(pool.tested, 30)
        self.assertFalse(pool.has_untested())

    def test_silent_failures_with_the_network_up_are_real_failures(self):
        everyone = self.addresses(30)
        pool, _ = self.make_pool({}, delay=0.0)                # todos dão "tempo esgotado"
        pool.load(everyone)
        pool.test_until(None, threading.Event(), is_offline=lambda failed_at: False)
        self.assertEqual(pool.tested, 30)

    def test_put_back_and_forget_failures(self):
        everyone = self.addresses(4)
        good = {everyone[0]: 10.0, everyone[1]: 20.0}
        pool, _ = self.make_pool(good, delay=0.0)
        pool.load(everyone)
        pool.test_until(None, threading.Event())
        best = pool.pop_best()
        pool.put_back(best)
        self.assertEqual(pool.pop_best().proxy, everyone[0])   # voltou para o lugar certo da fila
        self.assertEqual(pool.load(everyone), 0)
        pool.forget_failures()
        self.assertEqual(pool.load(everyone), 3)               # só o que continua na reserva fica de fora

    def test_does_not_retest_recent_failures_or_standby(self):
        everyone = self.addresses(20)
        good = {everyone[0]: 10.0, everyone[1]: 20.0}
        pool, _ = self.make_pool(good, delay=0.0)
        pool.load(everyone)
        pool.test_until(None, threading.Event())
        self.assertEqual(pool.load(everyone), 0)             # tudo testado há pouco
        self.assertEqual(pool.load(everyone + [("203.0.113.77", 1)]), 1)
        pool.mark_failed(everyone[0])
        self.assertEqual(pool.pop_best().proxy, everyone[1])
        self.assertIsNone(pool.pop_best())


class SocketChannel:
    """Faz um socket comum se passar por um canal do Paramiko."""

    def __init__(self, sock):
        self._sock = sock

    def recv(self, count):
        return self._sock.recv(count)

    def sendall(self, data):
        self._sock.sendall(data)

    def shutdown_write(self):
        self._sock.shutdown(socket.SHUT_WR)

    def close(self):
        self._sock.close()


def plain_socks_proxy():
    """O servidor SOCKS5 do app ligado direto à rede (sem SSH): um proxy de teste."""
    port = fakes.free_port()
    server = tunnel._SocksServer(
        port, lambda host, target_port, source: SocketChannel(socket.create_connection((host, target_port), timeout=5))
    )
    return server, ("127.0.0.1", port)


class Socks5Test(unittest.TestCase):
    def setUp(self):
        self.target = fakes.HttpTarget()
        self.server, self.proxy = plain_socks_proxy()
        self.addCleanup(self.target.close)
        self.addCleanup(self.server.close)

    def test_probe_by_ip_and_by_name(self):
        self.assertGreater(socks5.probe(self.proxy, self.target.address, 5), 0)
        self.assertGreater(socks5.probe(self.proxy, ("localhost", self.target.address[1]), 5), 0)
        self.assertEqual(self.target.hits, 2)

    def test_relays_data_both_ways(self):
        sock = socks5.connect(self.proxy, *self.target.address, deadline=time.monotonic() + 5)
        with sock:
            sock.sendall(b"GET / HTTP/1.1\r\n\r\n")
            data = b""
            while chunk := sock.recv(4096):
                data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.1 200 OK"))

    def test_probe_fails_when_destination_is_closed(self):
        with self.assertRaises(socks5.ProxyError):
            socks5.probe(self.proxy, ("127.0.0.1", fakes.free_port()), 5)

    def test_probe_fails_when_destination_stays_silent(self):
        silent = fakes.HttpTarget(respond=False)
        self.addCleanup(silent.close)
        started = time.monotonic()
        with self.assertRaises(OSError):
            socks5.probe(self.proxy, silent.address, 1.0)
        self.assertLess(time.monotonic() - started, 2.5)

    def test_rejects_something_that_is_not_a_socks_proxy(self):
        with self.assertRaises(OSError):
            socks5.probe(self.target.address, ("1.1.1.1", 80), 3)

    def test_proxy_is_down(self):
        with self.assertRaises(OSError):
            socks5.probe(("127.0.0.1", fakes.free_port()), ("1.1.1.1", 80), 3)


class FakeDiscordCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = fakes.build_fake_discord(tempfile.mkdtemp(prefix="dpl-fake-"))

    def setUp(self):
        fakes.kill_fakes()
        self.addCleanup(fakes.kill_fakes)

    def launches(self):
        return fakes.read_launches(self.root)


class DiscordTest(FakeDiscordCase):
    def test_finds_newest_complete_version(self):
        install = fakes.fake_install(self.root)
        self.assertEqual(install.version, "1.0.10")          # 1.0.10 > 1.0.4; a 1.0.11 não tem .exe
        self.assertTrue(install.exe_path.endswith(os.path.join("app-1.0.10", fakes.FAKE_EXE)))

    def test_missing_install(self):
        self.assertIsNone(discord.find_install(tempfile.mkdtemp(prefix="dpl-empty-"), fakes.FAKE_VARIANTS))

    def test_real_discord_is_located_without_touching_it(self):
        install = discord.find_install()
        if install is None:
            self.skipTest("Discord não instalado nesta máquina")
        self.assertTrue(os.path.isfile(install.exe_path))
        self.assertRegex(install.exe_path, r"\\Discord\\app-[\d.]+\\Discord\.exe$")

    def test_open_with_proxy_passes_the_chromium_flag(self):
        install = fakes.fake_install(self.root)
        calls = []
        original = win.spawn_detached
        win.spawn_detached = lambda executable, arguments: calls.append((executable, arguments)) or 4321
        self.addCleanup(setattr, win, "spawn_detached", original)
        self.assertEqual(discord.open_with_proxy(install, "203.0.113.5", 4145), 4321)
        self.assertEqual(calls, [(install.exe_path, ["--proxy-server=socks5://203.0.113.5:4145"])])

    def test_open_then_close(self):
        install = fakes.fake_install(self.root)
        before = len(self.launches())
        pid = fakes.open_fake_with_proxy(install, "203.0.113.5", 4145)
        self.assertTrue(fakes.wait_until(lambda: len(self.launches()) > before))
        launch = self.launches()[-1]
        self.assertEqual(launch["pid"], pid)
        self.assertEqual(launch["args"], "--proxy-server=socks5://203.0.113.5:4145")
        self.assertTrue(discord.is_running(install))
        self.assertEqual(discord.close(install), 1)
        self.assertFalse(discord.is_running(install))
        self.assertEqual(discord.close(install), 0)          # fechar sem nada aberto não é erro


class WinTest(FakeDiscordCase):
    def test_process_list_and_port_owner(self):
        me = os.getpid()
        self.assertIn(me, [pid for pid, _, _ in win.list_processes()])
        self.assertEqual(win.process_name(me).lower(), os.path.basename(sys.executable).lower())
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.assertIn(me, win.tcp_listener_pids(port))
        self.assertEqual(win.tcp_listener_pids(fakes.free_port()), [])

    def test_known_folders_match_the_environment(self):
        for folder, variable in ((win.FOLDERID_LOCAL_APP_DATA, "LOCALAPPDATA"), (win.FOLDERID_PROFILE, "USERPROFILE")):
            self.assertEqual(
                os.path.normcase(win._known_folder(folder, None)), os.path.normcase(os.environ[variable])
            )
        self.assertEqual(win.user_dirs(), (os.environ["LOCALAPPDATA"], os.environ["USERPROFILE"]))
        self.assertFalse(win.desktop_user_is_other_account())

    def test_system_paths_come_from_windows_not_from_the_environment(self):
        system32 = os.path.join(os.environ["SystemRoot"], "System32")
        program_files = os.environ["ProgramFiles"]
        tampered = {"SystemRoot": "C:\\falso", "windir": "C:\\falso", "ProgramFiles": "C:\\falso"}
        original = {name: os.environ[name] for name in tampered if name in os.environ}
        os.environ.update(tampered)
        try:
            found = (win.system_directory(), win.program_files())
        finally:
            os.environ.update(original)
        self.assertEqual(os.path.normcase(found[0]), os.path.normcase(system32))
        self.assertEqual(os.path.normcase(found[1]), os.path.normcase(program_files))

    def test_desktop_user_is_identified_without_explorers_token(self):
        domain, name = win.session_user()
        self.assertEqual(name.lower(), os.environ["USERNAME"].lower())
        sid = win.account_sid(domain, name)
        self.assertRegex(sid, r"^S-1-\d+-")
        self.assertEqual(sid, win.process_user_sid())
        local, profile = win.profile_dirs_from_registry(sid)
        self.assertEqual(os.path.normcase(local), os.path.normcase(os.environ["LOCALAPPDATA"]))
        self.assertEqual(os.path.normcase(profile), os.path.normcase(os.environ["USERPROFILE"]))
        user = win._DesktopUser()
        self.assertFalse(user.other_account)
        overrides = user.environment_overrides()
        for variable in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "USERNAME", "HOMEDRIVE", "HOMEPATH"):
            self.assertEqual(
                os.path.normcase(overrides[variable]), os.path.normcase(os.environ[variable]), variable
            )

    def test_another_account_gets_its_own_profile_variables(self):
        # O caso que não dá para reproduzir aqui: launcher elevado com a conta de outra pessoa.
        user = win._DesktopUser()
        user.other_account, user.domain, user.name = True, "PC", "Filho"
        user.profile = "C:\\Users\\Filho"
        user.local_app_data = "C:\\Users\\Filho\\AppData\\Local"
        self.assertEqual(user.environment_overrides(), {
            "USERPROFILE": "C:\\Users\\Filho",
            "HOMEDRIVE": "C:",
            "HOMEPATH": "\\Users\\Filho",
            "APPDATA": "C:\\Users\\Filho\\AppData\\Roaming",
            "LOCALAPPDATA": "C:\\Users\\Filho\\AppData\\Local",
            "TEMP": "C:\\Users\\Filho\\AppData\\Local\\Temp",
            "TMP": "C:\\Users\\Filho\\AppData\\Local\\Temp",
            "USERNAME": "Filho",
            "USERDOMAIN": "PC",
        })

    def test_elevated_launcher_starts_the_program_without_elevation(self):
        # Finge que o launcher está elevado: o caminho usado é o de "filho do Explorer".
        self.patch_win("is_elevated", lambda: True)
        install = fakes.fake_install(self.root)
        before = len(self.launches())
        pid = win.spawn_detached(install.exe_path, fakes.fake_arguments(install, "y"))
        self.assertTrue(fakes.wait_until(lambda: len(self.launches()) > before))
        parents = {child: parent for child, parent, _ in win.list_processes()}
        self.assertEqual(parents[pid], self.shell_pid())

    @staticmethod
    def no_explorer(*args):
        raise OSError("o Explorer do Windows não está em execução")

    def no_sleep(self):
        original = win.time.sleep
        win.time.sleep = lambda seconds: None
        self.addCleanup(setattr, win.time, "sleep", original)

    def test_limited_token_start(self):
        # O plano B do launcher elevado: token do próprio processo, sem os poderes de administrador.
        os.environ["DPL_TEST_MARKER"] = "1"
        self.addCleanup(os.environ.pop, "DPL_TEST_MARKER", None)
        install = fakes.fake_install(self.root)
        before = len(self.launches())
        pid = win.spawn_with_limited_token(install.exe_path, fakes.fake_arguments(install, "--proxy-server=socks5://127.0.0.1:2"))
        self.assertTrue(fakes.wait_until(lambda: len(self.launches()) > before))
        launch = self.launches()[-1]
        self.assertEqual(launch["pid"], pid)
        self.assertEqual(launch["args"], "--proxy-server=socks5://127.0.0.1:2")
        self.assertFalse(launch["elevated"])
        self.assertEqual(launch["leaked"], "")                 # ambiente do usuário, não o do launcher
        self.assertEqual(win.kill_by_name(fakes.FAKE_EXE), 1)  # e o launcher consegue encerrá-lo

    def test_without_explorer_the_limited_token_is_used(self):
        self.patch_win("is_elevated", lambda: True)
        self.patch_win("spawn_as_desktop_user", self.no_explorer)
        install = fakes.fake_install(self.root)
        before = len(self.launches())
        pid = win.spawn_detached(install.exe_path, fakes.fake_arguments(install, "z"))
        self.assertTrue(fakes.wait_until(lambda: len(self.launches()) > before))
        self.assertEqual(self.launches()[-1]["pid"], pid)

    def test_never_falls_back_to_an_elevated_start(self):
        def broken(*args):
            raise OSError("CreateRestrictedToken falhou")

        self.patch_win("is_elevated", lambda: True)
        self.patch_win("spawn_as_desktop_user", self.no_explorer)
        self.patch_win("spawn_with_limited_token", broken)
        self.no_sleep()
        install = fakes.fake_install(self.root)
        before = len(self.launches())
        with self.assertRaises(OSError):
            win.spawn_detached(install.exe_path, fakes.fake_arguments(install, "z"))
        time.sleep(0.5)
        self.assertEqual(len(self.launches()), before)         # nada foi aberto como administrador
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [])

    def test_another_account_never_gets_the_launchers_token(self):
        # Launcher elevado com a conta de outra pessoa: o token dele, mesmo rebaixado, abriria o perfil errado.
        used = []

        class OtherAccount:
            other_account = True

        self.patch_win("is_elevated", lambda: True)
        self.patch_win("desktop_user", lambda: OtherAccount())
        self.patch_win("spawn_as_desktop_user", self.no_explorer)
        self.patch_win("spawn_with_limited_token", lambda *args: used.append(args) or 1)
        self.no_sleep()
        install = fakes.fake_install(self.root)
        with self.assertRaises(OSError):
            win.spawn_detached(install.exe_path, fakes.fake_arguments(install, "z"))
        self.assertEqual(used, [])

    def patch_win(self, name, value):
        original = getattr(win, name)
        setattr(win, name, value)
        self.addCleanup(setattr, win, name, original)

    def test_single_instance(self):
        name = f"dpl-teste-{os.getpid()}-{time.monotonic_ns()}"
        self.assertTrue(win.acquire_single_instance(name))
        self.assertFalse(win.acquire_single_instance(name))

    def shell_pid(self):
        pid = ctypes.wintypes.DWORD()
        win.user32.GetWindowThreadProcessId(win.user32.GetShellWindow(), ctypes.byref(pid))
        return pid.value

    def spawn_as_desktop_user(self):
        install = fakes.fake_install(self.root)
        before = len(self.launches())
        pid = win.spawn_as_desktop_user(
            install.exe_path, fakes.fake_arguments(install, "--proxy-server=socks5://127.0.0.1:1")
        )
        self.assertTrue(fakes.wait_until(lambda: len(self.launches()) > before))
        launch = self.launches()[-1]
        self.assertEqual(launch["pid"], pid)
        self.assertEqual(launch["args"], "--proxy-server=socks5://127.0.0.1:1")
        parents = {child: parent for child, parent, _ in win.list_processes()}
        self.assertEqual(parents[pid], self.shell_pid())      # filho do Explorer, não do launcher
        self.assertFalse(launch["elevated"])
        return launch

    def test_spawn_as_desktop_user_gets_the_users_own_environment(self):
        os.environ["DPL_TEST_MARKER"] = "1"
        self.addCleanup(os.environ.pop, "DPL_TEST_MARKER", None)
        launch = self.spawn_as_desktop_user()
        self.assertEqual(launch["leaked"], "")               # o ambiente veio do Explorer, não do launcher

    def test_spawn_as_desktop_user_without_environment_block(self):
        # Emojis no valor: o bloco montado à mão precisa contar o tamanho em UTF-16.
        os.environ["DPL_TEST_MARKER"] = "\U0001F600\U0001F680\U0001F4BB"
        os.environ["TCL_LIBRARY"] = r"C:\falso\_MEI123\tcl"
        self.addCleanup(os.environ.pop, "DPL_TEST_MARKER", None)
        self.addCleanup(os.environ.pop, "TCL_LIBRARY", None)
        original = win.userenv.CreateEnvironmentBlock
        win.userenv.CreateEnvironmentBlock = lambda *args: 0
        self.addCleanup(setattr, win.userenv, "CreateEnvironmentBlock", original)
        launch = self.spawn_as_desktop_user()
        # Sem o bloco do Explorer vale o ambiente do launcher, já sem as variáveis do empacotador.
        self.assertEqual(launch["leaked"], "DPL_TEST_MARKER;")

    def test_plain_spawn_strips_packager_variables(self):
        os.environ["TCL_LIBRARY"] = r"C:\falso\_MEI123\tcl"
        os.environ["_PYI_APPLICATION_HOME_DIR"] = r"C:\falso\_MEI123"
        self.addCleanup(os.environ.pop, "TCL_LIBRARY", None)
        self.addCleanup(os.environ.pop, "_PYI_APPLICATION_HOME_DIR", None)
        install = fakes.fake_install(self.root)
        before = len(self.launches())
        self.assertFalse(win.is_elevated(), "este teste presume um processo sem elevação")
        win.spawn_detached(install.exe_path, fakes.fake_arguments(install, "x"))
        self.assertTrue(fakes.wait_until(lambda: len(self.launches()) > before))
        self.assertEqual(self.launches()[-1]["leaked"], "")

    def test_child_dies_with_the_launcher(self):
        install = fakes.fake_install(self.root)
        helper = (
            "import subprocess, sys, time\n"
            f"sys.path.insert(0, {fakes.ROOT!r})\n"
            "from launcher import win\n"
            f"child = subprocess.Popen({[install.exe_path, *fakes.fake_arguments(install)]!r})\n"
            "win.kill_with_launcher(child)\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        parent = subprocess.Popen([sys.executable, "-c", helper], stdout=subprocess.PIPE, text=True)
        self.addCleanup(parent.kill)
        child_pid = int(parent.stdout.readline())
        self.assertIn(child_pid, win.pids_by_name(fakes.FAKE_EXE))
        parent.kill()                         # simula o launcher morrendo de repente
        parent.wait(5)
        self.assertTrue(fakes.wait_until(lambda: child_pid not in win.pids_by_name(fakes.FAKE_EXE), 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
