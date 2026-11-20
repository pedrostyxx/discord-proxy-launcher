"""Modo B de ponta a ponta com o Discord falso.

A maior parte usa uma "internet" roteirizada (listas e proxies de mentira) para
que cada cenário de failover seja exato e repetível. O último teste usa as
listas e os proxies públicos de verdade; ele só roda com DPL_NETWORK_TESTS=1.
"""
from __future__ import annotations

import os
import re
import socket
import tempfile
import threading
import time
import unittest

import fakes
from launcher import config, dconnect, discord, proxies, session, socks5, win
from test_ssh_mode import Recorder

_real_connect = socks5.connect


def address(index: int, block: int = 51) -> proxies.Proxy:
    return (f"198.{block}.100.{index}", 1080)


class World:
    """Internet de mentira: quais proxies estão vivos (e com que latência) e quais listas existem."""

    def __init__(self):
        self.alive: dict[proxies.Proxy, float] = {}
        self.lists: list[tuple[str, list[proxies.Proxy]]] = []
        self.check_delay = 0.01
        self.offline = False            # rede local caída: toda conexão falha na hora
        self.checked: list[proxies.Proxy] = []
        self.probed: list[proxies.Proxy] = []
        self.downloads = 0

    def download_lists(self, stop):
        self.downloads += 1
        if not self.offline:
            yield from self.lists

    def check(self, proxy, timeout=None):
        if self.offline:
            raise OSError(10051, "rede inacessível")
        time.sleep(self.check_delay)
        self.checked.append(proxy)
        latency = self.alive.get(proxy)
        if latency is None:
            raise TimeoutError("proxy morto")
        return proxies.CheckResult(connect_ms=latency / 2, total_ms=latency)

    def probe(self, proxy, target, timeout):
        if self.offline:
            raise OSError(10051, "rede inacessível")
        proxy = tuple(proxy)
        self.probed.append(proxy)
        latency = self.alive.get(proxy)
        if latency is None:
            raise TimeoutError("proxy morto")
        return latency / 2


class PublicCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = fakes.build_fake_discord(tempfile.mkdtemp(prefix="dpl-fake-"))

    def setUp(self):
        fakes.reset_fakes(self.root)
        self.addCleanup(fakes.kill_fakes)
        self.patch(config, "DISCORD_RESTART_PAUSE", 0.2)
        self.patch(discord, "open_with_proxy", fakes.open_fake_with_proxy)
        self.recorder = Recorder()
        self._sessions = []
        self.addCleanup(self._stop_sessions)

    def patch(self, owner, name, value):
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def _stop_sessions(self):
        for item in self._sessions:
            item.stop()
        for item in self._sessions:
            item.join(20)

    dedicated = None

    def start_session(self):
        self.notices = []
        item = session.PublicSession(
            dedicated=self.dedicated, on_notice=self.notices.append,
            on_status=self.recorder.on_status, on_finish=self.recorder.on_finish,
            find_install=lambda: fakes.fake_install(self.root),
        )
        self._sessions.append(item)
        item.start()
        self.item = item
        return item

    def relay_flag(self):
        """A flag do Discord: o proxy local da sessão, que repassa ao proxy público ativo."""
        self.assertTrue(fakes.wait_until(lambda: self.item._relay is not None, 10))
        return self.flag(("127.0.0.1", self.item._relay.port))

    def active(self):
        return self.item._relay.active.proxy if self.item._relay and self.item._relay.active else None

    def launches(self):
        return fakes.read_launches(self.root)

    def wait_launches(self, count):
        fakes.wait_until(lambda: len(self.launches()) >= count, 10)
        launches = self.launches()
        self.assertEqual(len(launches), count, launches)
        return launches

    @staticmethod
    def flag(proxy):
        return f"--proxy-server=socks5://{proxy[0]}:{proxy[1]}"

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


