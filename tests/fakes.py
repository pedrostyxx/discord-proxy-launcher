"""Infraestrutura de teste: servidor SSH local, destino HTTP local e um "Discord" falso.

Nada aqui toca no Discord verdadeiro, nas chaves SSH do usuário nem na porta 1080.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import paramiko  # noqa: E402

from launcher import discord, log, win  # noqa: E402

log.setup()     # cala o log do Paramiko, como o app faz

FAKE_FOLDER = "FakeChatDPL"
FAKE_EXE = "FakeChatDPL.exe"
FAKE_SCRIPT = "fakechat.py"
FAKE_VARIANTS = ((FAKE_FOLDER, FAKE_EXE),)


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _pump(source_recv, sink_send, on_end) -> None:
    try:
        while True:
            data = source_recv(65536)
            if not data:
                break
            sink_send(data)
    except Exception:
        pass
    finally:
        try:
            on_end()
        except Exception:
            pass


# --- Destino HTTP ----------------------------------------------------------------

class HttpTarget:
    """Responde `HTTP/1.1 200 OK` a qualquer conexão: o destino da verificação de saúde."""

    def __init__(self, respond: bool = True):
        self.respond = respond
        self.hits = 0
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(32)
        self.address = ("127.0.0.1", self._listener.getsockname()[1])
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            client.settimeout(10)
            client.recv(4096)
            self.hits += 1
            if self.respond:
                client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            else:
                time.sleep(8)
        except OSError:
            pass
        finally:
            client.close()

    def close(self) -> None:
        self._listener.close()


# --- Servidor SSH -------------------------------------------------------------------

class _Handler(paramiko.ServerInterface):
    def __init__(self, server: "SshTestServer"):
        self._server = server
        self.sockets: dict[int, socket.socket] = {}

    def get_allowed_auths(self, username):
        return ",".join(self._server.allowed_auths)

    def _offers(self, method: str) -> bool:
        return method in self._server.allowed_auths

    def check_auth_password(self, username, password):
        good = self._offers("password") and username == self._server.user and password == self._server.password
        return paramiko.AUTH_SUCCESSFUL if good else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        good = self._offers("publickey") and username == self._server.user and any(
            key.asbytes() == allowed.asbytes() for allowed in self._server.authorized_keys
        )
        return paramiko.AUTH_SUCCESSFUL if good else paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        # Como um servidor com PAM: a senha é pedida por "keyboard-interactive".
        if not self._offers("keyboard-interactive") or username != self._server.user:
            return paramiko.AUTH_FAILED
        query = paramiko.InteractiveQuery("", "")
        query.add_prompt("Password: ", False)
        return query

    def check_auth_interactive_response(self, responses):
        good = list(responses) == [self._server.password]
        return paramiko.AUTH_SUCCESSFUL if good else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        if not self._server.allow_forwarding:
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        try:
            self.sockets[chanid] = socket.create_connection(destination, timeout=5)
        except OSError:
            return paramiko.OPEN_FAILED_CONNECT_FAILED
        self._server.forwarded.append(destination)
        return paramiko.OPEN_SUCCEEDED


class SshTestServer:
    """Servidor SSH mínimo em 127.0.0.1 com login por senha/chave e `direct-tcpip`."""

    user = "tester"

    def __init__(self, password: str = "s3nha-de-teste", authorized_keys=(), allow_forwarding: bool = True,
                 allowed_auths=("password", "publickey"), port: int | None = None):
        self.password = password
        self.authorized_keys = list(authorized_keys)
        self.allow_forwarding = allow_forwarding
        self.allowed_auths = allowed_auths
        self.port = port or free_port()
        self.host_key = paramiko.ECDSAKey.generate()
        self.extra_host_keys: list[paramiko.PKey] = []     # outros tipos de chave que o servidor também oferece
        self.forwarded: list[tuple[str, int]] = []
        self.logins = 0
        self._listener: socket.socket | None = None
        self._transports: list[paramiko.Transport] = []
        self._lock = threading.Lock()

    def start(self) -> "SshTestServer":
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(32)
        self._listener = listener
        threading.Thread(target=self._accept_loop, args=(listener,), daemon=True).start()
        return self

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        with self._lock:
            transports, self._transports = self._transports, []
        for transport in transports:
            transport.close()

    def _accept_loop(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        transport = paramiko.Transport(client)
        transport.add_server_key(self.host_key)
        for key in self.extra_host_keys:
            transport.add_server_key(key)
        handler = _Handler(self)
        with self._lock:
            self._transports.append(transport)
        try:
            transport.start_server(server=handler)
        except Exception:
            transport.close()
            return
        def logged_in() -> bool:           # continua valendo depois que a conexão fecha
            auth = transport.auth_handler
            return auth is not None and auth.authenticated

        counted = False
        while True:
            if not counted and logged_in():
                self.logins += 1
                counted = True
            if not transport.is_active():
                break
            channel = transport.accept(0.2)
            if channel is None:
                continue
            outbound = handler.sockets.pop(channel.get_id(), None)
            if outbound is None:
                channel.close()
                continue
            outbound.settimeout(None)
            threading.Thread(
                target=_pump, args=(channel.recv, outbound.sendall, lambda o=outbound: o.shutdown(socket.SHUT_WR)),
                daemon=True,
            ).start()
            threading.Thread(
                target=_pump, args=(outbound.recv, channel.sendall, channel.close), daemon=True,
            ).start()


def generate_client_key(directory: str, name: str = "id_ed25519", passphrase: str = "") -> str:
    """Cria uma chave SSH de teste com o ssh-keygen do Windows; devolve o caminho."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-C", "dpl-teste", "-f", path],
        check=True, stdin=subprocess.DEVNULL, capture_output=True,
    )
    # O ssh.exe do Windows recusa chaves com qualquer permissão além do próprio
    # usuário, SYSTEM e Administradores; a pasta temporária acrescenta a entrada
    # "DIREITOS DO PROPRIETÁRIO" (S-1-3-4), que precisa sair.
    subprocess.run(
        ["icacls", path, "/inheritance:r", "/grant:r", f"{os.environ['USERNAME']}:F", "/remove", "*S-1-3-4"],
        check=True, stdin=subprocess.DEVNULL, capture_output=True,
    )
    return path


