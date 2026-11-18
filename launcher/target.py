"""Validação dos campos do Modo A (host, porta e usuário SSH)."""
from __future__ import annotations

import ipaddress
import re

from . import config

# Nada que comece com "-" chega ao ssh.exe: seria lido como uma opção de linha
# de comando, não como um endereço.
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")
_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.$@-]{0,63}$")
_HOST_WITH_PORT = re.compile(r"^([^:]+):(\d{1,5})$")
_BRACKETED = re.compile(r"^\[([^\]]+)\](?::(\d{1,5}))?$")


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def parse_target(host: str, port: str = "", user: str = "") -> tuple[str, int, str]:
    """Devolve (host, porta, usuário) ou lança ValueError com a mensagem para o usuário.

    Aceita também as formas coladas de um terminal: `usuario@host`, `host:porta`
    e `[ipv6]:porta`.
    """
    host = host.strip()
    user = user.strip() or config.SSH_DEFAULT_USER
    port = port.strip() or str(config.SSH_DEFAULT_PORT)
    if not host:
        raise ValueError("informe o IP ou host do servidor")
    if "@" in host:
        inline_user, host = host.rsplit("@", 1)
        user = inline_user.strip() or user
    bracketed = _BRACKETED.match(host)
    if bracketed:                   # [2001:db8::1] ou [2001:db8::1]:2222
        host = bracketed.group(1)
        port = bracketed.group(2) or port
    else:
        with_port = _HOST_WITH_PORT.match(host)
        if with_port:               # um IPv6 puro tem vários ":" e não cai aqui
            host, port = with_port.group(1), with_port.group(2)
    if not _HOST.match(host):
        raise ValueError("o IP ou host do servidor é inválido")
    if not _is_ip(host):
        # Nome de host: sem ":" e com rótulos de 1 a 63 caracteres (um "a..b"
        # ou um rótulo gigante nem chega a ser resolvido pelo DNS).
        if ":" in host or any(not 0 < len(label) < 64 for label in host.rstrip(".").split(".")):
            raise ValueError("o IP ou host do servidor é inválido")
    if not _USER.match(user):
        raise ValueError("o usuário SSH é inválido")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("a porta SSH deve ser um número entre 1 e 65535")
    return host, int(port), user
