"""Localiza, fecha e abre o Discord com `--proxy-server`."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import win
from .log import log

# (pasta em %LOCALAPPDATA%, executável): versão estável primeiro, depois PTB e Canary.
VARIANTS = (
    ("Discord", "Discord.exe"),
    ("DiscordPTB", "DiscordPTB.exe"),
    ("DiscordCanary", "DiscordCanary.exe"),
)
_APP_DIR = re.compile(r"^app-(\d+(?:\.\d+)*)$")


@dataclass(frozen=True)
class DiscordInstall:
    exe_path: str
    exe_name: str
    version: str


def find_install(local_app_data: str | None = None, variants=VARIANTS) -> DiscordInstall | None:
    """Acha o Discord.exe da versão mais recente instalada.

    O instalador mantém uma pasta por versão: %LOCALAPPDATA%\\Discord\\app-X.X.XXX.
    """
    root = local_app_data or win.user_dirs()[0]
    for folder, exe_name in variants:
        base = os.path.join(root, folder)
        try:
            names = os.listdir(base)
        except OSError:
            continue
        versions = []
        for name in names:
            match = _APP_DIR.match(name)
            if match:
                versions.append((tuple(int(part) for part in match.group(1).split(".")), name))
        # A pasta mais nova pode estar incompleta no meio de uma atualização;
        # nesse caso vale a mais recente que já tem o executável.
        for _, name in sorted(versions, reverse=True):
            exe_path = os.path.join(base, name, exe_name)
            if os.path.isfile(exe_path):
                return DiscordInstall(exe_path, exe_name, name[len("app-"):])
    return None


def close(install: DiscordInstall) -> int:
    """Encerra todos os processos do Discord. Devolve quantos foram encerrados."""
    killed = win.kill_by_name(install.exe_name)
    log.info("Discord encerrado (%d processos)", killed)
    return killed


def is_running(install: DiscordInstall) -> bool:
    return bool(win.pids_by_name(install.exe_name))


def proxy_argument(proxy_host: str, proxy_port: int) -> str:
    """A flag do Chromium que faz o Discord (Electron) sair pelo proxy SOCKS5."""
    return f"--proxy-server=socks5://{proxy_host}:{proxy_port}"


def open_with_proxy(install: DiscordInstall, proxy_host: str, proxy_port: int) -> int:
    """Abre o Discord apontando para o proxy SOCKS5; devolve o PID."""
    argument = proxy_argument(proxy_host, proxy_port)
    pid = win.spawn_detached(install.exe_path, [argument])
    log.info("Discord %s iniciado (pid %d) com %s", install.version, pid, argument)
    return pid
