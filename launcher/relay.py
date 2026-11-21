"""Modo B: proxy SOCKS5 local entre o Discord e os proxies (dedicado ou públicos).

O Discord fica apontado para 127.0.0.1 durante a sessão inteira. Cada conexão
que ele abre segue um plano montado na hora:

- com o servidor dedicado ativo: destinos do Discord vão por ele (e, se ele
  falhar, pelos proxies públicos que já estiverem testados); o resto — vídeos
  do YouTube, Spotify, envio de anexos ao Google — sai direto, como sem o app,
  porque o servidor só atende o Discord;
- com um proxy público ativo: tudo por ele e, se ele recusar ou demorar, a mesma
  conexão é tentada nos melhores da reserva — o Discord nem percebe.

Um proxy público que perde para os outros várias vezes seguidas sai de uso: o
da reserva é descartado na hora; o ativo é sinalizado e a sessão escolhe o
próximo. O servidor dedicado só leva a culpa por falhas que são dele (sem
conexão, sem TLS, tempo esgotado): uma recusa respondida prova que ele está
vivo. Trocar o ativo nunca exige fechar e reabrir o Discord.
"""
from __future__ import annotations

import queue
import socket
import threading
import time
from typing import NamedTuple

from . import config, dconnect, destinations, proxies, socks5, tunnel
from .log import log


class RelayError(OSError):
    """Nenhum caminho conseguiu a conexão. `socks_reply` é o código devolvido ao Discord."""

    def __init__(self, message: str, socks_reply: int = 1):
        super().__init__(message)
        self.socks_reply = socks_reply


class _SocketChannel:
    """Um socket com a interface de canal que o servidor SOCKS local usa."""

    def __init__(self, sock: socket.socket):
        self._sock = sock

    def recv(self, count: int) -> bytes:
        return self._sock.recv(count)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def shutdown_write(self) -> None:
        self._sock.shutdown(socket.SHUT_WR)

    def close(self) -> None:
        self._sock.close()


PROXY, VPS, DIRECT = "proxy", "vps", "direct"


class _Candidate(NamedTuple):
    kind: str
    entry: proxies.Approved | None


class Events(NamedTuple):
    active_failing: bool         # o proxy público ativo vem perdendo para a reserva
    vps_suspect: bool            # o servidor dedicado vem falhando nas conexões do Discord
    vps_rejected: str            # o servidor recusou o token deste app ("" se não)
    vps_refusing: bool = False   # o servidor responde, mas vem recusando tudo (limites)