# --- "Discord" falso -------------------------------------------------------------------
#
# Os testes precisam de um processo com nome próprio para abrir e encerrar sem
# encostar no Discord de verdade. Ele é uma CÓPIA do pythonw.exe oficial (que tem
# assinatura digital) com outro nome, rodando um script que só anota como foi
# chamado. Um executável compilado na hora não serve: sem assinatura, o Smart
# App Control do Windows 11 o bloqueia.

_FAKE_SCRIPT_SOURCE = '''\
import ctypes
import os
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
names = ("TCL_LIBRARY", "TK_LIBRARY", "_PYI_APPLICATION_HOME_DIR", "_MEIPASS2", "DPL_TEST_MARKER")
leaked = "".join(name + ";" for name in names if os.environ.get(name))

# O que o Chromium do Discord faz ao subir: abre o próprio token (para criar os
# tokens restritos dos processos filhos), o próprio processo e objetos que ele
# mesmo criou. Se algo disso falhar, o Discord fecha na hora — e este também.
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)
k32.GetCurrentProcess.restype = ctypes.c_void_p
k32.OpenProcess.restype = ctypes.c_void_p
k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
k32.CreateEventW.restype = ctypes.c_void_p
k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
k32.OpenEventW.restype = ctypes.c_void_p
k32.OpenEventW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
k32.CloseHandle.argtypes = [ctypes.c_void_p]
adv.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
token = ctypes.c_void_p()
token_ok = bool(adv.OpenProcessToken(k32.GetCurrentProcess(), 0x008B, ctypes.byref(token)))   # DUPLICATE|ASSIGN|QUERY|ADJUST_DEFAULT
if token_ok:
    k32.CloseHandle(token)
own = k32.OpenProcess(0x0440, False, os.getpid())                                           # DUP_HANDLE|QUERY_INFORMATION
process_ok = bool(own)
if own:
    k32.CloseHandle(own)
event_name = "Local\\\\dpl-fake-%d" % os.getpid()
event = k32.CreateEventW(None, True, False, event_name)
again = k32.OpenEventW(0x1F0003, False, event_name)                                         # EVENT_ALL_ACCESS
objects_ok = bool(event) and bool(again)

line = "\\t".join([
    str(os.getpid()),
    " ".join(sys.argv[1:]),
    str(bool(ctypes.windll.shell32.IsUserAnAdmin())),
    leaked,
    str(token_ok),
    str(process_ok),
    str(objects_ok),
])
with open(os.path.join(here, "launches.log"), "a", encoding="utf-8") as handle:
    handle.write(line + "\\n")
if not (token_ok and process_ok and objects_ok):
    sys.exit(3)
time.sleep(3600)
'''


