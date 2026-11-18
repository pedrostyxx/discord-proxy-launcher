"""Autoteste do executável: `discord-proxy-launcher.exe --selftest resultado.json`.

Confere que tudo o que foi empacotado carrega e funciona. Não abre janela, não
toca no Discord e não acessa a rede.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading


def _tkinter() -> str:
    import tkinter as tk

    root = tk.Tk()
    try:
        root.withdraw()
        return f"Tk {root.tk.call('info', 'patchlevel')}"
    finally:
        root.destroy()


def _tls() -> str:
    import certifi

    from . import proxies

    context = proxies.tls_context()
    if not os.path.isfile(certifi.where()):
        raise FileNotFoundError(certifi.where())
    return f"{len(context.get_ca_certs())} autoridades certificadoras"


def _paramiko_loopback() -> str:
    """Handshake SSH completo dentro do próprio processo (exercita a criptografia)."""
    import paramiko

    class Server(paramiko.ServerInterface):
        def get_allowed_auths(self, username):
            return "password"

        def check_auth_password(self, username, password):
            good = (username, password) == ("teste", "segredo")
            return paramiko.AUTH_SUCCESSFUL if good else paramiko.AUTH_FAILED

    left, right = socket.socketpair()
    server = paramiko.Transport(left)
    client = paramiko.Transport(right)
    try:
        server.add_server_key(paramiko.ECDSAKey.generate())
        server.start_server(event=threading.Event(), server=Server())
        client.start_client(timeout=15)
        client.auth_password("teste", "segredo")
        if not client.is_authenticated():
            raise RuntimeError("login não concluído")
        return f"paramiko {paramiko.__version__}, cifra {client.remote_cipher}"
    finally:
        client.close()
        server.close()


def _windows_api() -> dict:
    from . import win

    local_app_data, profile = win.user_dirs()
    token = win._limited_token()                # o token usado para abrir o Discord sem elevação
    win.kernel32.CloseHandle(token)
    return {
        "elevado": win.is_elevated(),
        "outra_conta": win.desktop_user_is_other_account(),
        "processos": len(win.list_processes()),
        "porta_135_tem_dono": bool(win.tcp_listener_pids(135)),
        "pastas_do_usuario_existem": os.path.isdir(local_app_data) and os.path.isdir(profile),
        "token_rebaixado": bool(token),
    }


def _discord() -> str:
    from . import discord

    install = discord.find_install()
    return f"{install.exe_name} {install.version}" if install else "não instalado"


def _ssh_exe() -> str:
    from . import tunnel, win

    path = tunnel.find_ssh_exe()
    if not path:
        return "ssh.exe ausente (o app usa o cliente SSH embutido)"
    with win.default_dll_directory():
        done = subprocess.run(
            [path, "-V"], capture_output=True, timeout=15,
            env=win.clean_environment(), creationflags=win.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
        )
    return (done.stderr or done.stdout).decode("utf-8", "replace").strip()


def _icon() -> str:
    from . import ui

    path = ui.resource_path(os.path.join("assets", "icon.ico"))
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    return f"{os.path.getsize(path)} bytes"


def run(output_path: str | None) -> int:
    checks = {}
    for name, function in (
        ("tkinter", _tkinter),
        ("tls", _tls),
        ("paramiko", _paramiko_loopback),
        ("windows_api", _windows_api),
        ("discord", _discord),
        ("ssh_exe", _ssh_exe),
        ("icone", _icon),
    ):
        try:
            checks[name] = {"ok": True, "info": function()}
        except Exception as exc:
            checks[name] = {"ok": False, "erro": f"{type(exc).__name__}: {exc}"}
    report = {
        "ok": all(check["ok"] for check in checks.values()),
        "python": sys.version.split()[0],
        "empacotado": bool(getattr(sys, "frozen", False)),
        "checks": checks,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text)
    return 0 if report["ok"] else 1
