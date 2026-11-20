"""Quais conexões do Discord vão pelo servidor dedicado.

É o mesmo critério que o próprio servidor aplica (server/dconnect_server.py); os
testes conferem que as duas cópias não divergem. O app decide a rota sozinho:
uma conexão que o servidor recusaria nem é enviada a ele.

Só passam domínios do próprio Discord, na porta 443. Ficam de fora, por exemplo:
- *.discordsays.com (Activities): é um proxy reverso que qualquer desenvolvedor
  aponta para onde quiser — serviria para usar o servidor com outros fins;
- storage.googleapis.com (envio de anexos): o servidor do Google atende qualquer
  bucket pelo cabeçalho Host, mesmo com o SNI conferido;
- integrações de terceiros (YouTube, Spotify, Twitch...).
Esses destinos saem direto da conexão do usuário, como sem o app.
"""
from __future__ import annotations

import re

# Sufixos com fronteira de ponto: "discord.com" vale para "cdn.discord.com", não para "evildiscord.com".
VPS_SUFFIXES = (
    "discord.com",
    "discord.gg",
    "discordapp.com",
    "discordapp.net",
    "discord.media",
    "hcaptcha.com",     # captcha do login: sair pelo mesmo IP da API evita captchas em laço
)
# Nomes sob esses domínios que apontam (CNAME) para plataformas de terceiros, onde o
# mesmo IP atende outros clientes. O servidor também recusa qualquer outro nome que
# passe a apontar para fora da lista.
VPS_EXCLUDED = frozenset({
    "support-dev.discord.com",      # Zendesk
    "creator-support.discord.com",  # Zendesk
    "support-apps.discord.com",     # Zendesk
    "status.hcaptcha.com",          # Atlassian Statuspage (CloudFront)
})
VPS_PORTS = frozenset({443})

_LABEL = rb"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rb"(?:" + _LABEL + rb"\.)+" + _LABEL)


def normalize_host(raw) -> str | None:
    """O nome de host validado, em minúsculas e sem o ponto final; None se for inválido.

    A validação é feita nos bytes, antes de qualquer decodificação: um NUL no meio
    do nome ("x.com\\x00.discord.com") passaria por uma comparação de sufixo e
    seria truncado pelo getaddrinfo, que resolveria outro host.
    """
    if isinstance(raw, str):
        try:
            raw = raw.encode("ascii")
        except UnicodeEncodeError:
            return None
    if not isinstance(raw, (bytes, bytearray)):
        return None
    raw = bytes(raw)
    if raw.endswith(b"."):
        raw = raw[:-1]
    if not 1 <= len(raw) <= 253:
        return None
    raw = raw.lower()
    if not _HOSTNAME.fullmatch(raw):
        return None
    name = raw.decode("ascii")
    if name.rsplit(".", 1)[1].isdigit():
        return None                 # um IP escrito como nome (127.0.0.1) não é um nome
    return name


def goes_through_vps(host, port: int) -> bool:
    """True se a conexão é com o Discord e deve sair pelo servidor dedicado."""
    name = normalize_host(host)
    if name is None or port not in VPS_PORTS or name in VPS_EXCLUDED:
        return False
    return any(name == suffix or name.endswith("." + suffix) for suffix in VPS_SUFFIXES)
