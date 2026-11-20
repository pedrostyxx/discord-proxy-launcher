"""Log de diagnóstico, desligado por padrão.

O app não grava nada em disco. Para investigar um problema, abra o app com
`--log` (ou defina a variável de ambiente DPL_DEBUG_LOG=1): ele passa a escrever
`discord-proxy-launcher.log` na mesma pasta do executável. Senhas nunca são
registradas. A opção de linha de comando existe porque o UAC não repassa as
variáveis de ambiente de quem abriu o app para o processo elevado.

O caminho do arquivo é fixo de propósito: o app roda como administrador e não
deve escrever em um caminho escolhido por uma variável de ambiente.
"""
from __future__ import annotations

import logging
import os
import sys

LOG_FILE_NAME = "discord-proxy-launcher.log"

log = logging.getLogger("dpl")
log.addHandler(logging.NullHandler())
log.propagate = False


def log_path() -> str:
    if getattr(sys, "frozen", False):
        folder = os.path.dirname(sys.executable)
    else:
        folder = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(folder, LOG_FILE_NAME)


def setup() -> None:
    # O Paramiko registra erros de rede por conta própria; sem um handler eles
    # iriam para o stderr, que não existe em um app sem console.
    paramiko_log = logging.getLogger("paramiko")
    paramiko_log.addHandler(logging.NullHandler())
    paramiko_log.propagate = False

    wanted = "--log" in sys.argv[1:] or os.environ.get("DPL_DEBUG_LOG", "").strip().lower() not in (
        "", "0", "false", "no", "nao", "não",
    )
    if not wanted:
        return
    try:
        handler = logging.FileHandler(log_path(), encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(threadName)s %(name)s: %(message)s"))
    for logger in (log, paramiko_log):
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    paramiko_log.setLevel(logging.INFO)