class ScriptedCase(PublicCase):
    """Internet roteirizada (World) no lugar das listas e dos proxies de verdade."""

    def setUp(self):
        super().setUp()
        self.world = World()
        self.online = True
        self.patch(proxies, "download_lists", self.world.download_lists)
        self.patch(proxies, "discord_check", self.world.check)
        self.patch(socks5, "probe", self.world.probe)
        self.patch(socks5, "direct_internet", lambda *args, **kwargs: self.online)
        self.patch(session, "_REFILL_RETRY", 0.5)
        self.patch(session.Session, "_backend_reachable", lambda _self: False)
        self.patch(config, "HEALTH_INTERVAL", 1.0)
        self.patch(config, "TEST_ENOUGH_APPROVED", None)        # lista inteira: escolha determinística


class ScriptedPublicSessionTest(ScriptedCase):
    def test_uses_the_fastest_and_fails_over_in_latency_order(self):
        first = [address(index) for index in range(1, 31)]
        second = [address(index, block=18) for index in range(1, 6)]
        slow, fast, medium, late = first[2], first[6], first[10], second[3]
        self.world.alive = {slow: 300.0, fast: 100.0, medium: 200.0, late: 80.0}
        self.world.lists = [("Fonte A", first), ("Fonte B", second)]

        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {fast[0]}:1080 (50ms)")
        self.assertEqual(self.recorder.events[0], (session.BLUE, "Conectando..."))
        self.assertEqual(self.recorder.events[mark - 2], (session.YELLOW, "Testando proxies... (30/30) — 3 aprovados"))
        launch = self.wait_launches(1)[0]
        self.assertEqual(launch["args"], self.relay_flag())       # o Discord fala com o proxy local...
        self.assertEqual(self.active(), fast)                     # ...que sai pelo mais rápido
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo", start=mark)      # monitor rodando

        del self.world.alive[fast]                    # o proxy ativo cai
        mark = self.recorder.wait_for(session.ORANGE, "Instabilidade detectada — trocando proxy...", start=mark)
        mark = self.recorder.wait_for(session.GREEN, f"Reconectado via {medium[0]}:1080", start=mark)
        self.assertEqual(self.active(), medium)
        self.assertEqual(self.wait_launches(1)[0]["pid"], launch["pid"])               # trocou sem reabrir o Discord
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [launch["pid"]])
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {medium[0]}:1080 (100ms)", start=mark)

        del self.world.alive[medium]
        mark = self.recorder.wait_for(session.GREEN, f"Reconectado via {slow[0]}:1080", start=mark)

        # A fila de reserva acabou: baixa lista de novo; a Fonte A não tem nada novo, a B tem.
        del self.world.alive[slow]
        downloads = self.world.downloads
        mark = self.recorder.wait_for(session.GREEN, f"Reconectado via {late[0]}:1080", start=mark)
        self.assertGreater(self.world.downloads, downloads)
        self.assertEqual(self.active(), late)
        self.assertEqual(len(self.launches()), 1)
        texts = [text for color, text in self.recorder.events if color == session.ORANGE]
        self.assertIn("Instabilidade — trocando proxy... (5/5) — 1 aprovado", texts)

        # Não sobrou nenhum proxy em lugar nenhum.
        del self.world.alive[late]
        self.assertTrue(self.recorder.finished.wait(30))
        self.assertEqual(
            self.recorder.events[-1],
            (session.RED, "Erro: nenhum proxy público funcionando no momento — tente mais tarde — Discord fechado"),
        )
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [])      # nada de Discord preso a um proxy morto

    def test_failover_respects_a_discord_the_user_closed(self):
        best, reserve = address(1), address(2)
        self.world.alive = {best: 100.0, reserve: 200.0}
        self.world.lists = [("Fonte A", [best, reserve])]
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {best[0]}")
        self.wait_launches(1)
        fakes.kill_fakes()                              # o usuário fechou o Discord
        del self.world.alive[best]
        mark = self.recorder.wait_for(session.GREEN, f"Reconectado via {reserve[0]}:1080", start=mark)
        time.sleep(1.0)
        self.assertEqual(len(self.launches()), 1)       # a troca de proxy não o reabriu
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [])
        # Quando ele reabrir o Discord pelo atalho (sem proxy), a vigia o traz para o proxy local.
        install = fakes.fake_install(self.root)
        win.spawn_detached(install.exe_path, fakes.fake_arguments(install))
        self.recorder.wait_for(session.ORANGE, "O Discord reiniciou sem o proxy", start=mark)
        self.wait_proxied_launches([self.relay_flag(), self.relay_flag()])
        self.assertEqual(self.active(), reserve)

    def test_local_outage_while_testing_does_not_burn_the_list(self):
        everyone = [address(index) for index in range(1, 201)]
        winner = everyone[150]
        self.world.alive = {winner: 100.0}
        self.world.check_delay = 0.05
        self.world.lists = [("Fonte A", everyone)]
        self.patch(config, "TEST_ENOUGH_APPROVED", 1)
        self.start_session()
        mark = self.recorder.wait_for(session.YELLOW, "Testando proxies")
        self.online = False                             # o Wi-Fi caiu no meio do teste
        self.world.offline = True
        mark = self.recorder.wait_for(session.ORANGE, "Sem internet — aguardando a conexão voltar...", start=mark)
        time.sleep(0.5)                                 # os testes que já estavam em andamento terminam
        tested = len(self.world.checked)
        time.sleep(2.5)
        self.assertEqual(len(self.world.checked), tested)            # com a rede fora, ninguém mais é testado
        self.assertFalse(self.recorder.finished.is_set())
        self.world.offline = False
        self.online = True
        # A lista não foi queimada: o proxy bom, lá no meio dela, ainda é encontrado.
        self.recorder.wait_for(session.GREEN, f"Proxy ativo — {winner[0]}", start=mark, timeout=60)
        # Só quem estava no meio do teste quando a rede caiu é testado de novo.
        repeated = len(self.world.checked) - len(set(self.world.checked))
        self.assertLessEqual(repeated, config.TEST_CONCURRENCY)

    def test_no_way_to_download_mid_session_waits_for_the_network(self):
        first, later = address(1), address(2, block=18)
        self.world.alive = {first: 100.0}
        self.world.lists = [("Fonte A", [first])]
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {first[0]}")
        # O proxy cai e, na hora de buscar outro, nenhuma fonte responde (rede fora do ar).
        self.world.lists = []
        del self.world.alive[first]
        mark = self.recorder.wait_for(session.ORANGE, "Sem internet — aguardando a conexão voltar...", start=mark)
        time.sleep(1.5)
        self.assertFalse(self.recorder.finished.is_set())            # a sessão espera, não desiste
        self.world.alive = {later: 80.0}
        self.world.lists = [("Fonte B", [later])]
        self.recorder.wait_for(session.GREEN, f"Reconectado via {later[0]}:1080", start=mark, timeout=30)

    def test_discord_still_reachable_keeps_the_proxy(self):
        # O destino da verificação (1.1.1.1:80) falha, mas o Discord responde pelo proxy: nada de troca.
        only = address(1)
        self.world.alive = {only: 100.0}
        self.world.lists = [("Fonte A", [only, address(2)])]
        self.patch(socks5, "probe", self.failing_probe)
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {only[0]}")
        for _ in range(3):
            mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {only[0]}", start=mark)
        self.assertNotIn(session.ORANGE, self.recorder.colors())
        self.wait_launches(1)

    @staticmethod
    def failing_probe(proxy, target, timeout):
        raise TimeoutError("1.1.1.1:80 fora do ar")

    def test_one_failed_check_does_not_switch(self):
        only = address(1)
        self.world.alive = {only: 100.0}
        self.world.lists = [("Fonte A", [only, address(2)])]
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        probes = len(self.world.probed)
        del self.world.alive[only]
        self.assertTrue(fakes.wait_until(lambda: len(self.world.probed) > probes, 10))   # uma falha...
        self.world.alive[only] = 100.0
        self.recorder.wait_for(session.GREEN, "Proxy ativo", start=mark)                 # ...e volta ao normal
        self.assertNotIn(session.ORANGE, self.recorder.colors())
        self.wait_launches(1)

    def test_dead_standby_is_skipped_without_restarting_discord_for_it(self):
        everyone = [address(index) for index in range(1, 11)]
        best, second, third = everyone[0], everyone[4], everyone[8]
        self.world.alive = {best: 100.0, second: 200.0, third: 300.0}
        self.world.lists = [("Fonte A", everyone)]
        self.patch(config, "STANDBY_FRESH_FOR", 0.0)            # toda reserva é revalidada antes do uso
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {best[0]}")
        del self.world.alive[best]
        del self.world.alive[second]                            # a reserva seguinte morreu também
        self.recorder.wait_for(session.GREEN, f"Reconectado via {third[0]}:1080", start=mark)
        self.assertEqual(self.active(), third)
        self.assertEqual([entry["args"] for entry in self.wait_launches(1)], [self.relay_flag()])

    def test_progress_text_and_early_stop(self):
        everyone = [address(index % 250 + 1, block=51 + index // 250) for index in range(400)]
        self.world.alive = {proxy: 100.0 + index for index, proxy in enumerate(everyone) if index % 2 == 0}
        self.world.lists = [("Fonte A", everyone)]
        self.patch(config, "TEST_ENOUGH_APPROVED", 6)
        self.start_session()
        self.recorder.wait_for(session.GREEN, "Proxy ativo")
        yellow = [text for color, text in self.recorder.events if color == session.YELLOW]
        self.assertTrue(yellow)
        for text in yellow:
            self.assertRegex(text, r"^Testando proxies\.\.\. \(\d+/400\) — \d+ aprovados?$")
        self.assertTrue(any(text.endswith("— 1 aprovado") for text in yellow))
        self.assertLess(len(self.world.checked), 60)            # parou cedo; o resto fica para depois
        self.assertRegex(self.wait_launches(1)[0]["args"], r"^--proxy-server=socks5://127\.0\.0\.1:\d+$")
        self.assertRegex(f"{self.active()[0]}:{self.active()[1]}", r"^198\.\d+\.100\.\d+:1080$")

    def test_no_list_available(self):
        self.world.lists = []
        self.start_session()
        self.assertTrue(self.recorder.finished.wait(10))
        self.assertEqual(
            self.recorder.events[-1],
            (session.RED, "Erro: não foi possível baixar a lista de proxies (sem internet?)"),
        )

    def test_no_working_proxy_leaves_discord_untouched(self):
        install = fakes.fake_install(self.root)
        old_pid = win.spawn_detached(install.exe_path, fakes.fake_arguments(install))
        self.assertTrue(fakes.wait_until(lambda: old_pid in win.pids_by_name(fakes.FAKE_EXE)))
        self.world.lists = [("Fonte A", [address(index) for index in range(1, 40)])]
        self.start_session()
        self.assertTrue(self.recorder.finished.wait(20))
        self.assertEqual(
            self.recorder.events[-1],
            (session.RED, "Erro: nenhum proxy público funcionando no momento — tente mais tarde"),
        )
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [old_pid])

    def test_stop_while_testing_is_quick(self):
        self.world.check_delay = 1.0
        self.world.lists = [("Fonte A", [address(index % 250 + 1, block=51 + index // 250) for index in range(600)])]
        item = self.start_session()
        self.recorder.wait_for(session.YELLOW, "Testando proxies")
        started = time.monotonic()
        item.stop()
        self.assertTrue(self.recorder.finished.wait(10))
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.recorder.events[-1], (session.WHITE, "Desconectado"))
        self.assertEqual(self.launches(), [])

    def test_stop_closes_the_discord_it_opened(self):
        only = address(1)
        self.world.alive = {only: 100.0}
        self.world.lists = [("Fonte A", [only])]
        item = self.start_session()
        self.recorder.wait_for(session.GREEN, "Proxy ativo")
        self.wait_launches(1)
        item.stop()
        self.assertTrue(self.recorder.finished.wait(10))
        self.assertEqual(self.recorder.events[-1], (session.WHITE, "Desconectado"))
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [])

    def test_local_outage_does_not_burn_the_queue(self):
        best, reserve = address(1), address(2)
        self.world.alive = {best: 100.0, reserve: 200.0}
        self.world.lists = [("Fonte A", [best, reserve])]
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {best[0]}")
        saved = dict(self.world.alive)
        self.online = False                         # a internet do usuário caiu: nada responde
        self.world.alive = {}
        mark = self.recorder.wait_for(session.ORANGE, "Sem internet — aguardando a conexão voltar...", start=mark)
        time.sleep(2)
        self.world.alive = saved
        self.online = True
        self.recorder.wait_for(session.GREEN, f"Proxy ativo — {best[0]}", start=mark, timeout=30)
        self.wait_launches(1)                       # mesmo proxy, Discord intocado
        self.assertNotIn("trocando proxy", " ".join(text for _, text in self.recorder.events))

    def test_discord_restarting_without_the_proxy_is_reopened(self):
        only = address(1)
        self.world.alive = {only: 100.0}
        self.world.lists = [("Fonte A", [only])]
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo")
        first = self.wait_launches(1)[0]["pid"]
        install = fakes.fake_install(self.root)
        win.kill_pid(first)
        win.spawn_detached(install.exe_path, fakes.fake_arguments(install))
        self.recorder.wait_for(session.ORANGE, "O Discord reiniciou sem o proxy", start=mark)
        self.wait_proxied_launches([self.relay_flag(), self.relay_flag()])

    def test_proxy_losing_in_discords_own_connections_is_replaced_without_restarting_discord(self):
        # O 1.1.1.1:80 responde pelo proxy ativo, mas as conexões do Discord por ele
        # falham e saem pela reserva: o relay avisa e a sessão troca o ativo.
        best, reserve = address(1), address(2)
        self.world.alive = {best: 100.0, reserve: 200.0}
        self.world.lists = [("Fonte A", [best, reserve])]
        self.patch(config, "HEALTH_INTERVAL", 60.0)             # a troca vem do relay, não da verificação
        echo_left, echo_right = socket.socketpair()
        self.addCleanup(echo_left.close)
        self.addCleanup(echo_right.close)
        pairs = []

        def fake_connect(proxy, host, port, *, deadline):
            if tuple(proxy) == best:
                raise socks5.ProxyError("falha geral", 1)
            left, right = socket.socketpair()
            pairs.append((left, right))
            self.addCleanup(left.close)
            self.addCleanup(right.close)
            return left

        self.patch(socks5, "connect", fake_connect)
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {best[0]}")
        launch = self.wait_launches(1)[0]
        for _ in range(config.RELAY_MAX_STRIKES):               # o "Discord" abre conexões pelo relay
            _real_connect(("127.0.0.1", self.item._relay.port), "discord.com", 443,
                          deadline=time.monotonic() + 10).close()
        self.assertEqual(len(pairs), config.RELAY_MAX_STRIKES)  # todas saíram pela reserva
        mark = self.recorder.wait_for(session.ORANGE, "Instabilidade detectada — trocando proxy...", start=mark)
        self.recorder.wait_for(session.GREEN, f"Reconectado via {reserve[0]}:1080", start=mark)
        self.assertEqual(self.active(), reserve)
        self.assertEqual(self.wait_launches(1)[0]["pid"], launch["pid"])
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [launch["pid"]])


class VpsWorld:
    """O servidor dedicado de mentira: no ar, fora do ar ou recusando esta versão do app."""

    def __init__(self):
        self.up = True
        self.rejected = False
        self.checks = 0
        self.healths = 0

    def _state(self):
        if self.rejected:
            raise dconnect.TokenRejected("versão aposentada", 410, "token_aposentado")
        if not self.up:
            raise dconnect.ServerUnavailable("fora do ar")

    def check(self, server, timeout=None):
        self.checks += 1
        self._state()
        return dconnect.Check(40.0, "FR")

    def health(self, server, timeout=None):
        self.healths += 1
        self._state()
        return dconnect.Health(20.0, "ok", "FR")


class DedicatedServerSessionTest(ScriptedCase):
    dedicated = dconnect.DedicatedServer("wss://dconnect.example.test/t/abcdef012345", "t" * 43)
    VPS_TEXT = "servidor dedicado de pedrostyxx · França"

    def setUp(self):
        super().setUp()
        self.vps = VpsWorld()
        self.patch(dconnect, "check", self.vps.check)
        self.patch(dconnect, "health", self.vps.health)
        self.patch(config, "VPS_RETRY_INITIAL", 1.0)
        self.patch(config, "VPS_RETURN_CONFIRM", 0.5)
        self.patch(config, "VPS_GRACE_DELAYS", (0.1, 0.1, 0.1))
        self.reserve = address(7)
        self.world.alive = {self.reserve: 100.0}
        self.world.lists = [("Fonte A", [self.reserve, address(8)])]

    def test_uses_the_dedicated_server_and_leaves_the_lists_alone(self):
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {self.VPS_TEXT} (40ms)")
        self.assertEqual(self.recorder.events[1], (session.BLUE, "Conectando ao servidor dedicado..."))
        self.assertEqual(self.wait_launches(1)[0]["args"], self.relay_flag())
        self.assertTrue(self.item._relay.active.dedicated)
        self.assertEqual(self.notices, ["vps"])
        self.recorder.wait_for(session.GREEN, f"Proxy ativo — {self.VPS_TEXT} (20ms)", start=mark)   # health
        self.assertEqual(self.world.downloads, 0)            # as listas ficam só de reserva

    def test_down_at_start_then_comes_back_without_reopening_discord(self):
        self.vps.up = False
        self.start_session()
        mark = self.recorder.wait_for(session.YELLOW, "Servidor dedicado indisponível — usando proxies públicos...")
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {self.reserve[0]}:1080", start=mark)
        launch = self.wait_launches(1)[0]
        self.assertEqual(self.notices, [])
        checks = self.vps.checks
        self.vps.up = True
        self.recorder.wait_for(session.GREEN, f"Reconectado via {self.VPS_TEXT}", start=mark, timeout=20)
        self.assertGreaterEqual(self.vps.checks - checks, 2)          # confirmou antes de voltar
        self.assertTrue(self.item._relay.active.dedicated)
        self.assertEqual(self.notices, ["vps"])
        self.assertEqual([entry["pid"] for entry in self.launches()], [launch["pid"]])

    def test_rejected_version_is_final_for_the_session(self):
        self.vps.rejected = True
        self.start_session()
        mark = self.recorder.wait_for(
            session.YELLOW, "O servidor dedicado não aceita esta versão do app — usando proxies públicos...")
        self.recorder.wait_for(session.GREEN, f"Proxy ativo — {self.reserve[0]}:1080", start=mark)
        self.assertEqual(self.notices, ["vps_update"])
        time.sleep(3)                                       # bem mais que o tempo de nova tentativa
        self.assertEqual(self.vps.checks, 1)

    def test_dies_mid_session_and_falls_back_to_the_lists(self):
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {self.VPS_TEXT}")
        launch = self.wait_launches(1)[0]
        self.vps.up = False
        mark = self.recorder.wait_for(session.ORANGE, "Servidor dedicado instável — tentando reconectar...", start=mark)
        mark = self.recorder.wait_for(
            session.ORANGE, "Servidor dedicado instável — trocando para proxies públicos...", start=mark)
        self.recorder.wait_for(session.GREEN, f"Reconectado via {self.reserve[0]}:1080", start=mark)
        self.assertFalse(self.item._relay.active.dedicated)
        self.assertEqual(self.notices, ["vps", None])
        self.assertEqual([entry["pid"] for entry in self.launches()], [launch["pid"]])

    def test_a_quick_restart_of_the_server_is_absorbed(self):
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {self.VPS_TEXT}")
        healths = self.vps.healths
        self.vps.up = False
        fakes.wait_until(lambda: self.vps.healths >= healths + 2, 10)   # duas verificações falharam
        self.vps.up = True                                              # voltou durante a tolerância
        self.recorder.wait_for(session.ORANGE, "Servidor dedicado instável — tentando reconectar...", start=mark)
        self.recorder.wait_for(session.GREEN, f"Proxy ativo — {self.VPS_TEXT}", start=mark + 1)
        self.assertNotIn("trocando para proxies públicos", " ".join(text for _, text in self.recorder.events))
        self.assertEqual(self.world.downloads, 0)

    def test_rejection_mid_session_shows_the_update_notice_at_once(self):
        item = self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {self.VPS_TEXT}")
        self.vps.rejected = True                            # o dono trocou o token: esta versão ficou velha
        mark = self.recorder.wait_for(
            session.ORANGE, "O servidor dedicado não aceita mais esta versão do app — usando proxies públicos...",
            start=mark)
        self.assertEqual(self.notices, ["vps", "vps_update"])           # sem ficar com o painel verde
        self.recorder.wait_for(session.GREEN, f"Reconectado via {self.reserve[0]}:1080", start=mark)
        checks = self.vps.checks
        time.sleep(2.5)
        self.assertEqual(self.vps.checks, checks)                       # não insiste
        item.stop()
        self.assertTrue(self.recorder.finished.wait(10))
        self.assertEqual(self.notices, ["vps", "vps_update"])           # o aviso continua depois do fim

    def test_a_server_that_refuses_everything_is_left_at_once(self):
        self.start_session()
        mark = self.recorder.wait_for(session.GREEN, f"Proxy ativo — {self.VPS_TEXT}")
        launch = self.wait_launches(1)[0]
        with self.item._relay._lock:
            self.item._relay._vps_refusing = True       # o relay viu o servidor recusar tudo (cota, limites)
        mark = self.recorder.wait_for(
            session.ORANGE, "Servidor dedicado instável — trocando para proxies públicos...", start=mark)
        self.recorder.wait_for(session.GREEN, f"Reconectado via {self.reserve[0]}:1080", start=mark)
        texts = " ".join(text for _, text in self.recorder.events)
        self.assertNotIn("tentando reconectar", texts)          # sem a tolerância: o /health diria que está tudo bem
        self.assertEqual([entry["pid"] for entry in self.launches()], [launch["pid"]])

    def test_stop_clears_the_notice(self):
        item = self.start_session()
        self.recorder.wait_for(session.GREEN, f"Proxy ativo — {self.VPS_TEXT}")
        item.stop()
        self.assertTrue(self.recorder.finished.wait(10))
        self.assertEqual(self.notices, ["vps", None])


@unittest.skipUnless(os.environ.get("DPL_NETWORK_TESTS") == "1", "defina DPL_NETWORK_TESTS=1 para usar a rede de verdade")
class RealNetworkTest(PublicCase):
    def test_real_lists_and_real_proxies(self):
        self.patch(config, "HEALTH_INTERVAL", 8.0)
        started = time.monotonic()
        item = self.start_session()
        mark = self.recorder.wait_for(session.GREEN, "Proxy ativo — ", timeout=300)
        print(f"\n  primeiro proxy em {time.monotonic() - started:.0f}s: {self.recorder.events[mark - 1][1]}")
        launch = self.wait_launches(1)[0]
        self.assertEqual(launch["args"], self.relay_flag())
        self.assertIn(self.item._relay.active.address, self.recorder.events[mark - 1][1])
        # Deixa o monitor de verdade rodar algumas rondas contra 1.1.1.1:80 pelo proxy.
        time.sleep(40)
        for color, text in self.recorder.events[mark:]:
            print(f"  [{color}] {text}")
        self.assertFalse(self.recorder.finished.is_set(), self.recorder.events[-1])
        item.stop()
        self.assertTrue(self.recorder.finished.wait(20))
        self.assertEqual(self.recorder.events[-1], (session.WHITE, "Desconectado"))
        self.assertEqual(win.pids_by_name(fakes.FAKE_EXE), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
