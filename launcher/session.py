"""Sessões de conexão: fluxo de início, monitor de saúde e troca automática.

Cada sessão roda em uma thread própria e fala com a interface só por dois
callbacks: on_status(cor, texto) e on_finish().
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Callable

from . import config, discord, proxies, socks5, tunnel, win
from .log import log

# Cores da linha de status
BLUE, YELLOW, GREEN, ORANGE, RED, WHITE = "blue", "yellow", "green", "orange", "red", "white"

_OFFLINE_TEXT = "Sem internet — aguardando a conexão voltar..."
_REOPEN_LIMIT = 3               # reaberturas do Discord toleradas...
_REOPEN_WINDOW = 600.0          # ...dentro desta janela (segundos)
_DIRECT_RECHECK = 600.0         # intervalo para reavaliar um teste de internet direta descartado
_REFILL_RETRY = 15.0            # espera entre tentativas de baixar a lista com a rede fora do ar


class SessionError(Exception):
    """Erro que encerra a sessão; a mensagem vai para a linha de status."""


class _Stopped(Exception):
    """O usuário clicou em Parar."""


class Session(threading.Thread):
    """Esqueleto comum aos dois modos: iniciar, monitorar e encerrar."""

    def __init__(
        self,
        on_status: Callable[[str, str], None],
        on_finish: Callable[[], None],
        find_install: Callable[[], discord.DiscordInstall | None] = discord.find_install,
    ):
        super().__init__(name="session", daemon=True)
        self._on_status = on_status
        self._on_finish = on_finish
        self._find_install = find_install
        self._stop_event = threading.Event()
        self._install: discord.DiscordInstall | None = None
        self._discord_pid: int | None = None
        self._discord_lock = threading.Lock()
        self._last_latency = 0.0
        self._reopen_times: list[float] = []
        self._direct_internet_seen = False
        self._direct_rechecked_at = time.monotonic()
        self._offline_lock = threading.Lock()
        self._offline_checked_at = float("-inf")
        self._offline_state = False

    def stop(self) -> None:
        self._stop_event.set()

    def abandon(self) -> None:
        """Último recurso da interface: o app vai fechar e esta thread ainda está
        presa em uma operação de rede. Encerra daqui mesmo o túnel e o Discord."""
        self._stop_event.set()
        for step in (self._cleanup, self._close_own_discord):
            try:
                step()
            except Exception:
                log.exception("falha ao abandonar a sessão")

    # -- pontos que cada modo implementa --

    def _connect(self) -> None:
        raise NotImplementedError

    def _recover(self) -> None:
        raise NotImplementedError

    def _active_proxy(self) -> tuple[str, int]:
        raise NotImplementedError

    def _active_text(self, latency_ms: float) -> str:
        raise NotImplementedError

    def _backend_alive(self) -> bool:
        return True

    def _backend_address(self) -> tuple[str, int] | None:
        """Endereço na internet do qual a sessão depende (servidor SSH ou proxy)."""
        return None

    def _cleanup(self) -> None:
        pass

    # -- execução --

    def run(self) -> None:
        error = None
        try:
            self._install = self._find_install()
            if self._install is None:
                raise SessionError("Discord não encontrado neste computador")
            # Se a internet direta responde agora, uma falha dela mais tarde
            # indica queda da rede local, e não do proxy.
            self._direct_internet_seen = socks5.direct_internet()
            self._connect()
            self._monitor()
        except _Stopped:
            pass
        except SessionError as exc:
            error = str(exc)
        except Exception as exc:
            log.exception("falha inesperada na sessão")
            error = f"falha inesperada ({type(exc).__name__})"

        stopped = self._stop_event.is_set()
        closed_discord = False
        try:
            self._cleanup()
            # Sem túnel e sem monitor, o Discord aberto pelo launcher ficaria preso
            # a um proxy que não existe mais (ou que ninguém mantém); e reabri-lo
            # sem proxy exporia a conexão direta sem o usuário pedir.
            closed_discord = self._close_own_discord()
        except Exception:
            log.exception("falha ao encerrar a sessão")
        if error and not stopped:
            self._status(RED, f"Erro: {error}" + (" — Discord fechado" if closed_discord else ""))
        else:
            self._status(WHITE, "Desconectado")
        self._on_finish()

    def _status(self, color: str, text: str) -> None:
        log.info("status [%s] %s", color, text)
        self._on_status(color, text)

    def _show_active(self, latency_ms: float) -> None:
        self._status(GREEN, self._active_text(latency_ms))

    def _check_stop(self) -> None:
        if self._stop_event.is_set():
            raise _Stopped()

    def _sleep(self, seconds: float) -> None:
        if self._stop_event.wait(seconds):
            raise _Stopped()

    # -- Discord --

    def _close_own_discord(self) -> bool:
        """Fecha o Discord se foi esta sessão que o abriu. True se fechou algo."""
        # O lock faz quem chega depois (abandon() vindo da interface, por exemplo)
        # esperar o fechamento terminar em vez de sair achando que já foi feito.
        with self._discord_lock:
            if self._discord_pid is None or self._install is None:
                return False
            closed = discord.close(self._install) > 0
            self._discord_pid = None
            return closed

    def _restart_discord(self, proxy_host: str, proxy_port: int) -> None:
        """Fecha o Discord e reabre apontando para o proxy."""
        assert self._install is not None
        with self._discord_lock:
            self._check_stop()
            if self._discord_pid is not None and not discord.is_running(self._install):
                # O usuário fechou o Discord: ele continua fechado. Quando for
                # reaberto, a vigia (_watch_discord) o traz para o proxy atual.
                log.info("Discord fechado pelo usuário; não será reaberto agora")
                return
            try:
                closed = discord.close(self._install)
            except OSError as exc:
                raise SessionError("não foi possível fechar o Discord (aberto como administrador?)") from exc
            if closed:
                self._sleep(config.DISCORD_RESTART_PAUSE)
            # Uma atualização do Discord cria uma pasta app-X nova; usa sempre a mais recente.
            self._install = self._find_install() or self._install
            try:
                self._discord_pid = discord.open_with_proxy(self._install, proxy_host, proxy_port)
            except OSError as exc:
                raise SessionError(f"não foi possível abrir o Discord ({exc.strerror or exc})") from exc

    def _classify_discord(self, pids: list[int]) -> tuple[int | None, bool]:
        """(PID do processo principal que já usa o nosso proxy, há um principal sem ele?).

        Processos auxiliares do Discord (GPU, renderer, o "relauncher" que ele usa
        para se reiniciar...) têm `--type=` na linha de comando e não contam. Um
        processo cuja linha de comando não dá para ler conta, na dúvida, como
        principal sem o proxy.
        """
        flag = discord.proxy_argument(*self._active_proxy())
        proxied, unproxied = None, False
        for pid in pids:
            command_line = win.process_command_line(pid)
            if not command_line:
                unproxied = True
            elif "--type=" in command_line:
                continue
            elif flag in command_line.split():
                proxied = proxied or pid
            else:
                unproxied = True
        return proxied, unproxied

    def _watch_discord(self) -> None:
        """Reabre o Discord com o proxy se ele reiniciou por conta própria.

        Ao se atualizar (ou ser reaberto pelo atalho), o Discord volta sem o
        `--proxy-server` e passaria a conectar direto, sem o launcher perceber.
        Se o usuário fechou o Discord de vez, nada é feito.
        """
        if self._discord_pid is None or self._install is None:
            return
        running = win.pids_by_name(self._install.exe_name)
        if not running or self._discord_pid in running:
            return
        proxied, unproxied = self._classify_discord(running)
        if proxied is not None:
            # O próprio Discord se reiniciou mantendo os argumentos (acontece ao
            # aplicar atualizações): ele continua no proxy, basta acompanhá-lo.
            log.info("o Discord reiniciou com o proxy (pid %d); nada a fazer", proxied)
            self._discord_pid = proxied
            return
        if not unproxied:
            # Só processos auxiliares: o Discord está no meio de um reinício (o
            # principal novo ainda vai aparecer) ou acabou de ser fechado. Espera.
            return
        now = time.monotonic()
        self._reopen_times = [moment for moment in self._reopen_times if now - moment < _REOPEN_WINDOW]
        if len(self._reopen_times) >= _REOPEN_LIMIT:
            # Algo reinicia o Discord em laço. Melhor encerrar com um erro claro
            # do que mostrar "Proxy ativo" com o Discord conectando por fora.
            raise SessionError("o Discord insiste em reiniciar sem o proxy")
        self._reopen_times.append(now)
        self._status(ORANGE, "O Discord reiniciou sem o proxy — reabrindo...")
        self._restart_discord(*self._active_proxy())
        self._show_active(self._last_latency)

    # -- monitor de saúde --

    def _health(self) -> float:
        """Uma verificação de saúde pelo proxy ativo; devolve a latência em ms.

        Primeiro a da especificação (TCP até 1.1.1.1:80). Se ela falhar, vale o
        que realmente importa: o Discord responde por este proxy? Assim um proxy
        (ou VPS) que não alcança aquele endereço — ou uma queda do próprio
        1.1.1.1 — não derruba uma conexão que está funcionando.
        """
        proxy = self._active_proxy()
        try:
            latency = socks5.probe(proxy, config.HEALTH_TARGET, config.HEALTH_TIMEOUT)
        except OSError as probe_error:
            try:
                latency = proxies.discord_check(proxy, config.HEALTH_TIMEOUT).total_ms
            except OSError:
                raise probe_error from None
        self._last_latency = latency
        return latency

    def _monitor(self) -> None:
        failures = 0
        ticks = 0
        next_check = time.monotonic() + config.HEALTH_INTERVAL
        while True:
            self._sleep(1.0)
            ticks += 1
            if ticks % 3 == 0:
                self._watch_discord()
            if not self._backend_alive():
                # O túnel caiu de vez: não há por que esperar duas verificações.
                log.info("o túnel não está mais ativo")
                self._handle_outage()
                failures = 0
                next_check = time.monotonic() + config.HEALTH_INTERVAL
                continue
            if time.monotonic() < next_check:
                continue
            try:
                latency = self._health()
            except OSError as exc:
                failures += 1
                log.info("verificação falhou (%d/%d): %s", failures, config.HEALTH_MAX_FAILURES, exc)
                if failures >= config.HEALTH_MAX_FAILURES:
                    self._handle_outage()
                    failures = 0
            else:
                failures = 0
                self._show_active(latency)
                self._recheck_direct_internet()
            next_check = time.monotonic() + config.HEALTH_INTERVAL

    # -- queda da rede local --

    def _offline(self, since: float | None = None) -> bool:
        """True se a internet do próprio computador caiu (e não só o proxy).

        Só vale quando a internet direta respondia no início da sessão; em uma
        rede que bloqueia esse teste ele não diz nada, e a resposta é sempre False.

        `since` é o instante (time.perf_counter) do evento que levantou a dúvida.
        O teste precisa ter começado depois dele — um resultado mais antigo
        diria "tudo bem" sobre uma queda que acabou de acontecer —, mas um mesmo
        teste serve a todas as threads que perguntarem sobre falhas anteriores a ele.
        """
        if not self._direct_internet_seen:
            return False
        if since is None:
            since = time.perf_counter()
        with self._offline_lock:
            if self._offline_checked_at < since:
                started = time.perf_counter()
                self._offline_state = not socks5.direct_internet()
                self._offline_checked_at = started
            return self._offline_state

    def _recheck_direct_internet(self) -> None:
        """Um teste de internet direta descartado (a rede o bloqueava) volta a
        ser considerado se, mais tarde, ele passar a responder."""
        if self._direct_internet_seen:
            return
        now = time.monotonic()
        if now - self._direct_rechecked_at >= _DIRECT_RECHECK:
            self._direct_rechecked_at = now
            self._direct_internet_seen = socks5.direct_internet()

    def _backend_reachable(self) -> bool:
        address = self._backend_address()
        if address is None:
            return False
        try:
            socket.create_connection(address, timeout=3.0).close()
            return True
        except OSError:
            return False

    def _wait_for_internet(self) -> None:
        """Espera a rede local voltar, pelo tempo que for (até clicarem em Parar)."""
        self._status(ORANGE, _OFFLINE_TEXT)
        while self._offline():
            self._sleep(5.0)
            if self._backend_reachable() and self._offline():
                # O servidor/proxy responde e, ainda assim, um teste direto feito
                # agora falha: é o teste que não funciona nesta rede (o computador
                # mudou de rede), não a internet. Deixa de usá-lo.
                self._direct_internet_seen = False

    def _handle_outage(self) -> None:
        """Decide entre esperar a internet voltar e trocar/reconectar o proxy.

        Se a queda é da rede do próprio usuário (Wi-Fi caiu, notebook voltou da
        suspensão), trocar de proxy não resolve nada e só gastaria a fila de
        reserva — então o app espera a conexão voltar antes de agir.
        """
        if self._offline():
            self._wait_for_internet()
            if self._backend_alive():
                try:
                    latency = self._health()
                except OSError:
                    pass
                else:
                    self._show_active(latency)
                    return
        self._recover()


class SshSession(Session):
    """Modo A — Meu servidor: túnel SSH próprio + Discord em 127.0.0.1."""

    def __init__(self, host: str, port: int, user: str, password: str | None, **kwargs):
        super().__init__(**kwargs)
        self._host, self._port, self._user = host, port, user
        self._password = password or None
        self._tunnel = None

    def _connect(self) -> None:
        self._status(BLUE, "Conectando...")
        self._tunnel = self._open_tunnel(tunnel.pick_local_port())
        self._check_stop()
        latency = self._verify_tunnel()
        # Só agora, com o túnel novo funcionando, o `ssh` de um túnel antigo que
        # ainda ocupe a porta padrão é encerrado; e só então o Discord reinicia.
        tunnel.close_stale_ssh(self._tunnel.local_port)
        self._restart_discord(config.LOCAL_SOCKS_HOST, self._tunnel.local_port)
        self._show_active(latency)

    def _open_tunnel(self, local_port: int):
        if self._password:
            candidates = [
                tunnel.ParamikoTunnel(self._host, self._port, self._user, self._password, local_port)
            ]
        else:
            candidates = []
            ssh_exe = tunnel.find_ssh_exe()
            # Elevado com a conta de outro usuário, o ssh.exe procuraria as chaves
            # no perfil errado; o cliente embutido lê as do usuário certo.
            if ssh_exe and not win.desktop_user_is_other_account():
                candidates.append(
                    tunnel.SshExeTunnel(ssh_exe, self._host, self._port, self._user, local_port)
                )
            candidates.append(
                tunnel.ParamikoTunnel(self._host, self._port, self._user, None, local_port)
            )
        error: Exception | None = None
        for candidate in candidates:
            try:
                candidate.start(self._stop_event)
                return candidate
            except tunnel.TunnelError as exc:
                self._check_stop()
                log.info("túnel via %s falhou: %s", type(candidate).__name__, exc)
                error = exc
                if exc.kind in ("network", "port", "hostkey"):
                    break               # rede, porta ou chave do servidor: outro cliente não muda nada
        raise SessionError(str(error))

    def _verify_tunnel(self) -> float:
        """Confirma que o túnel encaminha tráfego de verdade."""
        try:
            return self._health()
        except OSError as exc:
            time.sleep(0.2)             # o ssh.exe relata a recusa pelo stderr
            if self._tunnel.forwarding_refused():
                message = "o servidor SSH não permite encaminhamento (AllowTcpForwarding)"
            else:
                message = "o túnel abriu, mas o servidor não alcança a internet"
            raise SessionError(message) from exc

    def _recover(self) -> None:
        self._status(ORANGE, "Instabilidade detectada — reconectando o túnel SSH...")
        for attempt in range(config.SSH_RECONNECT_ATTEMPTS):
            delay = config.SSH_RECONNECT_DELAYS[min(attempt, len(config.SSH_RECONNECT_DELAYS) - 1)]
            if delay:
                self._sleep(delay)
            self._check_stop()
            self._tunnel.stop()
            try:
                self._tunnel.start(self._stop_event)
                self._check_stop()
                self._verify_tunnel()
            except (tunnel.TunnelError, SessionError) as exc:
                self._check_stop()
                log.info("reconexão %d falhou: %s", attempt + 1, exc)
                if isinstance(exc, tunnel.TunnelError) and exc.kind in ("hostkey", "auth"):
                    # Insistir não resolve, e o aviso (principalmente o de chave
                    # trocada) precisa chegar ao usuário com todas as letras.
                    raise SessionError(str(exc)) from exc
                continue
            # O endereço do proxy (127.0.0.1) não mudou: o Discord reconecta sozinho.
            self._status(GREEN, f"Reconectado via {self._tunnel.label}")
            return
        raise SessionError(
            f"o túnel SSH caiu e {config.SSH_RECONNECT_ATTEMPTS} tentativas de reconexão falharam"
        )

    def _active_proxy(self) -> tuple[str, int]:
        return (config.LOCAL_SOCKS_HOST, self._tunnel.local_port)

    def _active_text(self, latency_ms: float) -> str:
        return f"Proxy ativo — {self._tunnel.label} ({latency_ms:.0f}ms)"

    def _backend_alive(self) -> bool:
        return self._tunnel is not None and self._tunnel.alive()

    def _backend_address(self) -> tuple[str, int] | None:
        return (self._host, self._port)

    def _cleanup(self) -> None:
        if self._tunnel is not None:
            self._tunnel.stop()


class PublicSession(Session):
    """Modo B — Proxy público: lista gratuita, testada e trocada automaticamente."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._pool = proxies.ProxyPool()
        self._active: proxies.Approved | None = None

    def _connect(self) -> None:
        self._status(BLUE, "Conectando...")
        entry = self._next_proxy(YELLOW, "Testando proxies...")
        self._use(entry)
        self._show_active(entry.connect_ms)

    def _recover(self) -> None:
        self._status(ORANGE, "Instabilidade detectada — trocando proxy...")
        if self._active is not None:
            self._pool.mark_failed(self._active.proxy)
        entry = self._next_proxy(ORANGE, "Instabilidade — trocando proxy...")
        self._use(entry)
        self._status(GREEN, f"Reconectado via {entry.address}")

    def _use(self, entry: proxies.Approved) -> None:
        self._active = entry
        self._last_latency = entry.connect_ms
        self._restart_discord(*entry.proxy)

    def _next_proxy(self, color: str, prefix: str) -> proxies.Approved:
        """O melhor proxy disponível: primeiro a reserva, depois o resto da lista,
        depois uma lista nova."""

        def progress(tested: int, total: int, approved: int) -> None:
            plural = "" if approved == 1 else "s"
            self._status(color, f"{prefix} ({tested}/{total}) — {approved} aprovado{plural}")

        while True:
            self._check_stop()
            entry = self._pool.pop_best()
            if entry is not None:
                if time.monotonic() - entry.tested_at <= config.STANDBY_FRESH_FOR:
                    return entry
                # Proxy gratuito cai a toda hora: confirma que a reserva ainda
                # funciona antes de fechar o Discord por causa dela.
                try:
                    result = proxies.discord_check(entry.proxy)
                except OSError:
                    if self._offline():
                        self._pool.put_back(entry)      # quem caiu foi a rede local, não o proxy
                        self._wait_for_internet()
                    else:
                        self._pool.mark_failed(entry.proxy)
                    continue
                return proxies.Approved(result.total_ms, entry.proxy, result.connect_ms, time.monotonic())
            if not self._pool.has_untested():
                self._refill()
            self._pool.test_until(
                config.TEST_ENOUGH_APPROVED, self._stop_event, progress,
                is_offline=self._offline,
                on_offline=lambda: self._status(ORANGE, _OFFLINE_TEXT),
            )

    def _refill(self) -> None:
        """Carrega uma lista nova na fila; espera se não houver como baixar."""
        while True:
            downloaded = False
            for name, found in proxies.download_lists(self._stop_event):
                downloaded = True
                loaded = self._pool.load(found)
                log.info("lista %s: %d proxies para testar", name, loaded)
                if loaded:
                    return
            self._check_stop()
            if downloaded:
                raise SessionError("nenhum proxy público funcionando no momento — tente mais tarde")
            if self._active is None:
                # Logo no início, com o usuário olhando: melhor avisar do que esperar.
                raise SessionError("não foi possível baixar a lista de proxies (sem internet?)")
            # No meio de uma sessão, nenhuma fonte responder quer dizer que a rede
            # caiu. Os proxies "reprovados" nesse intervalo não tinham culpa.
            self._pool.forget_failures()
            self._status(ORANGE, _OFFLINE_TEXT)
            self._sleep(_REFILL_RETRY)

    def _active_proxy(self) -> tuple[str, int]:
        assert self._active is not None
        return self._active.proxy

    def _active_text(self, latency_ms: float) -> str:
        assert self._active is not None
        return f"Proxy ativo — {self._active.address} ({latency_ms:.0f}ms)"

    def _backend_address(self) -> tuple[str, int] | None:
        return self._active.proxy if self._active is not None else None