class ProxyRelay:
    """Servidor SOCKS5 em 127.0.0.1 que repassa cada conexão do Discord."""

    def __init__(self, port: int, pool: proxies.ProxyPool, vps: dconnect.DedicatedServer | None = None):
        self._pool = pool
        self._vps = vps
        self._lock = threading.Lock()
        self._active: proxies.Approved | None = None
        self._fallback: proxies.Approved | None = None
        self._strikes: dict[proxies.Proxy, int] = {}
        self._active_failing = False
        self._vps_failures: list[float] = []
        self._vps_suspect = False
        self._vps_refusals: list[float] = []
        self._vps_refusing = False
        self._vps_rejected = ""
        self._mismatch_logged = False
        self.port = port
        self._server = tunnel._SocksServer(port, self._open_channel)

    # -- controle (pela sessão) --

    @property
    def active(self) -> proxies.Approved | None:
        with self._lock:
            return self._active

    @property
    def active_failing(self) -> bool:
        """O ativo perdeu para a reserva RELAY_MAX_STRIKES vezes seguidas."""
        with self._lock:
            return self._active_failing

    def set_active(self, entry: proxies.Approved) -> None:
        with self._lock:
            self._active = entry
            self._fallback = None
            self._strikes.pop(entry.proxy, None)
            self._active_failing = False
            self._clear_vps_signals()

    def _clear_vps_signals(self) -> None:
        self._vps_failures.clear()
        self._vps_suspect = False
        self._vps_refusals.clear()
        self._vps_refusing = False

    def demote_active(self) -> None:
        """Enquanto a sessão procura outro, o ativo com problema vira a última opção."""
        with self._lock:
            if self._active is not None:
                self._fallback, self._active = self._active, None
            self._active_failing = False
            self._clear_vps_signals()

    def take_events(self) -> Events:
        """Lê e zera os sinais para a sessão (chamado só pela thread da sessão)."""
        with self._lock:
            events = Events(self._active_failing, self._vps_suspect, self._vps_rejected, self._vps_refusing)
            self._active_failing = False
            self._vps_suspect = False
            self._vps_refusing = False
            return events

    def close(self) -> None:
        self._server.close()

    # -- o plano de cada conexão --

    def _plan(self, host: str, port: int) -> list[_Candidate]:
        plan = self._proxied_plan(host, port)
        if destinations.goes_direct_first(host, port):
            # Mede a rede do usuário (regiões de voz): direto primeiro; os proxies só
            # correm junto se a internet dele não responder dentro do RELAY_STAGGER.
            plan = [_Candidate(DIRECT, None)] + [candidate for candidate in plan if candidate.kind != DIRECT]
        return plan

    def _proxied_plan(self, host: str, port: int) -> list[_Candidate]:
        discord_host = destinations.goes_through_vps(host, port)
        with self._lock:
            active, fallback = self._active, self._fallback
            vps_usable = self._vps is not None and not self._vps_rejected
        on_vps = active is not None and active.dedicated
        vps_fallback = active is None and fallback is not None and fallback.dedicated
        vps_entry = active if on_vps else (fallback if vps_fallback else None)

        plan: list[_Candidate] = []
        free = [entry for entry in self._pool.peek(config.RELAY_BACKUPS + 1) if not entry.dedicated]
        if on_vps or vps_fallback:
            if not discord_host:
                return [_Candidate(DIRECT, None)]       # o servidor só atende o Discord
            if on_vps and vps_usable:
                plan.append(_Candidate(VPS, vps_entry))
            plan += [_Candidate(PROXY, entry) for entry in free[:config.RELAY_BACKUPS]]
            if vps_fallback and vps_usable:
                plan.append(_Candidate(VPS, vps_entry))
            return plan

        if active is not None:
            plan.append(_Candidate(PROXY, active))
        for entry in free:
            if len(plan) > config.RELAY_BACKUPS:
                break
            if all(entry.proxy != other.entry.proxy for other in plan):
                plan.append(_Candidate(PROXY, entry))
        if fallback is not None and all(fallback.proxy != other.entry.proxy for other in plan):
            plan.append(_Candidate(PROXY, fallback))
        if discord_host and vps_usable and self._vps is not None:
            # Com o servidor dedicado fora do ar, ele ainda é a última tentativa:
            # se voltou, a conexão passa, e as falhas não contam contra ninguém.
            plan.append(_Candidate(VPS, None))
        return plan

    def _attempt(self, candidate: _Candidate, host: str, port: int, deadline: float):
        if candidate.kind == PROXY:
            sock = socks5.connect(candidate.entry.proxy, host, port, deadline=deadline)
            sock.settimeout(None)
            return _SocketChannel(sock)
        if candidate.kind == VPS:
            return dconnect.open_tunnel(self._vps, host, port, deadline=deadline)
        sock = socket.create_connection((host, port), timeout=max(0.5, deadline - time.monotonic()))
        sock.settimeout(None)
        return _SocketChannel(sock)

    # -- conexões do Discord --

    def _open_channel(self, host: str, port: int, source):
        plan = self._plan(host, port)
        if not plan:
            raise RelayError("nenhum proxy disponível")
        results: queue.Queue = queue.Queue()
        settle_lock = threading.Lock()
        settled = False

        def attempt(index: int, candidate: _Candidate) -> None:
            try:
                channel = self._attempt(candidate, host, port, time.monotonic() + config.RELAY_ATTEMPT_TIMEOUT)
            except OSError as exc:
                results.put((index, None, exc))
                return
            except Exception as exc:          # nunca deixa a conexão esperando o prazo todo
                results.put((index, None, OSError(f"{type(exc).__name__}: {exc}")))
                return
            with settle_lock:
                if not settled:
                    results.put((index, channel, None))
                    return
            channel.close()                     # outro caminho já ganhou esta conexão

        launched = 0

        def launch_next() -> float:
            nonlocal launched
            threading.Thread(
                target=attempt, args=(launched, plan[launched]), name="relay-try", daemon=True
            ).start()
            launched += 1
            return time.monotonic() + config.RELAY_STAGGER

        deadline = time.monotonic() + config.RELAY_CONNECT_TIMEOUT
        next_launch = launch_next()
        finished = 0
        errors: dict[int, OSError] = {}
        winner = None
        while winner is None:
            now = time.monotonic()
            if now >= deadline:
                break
            if launched < len(plan) and now >= next_launch:
                next_launch = launch_next()         # o atual está demorando: o próximo corre junto
                continue
            wait = deadline - now
            if launched < len(plan):
                wait = min(wait, next_launch - now)
            try:
                index, channel, error = results.get(timeout=max(wait, 0.01))
            except queue.Empty:
                continue
            finished += 1
            if channel is not None:
                winner = (index, channel)
            else:
                errors[index] = error
                self._note_failure(plan[index], error, host)
                if launched < len(plan):
                    next_launch = launch_next()     # falhou: o próximo começa já
                elif finished >= launched:
                    break
        with settle_lock:
            settled = True
        while True:                                 # sobras que chegaram junto com o vencedor
            try:
                _, extra, _ = results.get_nowait()
            except queue.Empty:
                break
            if extra is not None and (winner is None or extra is not winner[1]):
                extra.close()

        if winner is None:
            log.debug("relay: %s:%s falhou por %d caminhos", host, port, len(plan))
            raise RelayError(f"nenhum caminho alcançou {host}:{port}", self._reply_for(errors))

        index, channel = winner
        self._score(plan, index)
        return channel

    @staticmethod
    def _reply_for(errors: dict[int, OSError]) -> int:
        replies = set()
        for error in errors.values():
            if isinstance(error, dconnect.DestinationRefused):
                replies.add(2)
            elif isinstance(error, dconnect.DestinationUnreachable):
                replies.add(4)
            else:
                replies.add(getattr(error, "reply", None))
        reply = replies.pop() if len(replies) == 1 else None
        return reply or 1

    def _note_failure(self, candidate: _Candidate, error: OSError, host: str) -> None:
        """Contabiliza falhas do servidor dedicado (as dos proxies públicos vão em _score)."""
        if candidate.kind != VPS:
            return
        if isinstance(error, dconnect.TokenRejected):
            with self._lock:
                first = not self._vps_rejected
                self._vps_rejected = self._vps_rejected or (error.code or "rejeitado")
            if first:
                log.warning("relay: o servidor dedicado recusou este app (%s)", error)
            return
        if isinstance(error, dconnect.DestinationRefused):
            if not self._mismatch_logged:
                self._mismatch_logged = True
                log.warning("relay: o servidor dedicado recusou %s (lista diferente da do app?)", host)
            return
        now = time.monotonic()
        if isinstance(error, (dconnect.ServerBusy, dconnect.DestinationUnreachable)):
            # Respondeu, então está vivo; mas se só recusa (cota, limites de um IP
            # compartilhado), para este usuário ele não está servindo.
            with self._lock:
                if candidate.entry is None or self._active is not candidate.entry:
                    return
                self._vps_refusals.append(now)
                refusals = self._vps_refusals
                if (len(refusals) >= config.VPS_REFUSING_COUNT
                        and refusals[-1] - refusals[0] >= config.VPS_REFUSING_SPAN and not self._vps_refusing):
                    self._vps_refusing = True
                    log.info("relay: o servidor dedicado recusou %d conexões em %.0f s", len(refusals),
                             refusals[-1] - refusals[0])
            return
        with self._lock:
            if candidate.entry is None or self._active is not candidate.entry:
                return                          # já não é o ativo: não conta contra o atual
            self._vps_failures.append(now)
            failures = self._vps_failures
            if (len(failures) >= config.VPS_SUSPECT_FAILURES
                    and failures[-1] - failures[0] >= config.VPS_SUSPECT_SPAN and not self._vps_suspect):
                self._vps_suspect = True
                log.info("relay: o servidor dedicado falhou %d vezes em %.0f s", len(failures),
                         failures[-1] - failures[0])

    def _score(self, plan: list[_Candidate], winner: int) -> None:
        """Quem foi tentado antes do vencedor perdeu esta (falhou ou demorou demais).

        Só vale para os proxies públicos e só quando alguém conseguiu: se todos
        falham, pode ser o destino ou a rede do próprio usuário, e aí nenhum proxy
        tem culpa provada. O servidor dedicado nunca leva derrota por demorar mais
        que um proxy público (ele tem mais idas e voltas por conexão).
        """
        dropped = []
        with self._lock:
            won = plan[winner]
            if won.kind == VPS:
                self._vps_failures.clear()
                self._vps_refusals.clear()
            elif won.kind == PROXY:
                self._strikes.pop(won.entry.proxy, None)
            for candidate in plan[:winner]:
                if candidate.kind != PROXY:
                    continue
                entry = candidate.entry
                strikes = self._strikes.get(entry.proxy, 0) + 1
                if self._active is not None and entry.proxy == self._active.proxy:
                    self._strikes[entry.proxy] = strikes
                    if strikes >= config.RELAY_MAX_STRIKES and not self._active_failing:
                        self._active_failing = True
                        log.info("relay: o proxy ativo %s perdeu %d vezes seguidas", entry.address, strikes)
                elif strikes >= config.RELAY_MAX_STRIKES:
                    self._strikes.pop(entry.proxy, None)
                    dropped.append(entry)
                else:
                    self._strikes[entry.proxy] = strikes
        for entry in dropped:
            log.info("relay: %s saiu da reserva (perdeu %d vezes seguidas)", entry.address, config.RELAY_MAX_STRIKES)
            self._pool.mark_failed(entry.proxy)