def build_fake_discord(root: str) -> str:
    """Monta `root\\FakeChatDPL\\app-X\\FakeChatDPL.exe` como o instalador do Discord faria.

    Devolve a pasta que faz o papel de %LOCALAPPDATA%.
    """
    python_home = sys.base_prefix
    runtime = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    base = os.path.join(root, FAKE_FOLDER)
    # 1.0.10 é mais nova que 1.0.4 (ordem numérica); 1.0.11 está incompleta, sem o .exe.
    for version in ("1.0.4", "1.0.10"):
        folder = os.path.join(base, f"app-{version}")
        os.makedirs(folder, exist_ok=True)
        shutil.copyfile(os.path.join(python_home, "pythonw.exe"), os.path.join(folder, FAKE_EXE))
        for name in (runtime, "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
            source = os.path.join(python_home, name)
            if os.path.isfile(source):
                shutil.copyfile(source, os.path.join(folder, name))
        # O ._pth diz ao interpretador copiado onde está a biblioteca padrão.
        with open(os.path.join(folder, FAKE_EXE[:-4] + "._pth"), "w", encoding="utf-8") as handle:
            handle.write(f"{os.path.join(python_home, 'Lib')}\n{os.path.join(python_home, 'DLLs')}\n")
        with open(os.path.join(folder, FAKE_SCRIPT), "w", encoding="utf-8") as handle:
            handle.write(_FAKE_SCRIPT_SOURCE)
    os.makedirs(os.path.join(base, "app-1.0.11"), exist_ok=True)
    return root


def fake_install(root: str):
    return discord.find_install(root, FAKE_VARIANTS)


def fake_arguments(install, *arguments: str) -> list[str]:
    """Argumentos para iniciar o Discord falso: o script vem antes dos argumentos de verdade."""
    return [os.path.join(os.path.dirname(install.exe_path), FAKE_SCRIPT), *arguments]


def open_fake_with_proxy(install, proxy_host: str, proxy_port: int) -> int:
    """Faz o papel de discord.open_with_proxy para o Discord falso (mesma flag, mesmo spawn)."""
    return win.spawn_detached(
        install.exe_path, fake_arguments(install, discord.proxy_argument(proxy_host, proxy_port))
    )


def read_launches(root: str, version: str = "1.0.10") -> list[dict]:
    path = os.path.join(root, FAKE_FOLDER, f"app-{version}", "launches.log")
    try:
        with open(path, encoding="utf-8") as handle:
            lines = [line.rstrip("\r\n") for line in handle if line.strip()]
    except OSError:
        return []
    launches = []
    for line in lines:
        pid, arguments, elevated, leaked, token_ok, process_ok, objects_ok = (line.split("\t") + [""] * 7)[:7]
        launches.append({
            "pid": int(pid), "args": arguments, "elevated": elevated == "True", "leaked": leaked,
            "token_ok": token_ok == "True", "process_ok": process_ok == "True", "objects_ok": objects_ok == "True",
        })
    return launches


def kill_fakes() -> None:
    try:
        win.kill_by_name(FAKE_EXE)
    except OSError:
        pass


def reset_fakes(root: str) -> None:
    """Encerra os Discords falsos e zera o registro de aberturas (entre um teste e outro)."""
    kill_fakes()
    for version in ("1.0.4", "1.0.10"):
        try:
            os.remove(os.path.join(root, FAKE_FOLDER, f"app-{version}", "launches.log"))
        except OSError:
            pass
