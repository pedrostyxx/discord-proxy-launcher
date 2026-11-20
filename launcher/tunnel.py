"""Modo A: túnel SSH com proxy SOCKS5 local — o equivalente a `ssh -D 1080 -N`.

Duas implementações com a mesma interface:

* SshExeTunnel   — o ssh.exe do próprio Windows, usado no login por chave.
* ParamikoTunnel — SSH embutido no app, usado no login por senha (o ssh.exe não
  aceita senha sem um terminal) e como alternativa quando o ssh.exe não serve.
"""
from __future__ import annotations

import collections
import errno
import os
import re
import select
import socket
import struct
import subprocess
import threading
import time

import paramiko

from . import config, win
from .log import log

KEY_FILES = ("id_ed25519", "id_ecdsa", "id_rsa")
_CHANNEL_OPEN_TIMEOUT = 10.0
_BUFFER = 65536


class TunnelError(Exception):
    """Falha ao abrir o túnel. `kind`: auth, network, hostkey, port ou other."""

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


# --- Porta local ------------------------------------------------------------------

def _can_bind(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((config.LOCAL_SOCKS_HOST, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def pick_local_port(preferred: int | None = None) -> int:
    """A porta do proxy local: a padrão (1080) se estiver livre, senão uma livre qualquer.

    Não encerra nada. Um túnel antigo que ainda ocupe a porta padrão só é
    fechado depois que o túnel novo estiver funcionando (close_stale_ssh), para
    um login que falha não derrubar uma conexão que estava de pé.
    """
    if preferred is None:
        preferred = config.LOCAL_SOCKS_PORT
    if _can_bind(preferred):
        return preferred
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((config.LOCAL_SOCKS_HOST, 0))
        port = probe.getsockname()[1]
    finally:
        probe.close()
    log.info("porta %d ocupada; usando %d", preferred, port)
    return port


def close_stale_ssh(port_in_use: int, preferred: int | None = None) -> int:
    """Encerra o `ssh` de um túnel antigo que ainda escute na porta padrão.

    Só o processo dono daquela porta e só na sessão do usuário: outras sessões
    ssh dele, de outros usuários do computador e de serviços não são tocadas.
    Devolve quantos processos encerrou.
    """
    if preferred is None:
        preferred = config.LOCAL_SOCKS_PORT
    if port_in_use == preferred:
        return 0                            # quem escuta na porta padrão é o túnel novo
    closed = 0
    for pid in set(win.tcp_listener_pids(preferred)):
        if (win.process_name(pid) or "").lower() == "ssh.exe" and win.in_current_session(pid):
            log.info("encerrando túnel ssh antigo (pid %d) na porta %d", pid, preferred)
            if win.kill_pid(pid):
                closed += 1
    return closed


def _label(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _wildcard_match(pattern: str, name: str) -> bool:
    """Compara um nome com um padrão do known_hosts, em que só `*` e `?` são curingas.

    (O fnmatch não serve: para ele os colchetes de "[host]:porta" seriam uma classe de caracteres.)
    """
    expression = "".join(".*" if char == "*" else "." if char == "?" else re.escape(char) for char in pattern)
    return re.fullmatch(expression, name) is not None


def _timeout_error(label: str) -> TunnelError:
    return TunnelError(f"tempo esgotado ao conectar em {label} ({config.SSH_START_TIMEOUT:.0f} s)", "network")


# --- ssh.exe ------------------------------------------------------------------------

def find_ssh_exe() -> str | None:
    """O cliente OpenSSH instalado em uma pasta protegida do sistema.

    Nunca procura pelo PATH nem pela pasta atual: o app roda como administrador
    e não pode executar um `ssh.exe` que qualquer programa conseguiria plantar.
    Sem ele, o túnel é feito pelo cliente embutido.
    """
    candidates = []
    try:
        candidates.append(os.path.join(win.system_directory(), "OpenSSH", "ssh.exe"))
        candidates.append(os.path.join(win.program_files(), "OpenSSH", "ssh.exe"))
    except OSError:
        pass
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


class SshExeTunnel:
    """`ssh -D` em um processo filho, autenticando com a chave SSH do usuário."""

    def __init__(self, ssh_exe: str, host: str, port: int, user: str, local_port: int,
                 extra_options: tuple[str, ...] = ()):
        self._ssh_exe = ssh_exe
        self.host, self.port, self.user = host, port, user
        self.local_port = local_port
        self.label = _label(host, port)
        self._extra_options = tuple(extra_options)
        self._process: subprocess.Popen | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=40)

    def command(self) -> list[str]:
        return [
            self._ssh_exe,
            # O ssh roda com o privilégio de administrador do launcher. O
            # ~/.ssh/config é um arquivo que qualquer programa do usuário altera e
            # que sabe mandar executar comandos (ProxyCommand, Match exec...), então
            # ele não é lido: host, porta e usuário vêm da tela do app.
            "-F", "none",
            "-D", f"{config.LOCAL_SOCKS_HOST}:{self.local_port}",
            "-N", "-T",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",                 # sem terminal: falhar em vez de pedir senha
            "-o", f"ConnectTimeout={int(config.SSH_START_TIMEOUT)}",
            "-o", "ServerAliveInterval=15",        # percebe a queda do servidor em ~30 s
            "-o", "ServerAliveCountMax=2",
            *self._extra_options,
            "-p", str(self.port),
            "-l", self.user,
            self.host,
        ]

    def start(self, stop: threading.Event) -> None:
        self._stderr = collections.deque(maxlen=40)
        try:
            with win.default_dll_directory():
                process = subprocess.Popen(
                    self.command(),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    env=self._environment(),
                    cwd=self._working_directory(),
                    creationflags=win.CREATE_NO_WINDOW,
                )
        except OSError as exc:
            raise TunnelError(f"não foi possível executar o ssh ({exc.strerror or exc})") from exc
        self._process = process
        win.kill_with_launcher(process)
        threading.Thread(
            target=self._drain, args=(process, self._stderr), name="ssh-stderr", daemon=True
        ).start()

        # A porta local só começa a escutar depois que o login no servidor deu certo.
        deadline = time.monotonic() + config.SSH_START_TIMEOUT
        while True:
            if process.poll() is not None:
                time.sleep(0.15)                   # dá tempo de ler o fim do stderr
                raise self._failure()
            if process.pid in win.tcp_listener_pids(self.local_port):
                return
            if stop.is_set() or time.monotonic() >= deadline:
                self.stop()
                raise self._failure(timed_out=True)
            time.sleep(0.15)

    @staticmethod
    def _working_directory() -> str | None:
        """Pasta onde o ssh roda: uma que só o administrador altera, para nenhum
        caminho relativo apontar para um arquivo plantado. (Proteção extra: o
        "-F none" já impede a leitura de qualquer config.)"""
        try:
            return win.system_directory()
        except OSError:
            return None

    @staticmethod
    def _environment() -> dict[str, str]:
        """Ambiente do ssh sem as variáveis SSH_* que o fariam carregar ou executar
        algo indicado de fora (SSH_ASKPASS, SSH_SK_PROVIDER...). Fica só o agente."""
        return {
            name: value for name, value in win.clean_environment().items()
            if not name.upper().startswith("SSH_") or name.upper() == "SSH_AUTH_SOCK"
        }

    @staticmethod
    def _drain(process: subprocess.Popen, lines: collections.deque) -> None:
        # Precisa ser lido sempre: o ssh escreve um aviso a cada conexão recusada
        # e travaria quando o buffer do pipe enchesse.
        try:
            for raw in process.stderr:
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    lines.append(text)
                    log.debug("ssh: %s", text)
        except (OSError, ValueError):
            pass

    def _failure(self, timed_out: bool = False) -> TunnelError:
        output = "\n".join(self._stderr).lower()
        if "identification has changed" in output or "man-in-the-middle" in output:
            return TunnelError(
                f"a chave do servidor mudou — se você o reinstalou, rode: ssh-keygen -R {self.host}",
                "hostkey",
            )
        if "unprotected private key" in output or "bad permissions" in output:
            return TunnelError("a chave SSH tem permissões abertas demais para o ssh", "auth")
        if "permission denied" in output or "too many authentication failures" in output:
            return TunnelError("o servidor recusou a chave SSH — informe a senha", "auth")
        if "could not resolve hostname" in output:
            return TunnelError("servidor não encontrado (confira o IP/host)", "network")
        if "connection refused" in output:
            return TunnelError(f"conexão recusada em {self.label} (porta SSH correta?)", "network")
        if "address already in use" in output or "cannot listen" in output or "could not request local forwarding" in output:
            return TunnelError(f"a porta local {self.local_port} está em uso", "port")
        if timed_out or "timed out" in output:
            return _timeout_error(self.label)
        last = self._stderr[-1] if self._stderr else "o ssh terminou sem explicar o motivo"
        return TunnelError(f"ssh: {last[:110]}")

    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def forwarding_refused(self) -> bool:
        return any("administratively prohibited" in line.lower() for line in self._stderr)

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(3)
        except subprocess.TimeoutExpired:
            process.kill()


# --- Paramiko -----------------------------------------------------------------------

def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ConnectionError("conexão encerrada")
        buf += chunk
    return bytes(buf)


class _SocksServer:
    """Servidor SOCKS5 local (só CONNECT, sem autenticação, só em 127.0.0.1).

    Cada conexão aceita vira o canal que `open_channel(host, porta, origem)`
    devolver: no túnel embutido, um canal `direct-tcpip` no SSH (é o que o
    `ssh -D` faz); no modo Proxy público, uma conexão por um proxy público.
    """

    def __init__(self, port: int, open_channel):
        self._open_channel = open_channel
        self._lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._closed = False
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self._listener.bind((config.LOCAL_SOCKS_HOST, port))
            self._listener.listen(128)
        except OSError:
            self._listener.close()
            raise
        threading.Thread(target=self._accept_loop, name="socks-accept", daemon=True).start()

    def close(self) -> None:
        self._closed = True
        with self._lock:
            sockets = [self._listener, *self._connections]
            self._connections.clear()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                client, address = self._listener.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve, args=(client, address), name="socks-conn", daemon=True
            ).start()

    @staticmethod
    def _reply(client: socket.socket, code: int) -> None:
        client.sendall(b"\x05" + bytes([code]) + b"\x00\x01" + b"\x00" * 6)

    def _serve(self, client: socket.socket, address) -> None:
        channel = None
        with self._lock:
            if self._closed:
                client.close()
                return
            self._connections.add(client)
        try:
            client.settimeout(10)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            version, method_count = _recv_exact(client, 2)
            if version != 5:
                return
            if 0 not in _recv_exact(client, method_count):
                client.sendall(b"\x05\xff")
                return
            client.sendall(b"\x05\x00")

            version, command, _, address_type = _recv_exact(client, 4)
            if address_type == 1:
                host = socket.inet_ntoa(_recv_exact(client, 4))
            elif address_type == 3:
                host = _recv_exact(client, _recv_exact(client, 1)[0]).decode("ascii", "replace")
            elif address_type == 4:
                host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
            else:
                self._reply(client, 8)
                return
            port = struct.unpack(">H", _recv_exact(client, 2))[0]
            if version != 5 or command != 1:
                self._reply(client, 7)
                return

            try:
                channel = self._open_channel(host, port, address)
            except paramiko.ChannelException as exc:
                # 1 = proibido pelo servidor (AllowTcpForwarding no); 2 = destino não respondeu
                self._reply(client, 2 if exc.code == 1 else 5)
                return
            except Exception as exc:
                log.debug("canal para %s:%s falhou: %s", host, port, exc)
                self._reply(client, getattr(exc, "socks_reply", None) or 1)
                return
            self._reply(client, 0)
            client.settimeout(None)
            self._relay(client, channel)
        except (OSError, EOFError, struct.error):
            pass
        finally:
            with self._lock:
                self._connections.discard(client)
            for endpoint in (channel, client):
                try:
                    if endpoint is not None:
                        endpoint.close()
                except Exception:
                    pass

    @staticmethod
    def _relay(client: socket.socket, channel: paramiko.Channel) -> None:
        def downstream() -> None:               # servidor -> Discord
            try:
                while True:
                    data = channel.recv(_BUFFER)
                    if not data:
                        break
                    client.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    client.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        thread = threading.Thread(target=downstream, name="socks-down", daemon=True)
        thread.start()
        try:                                     # Discord -> servidor
            while True:
                data = client.recv(_BUFFER)
                if not data:
                    break
                channel.sendall(data)
        except Exception:
            pass
        finally:
            try:
                channel.shutdown_write()
            except Exception:
                pass
        # O cliente parou de enviar; dá um prazo para o resto da resposta chegar.
        thread.join(30)


# Chaves que cada servidor apresentou em logins bem-sucedidos do cliente embutido,
# guardadas só na memória e enquanto o app estiver aberto (um Parar/Iniciar não as
# esquece). Valem como linhas a mais do known_hosts, que o app não grava.
_seen_host_keys: dict[tuple[str, int], list[paramiko.PKey]] = {}
_seen_host_keys_lock = threading.Lock()


class ParamikoTunnel:
    """Cliente SSH embutido + servidor SOCKS5 local."""

    def __init__(self, host: str, port: int, user: str, password: str | None,
                 local_port: int, ssh_dir: str | None = None):
        self.host, self.port, self.user = host, port, user
        self._password = password or None
        self.local_port = local_port
        self.label = _label(host, port)
        self._ssh_dir = ssh_dir or os.path.join(win.user_dirs()[1], ".ssh")
        self._transport: paramiko.Transport | None = None
        self._server: _SocksServer | None = None
        self._refused = False

    # -- ciclo de vida --

    def start(self, stop: threading.Event) -> None:
        self._refused = False
        transport = self._connect(stop)
        try:
            self._server = _SocksServer(self.local_port, self._open_channel)
        except OSError as exc:
            transport.close()
            raise TunnelError(f"a porta local {self.local_port} está em uso", "port") from exc
        self._transport = transport

    def alive(self) -> bool:
        transport = self._transport
        return transport is not None and transport.is_active()

    def forwarding_refused(self) -> bool:
        return self._refused

    def stop(self) -> None:
        server, self._server = self._server, None
        transport, self._transport = self._transport, None
        if server is not None:
            server.close()
        if transport is not None:
            transport.close()

    def _open_channel(self, host: str, port: int, source) -> paramiko.Channel:
        transport = self._transport
        if transport is None or not transport.is_active():
            raise OSError("túnel SSH fora do ar")
        try:
            return transport.open_channel(
                "direct-tcpip", (host, port), source, timeout=_CHANNEL_OPEN_TIMEOUT
            )
        except paramiko.ChannelException as exc:
            if exc.code == 1:
                self._refused = True
            raise

    # -- conexão --

    def _connect(self, stop: threading.Event) -> paramiko.Transport:
        deadline = time.monotonic() + config.SSH_START_TIMEOUT

        def left() -> float:
            """Tempo que resta do prazo único de 10 s; estourou (ou clicaram em Parar) = erro."""
            remaining = deadline - time.monotonic()
            if stop.is_set() or remaining <= 0:
                raise _timeout_error(self.label)
            return remaining

        sock = self._open_socket(left)
        try:
            transport = paramiko.Transport(sock)
        except Exception:
            sock.close()
            raise
        try:
            transport.banner_timeout = transport.handshake_timeout = left()
            known = self._known_host_keys()
            remembered = self._remembered_host_keys()
            self._prefer_known_key_types(transport, known + remembered)
            # Negociação acompanhada daqui, para respeitar o prazo e o botão Parar
            # (o start_client() bloqueante volta em silêncio quando o tempo acaba).
            negotiated = threading.Event()
            transport.start_client(event=negotiated)
            while not negotiated.wait(0.1):
                left()
            if not (transport.is_active() and transport.initial_kex_done):
                raise transport.get_exception() or _timeout_error(self.label)
            server_key = self._verify_host_key(transport, known, remembered)
            transport.auth_timeout = left()
            self._authenticate(transport)
            transport.set_keepalive(15)
            self._remember_host_key(server_key)
            return transport
        except TunnelError:
            self._discard(transport, sock)
            raise
        except paramiko.AuthenticationException as exc:
            self._discard(transport, sock)
            if "timeout" in str(exc).lower():
                raise _timeout_error(self.label) from exc
            raise TunnelError("o servidor recusou o login", "auth") from exc
        except (paramiko.SSHException, EOFError, OSError) as exc:
            self._discard(transport, sock)
            text = str(exc).lower()
            if "banner" in text or "protocol version" in text:
                message = f"{self.label} não respondeu como um servidor SSH (porta correta?)"
            elif "timeout" in text or "timed out" in text or time.monotonic() >= deadline:
                raise _timeout_error(self.label) from exc
            elif "no existing session" in text:
                message = "o servidor encerrou a conexão durante o login"
            else:
                message = f"falha na conexão SSH ({str(exc)[:80] or type(exc).__name__})"
            raise TunnelError(message, "network") from exc

    @staticmethod
    def _discard(transport: paramiko.Transport, sock: socket.socket) -> None:
        # Transport.close() não faz nada se a negociação nem começou; o socket é fechado à parte.
        for item in (transport, sock):
            try:
                item.close()
            except Exception:
                pass

    def _resolve(self, left) -> list:
        """Resolve o nome do servidor sem ficar preso ao DNS: a consulta roda em
        outra thread e a espera respeita o prazo e o botão Parar."""
        outcome: list = []

        def lookup() -> None:
            try:
                outcome.append(socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM))
            except Exception as exc:
                outcome.append(exc)

        worker = threading.Thread(target=lookup, name="ssh-dns", daemon=True)
        worker.start()
        while worker.is_alive():
            left()
            worker.join(0.1)
        result = outcome[0] if outcome else OSError("sem resposta do DNS")
        if isinstance(result, (socket.gaierror, UnicodeError)):
            raise TunnelError("servidor não encontrado (confira o IP/host)", "network") from result
        if isinstance(result, Exception):
            raise TunnelError("sem rota de rede até o servidor", "network") from result
        return result

    @staticmethod
    def _connect_socket(sock: socket.socket, address, budget: float, left) -> None:
        """connect() em fatias de 0,1 s, para o prazo e o botão Parar valerem durante a conexão."""
        sock.setblocking(False)
        code = sock.connect_ex(address)
        if code not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
            raise OSError(code, os.strerror(code))
        deadline = time.monotonic() + budget
        while True:
            left()
            if time.monotonic() >= deadline:
                raise TimeoutError("tempo esgotado")
            _, writable, failed = select.select([], [sock], [sock], 0.1)
            if writable or failed:
                code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if code:
                    raise OSError(code, os.strerror(code))
                if not writable:
                    raise ConnectionError("a conexão falhou")
                break
        sock.setblocking(True)

    def _open_socket(self, left) -> socket.socket:
        """Conecta no servidor tentando cada endereço (IPv6/IPv4) dentro do mesmo prazo."""
        addresses = self._resolve(left)
        failure: OSError | None = None
        for index, (family, kind, protocol, _, address) in enumerate(addresses):
            sock = socket.socket(family, kind, protocol)
            try:
                # O tempo que resta é repartido entre os endereços que faltam: um
                # IPv6 que não responde não pode consumir o prazo do IPv4.
                self._connect_socket(sock, address, left() / (len(addresses) - index), left)
                return sock
            except TunnelError:
                sock.close()
                raise
            except OSError as exc:
                sock.close()
                failure = exc
        if isinstance(failure, ConnectionRefusedError):
            raise TunnelError(f"conexão recusada em {self.label} (porta SSH correta?)", "network") from failure
        if isinstance(failure, TimeoutError):
            raise _timeout_error(self.label) from failure
        raise TunnelError("sem rota de rede até o servidor", "network") from failure

    # -- chave do servidor --
    # Mesma regra do `StrictHostKeyChecking=no` do OpenSSH: um servidor nunca
    # visto é aceito; um servidor conhecido que apresenta uma chave diferente de
    # todas as registradas — de qualquer tipo — é recusado, e a senha não chega
    # a ser enviada. "Registradas" = as do known_hosts mais as que o servidor
    # apresentou em logins anteriores desta execução do app (o app não grava no
    # known_hosts; nada vai para o disco).

    def _remembered_host_keys(self) -> list[paramiko.PKey]:
        with _seen_host_keys_lock:
            return list(_seen_host_keys.get((self.host.lower(), self.port), ()))

    def _remember_host_key(self, key: paramiko.PKey) -> None:
        with _seen_host_keys_lock:
            keys = _seen_host_keys.setdefault((self.host.lower(), self.port), [])
            if all(existing.asbytes() != key.asbytes() for existing in keys):
                keys.append(key)

    def _known_host_keys(self) -> list[paramiko.PKey]:
        name = (self.host if self.port == config.SSH_DEFAULT_PORT else f"[{self.host}]:{self.port}").lower()
        try:
            with open(os.path.join(self._ssh_dir, "known_hosts"), encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return []
        keys = []
        for line in lines:
            line = line.strip()
            if not line or line[0] in "#@":       # comentários e marcadores (@cert-authority, @revoked)
                continue
            try:
                entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
            except Exception:
                continue                           # linha ilegível: ignora só ela, não o arquivo todo
            if entry is None or entry.key is None or not entry.hostnames:
                continue
            if self._host_patterns_match(entry.hostnames, name):
                keys.append(entry.key)
        return keys

    @staticmethod
    def _host_patterns_match(patterns: list[str], name: str) -> bool:
        """As regras do known_hosts: nome exato, hash, curingas (* e ?) e negação (!)."""
        matched = False
        for pattern in patterns:
            negated = pattern.startswith("!")
            if negated:
                pattern = pattern[1:]
            if pattern.startswith("|1|"):          # nome guardado como hash (HashKnownHosts)
                try:
                    hit = paramiko.HostKeys.hash_host(name, pattern) == pattern
                except Exception:
                    hit = False
            else:
                hit = _wildcard_match(pattern.lower(), name)
            if hit and negated:
                return False
            matched = matched or hit
        return matched

    @staticmethod
    def _prefer_known_key_types(transport: paramiko.Transport, known: list[paramiko.PKey]) -> None:
        """Pede ao servidor um tipo de chave que já conhecemos, para a comparação ser possível."""
        options = transport.get_security_options()
        available = list(options.key_types)
        wanted: list[str] = []
        for key in known:
            key_type = key.get_name()
            names = ("rsa-sha2-512", "rsa-sha2-256") if key_type == "ssh-rsa" else (key_type,)
            wanted += [name for name in names if name in available and name not in wanted]
        if wanted:
            options.key_types = wanted + [name for name in available if name not in wanted]

    def _verify_host_key(self, transport: paramiko.Transport, known: list[paramiko.PKey],
                         remembered: list[paramiko.PKey]) -> paramiko.PKey:
        presented = transport.get_remote_server_key()
        trusted = {key.asbytes() for key in known} | {key.asbytes() for key in remembered}
        if trusted and presented.asbytes() not in trusted:
            if known:
                message = f"a chave do servidor mudou — se você o reinstalou, rode: ssh-keygen -R {self.host}"
            else:
                message = "a chave do servidor mudou desde o último login — reabra o launcher se o reinstalou"
            raise TunnelError(message, "hostkey")
        return presented

    # -- login --

    def _authenticate(self, transport: paramiko.Transport) -> None:
        if self._password:
            try:
                transport.auth_password(self.user, self._password)
            except paramiko.BadAuthenticationType as exc:
                # Se o servidor oferece "keyboard-interactive", o Paramiko já tentou
                # a senha por esse caminho e ela foi recusada.
                if "keyboard-interactive" in (exc.allowed_types or ()):
                    raise TunnelError("usuário ou senha SSH incorretos", "auth") from exc
                raise TunnelError("o servidor não aceita login por senha (use chave SSH)", "auth") from exc
            except paramiko.AuthenticationException as exc:
                if "timeout" in str(exc).lower():
                    raise _timeout_error(self.label) from exc
                raise TunnelError("usuário ou senha SSH incorretos", "auth") from exc
            if not transport.is_authenticated():
                raise TunnelError("o servidor exige uma etapa extra de login (2FA)", "auth")
            return

        offered = 0
        locked = False
        agent = None
        try:
            keys = []
            try:
                agent = paramiko.Agent()
                keys += list(agent.get_keys())
            except Exception as exc:
                log.debug("agente SSH indisponível: %s", exc)
            for name in KEY_FILES:
                path = os.path.join(self._ssh_dir, name)
                if not os.path.isfile(path):
                    continue
                try:
                    keys.append(paramiko.PKey.from_path(path))
                except Exception as exc:
                    # Chave protegida por senha (passphrase) ou em formato não suportado.
                    text = f"{type(exc).__name__} {exc}".lower()
                    locked = locked or "password" in text or "encrypted" in text
                    log.debug("chave %s não carregada: %s", name, exc)
            for key in keys:
                offered += 1
                try:
                    transport.auth_publickey(self.user, key)
                except paramiko.BadAuthenticationType as exc:
                    raise TunnelError("o servidor não aceita chave SSH — informe a senha", "auth") from exc
                except paramiko.AuthenticationException as exc:
                    if "timeout" in str(exc).lower():
                        raise _timeout_error(self.label) from exc
                    continue
                if transport.is_authenticated():
                    return
        finally:
            if agent is not None:
                try:
                    agent.close()
                except Exception:
                    pass
        if offered:
            raise TunnelError("o servidor recusou a chave SSH — informe a senha", "auth")
        if locked:
            raise TunnelError("a chave SSH tem senha (passphrase) — informe a senha do servidor", "auth")
        raise TunnelError("nenhuma chave SSH encontrada em .ssh — informe a senha", "auth")
