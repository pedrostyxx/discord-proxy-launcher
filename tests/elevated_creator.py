"""Ajuda dos testes: abre o Discord falso como o launcher elevado abriria.

Uso: python elevated_creator.py <raiz_do_projeto> <exe_do_discord_falso> <modo> [--sem-correcao]
  modo: pelo-explorer   pedindo ao Explorer que abra (o caminho normal do launcher elevado)
        explorer        como filho do Explorer (o primeiro plano B)
        explorer-sddl   idem, sem acesso ao token do Explorer (vale o DACL montado a partir do SID)
        limitado        com o token rebaixado do próprio processo (o último plano B)

Antes de abrir, troca o DACL padrão deste processo por "só Administradores e
SYSTEM" — o DACL padrão de um processo elevado. É assim que dá para reproduzir,
sem o UAC, o que acontece quando o launcher roda como administrador.
Roda em um processo separado para não mexer no token de quem chamou. Imprime o
PID aberto e, na linha seguinte, se o DACL padrão deste processo foi restaurado.
"""
import contextlib
import ctypes
import os
import sys

root, exe, mode = sys.argv[1], sys.argv[2], sys.argv[3]
without_fix = "--sem-correcao" in sys.argv[4:]
sys.path.insert(0, root)

from launcher import win  # noqa: E402


def use_elevated_default_dacl() -> None:
    descriptor = win.LPVOID()
    if not win.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "D:(A;;GA;;;BA)(A;;GA;;;SY)", win.SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    present, defaulted, acl = win.BOOL(), win.BOOL(), win.LPVOID()
    win.advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted))
    token = win.HANDLE()
    if not win.advapi32.OpenProcessToken(
        win.kernel32.GetCurrentProcess(), win.TOKEN_ADJUST_DEFAULT | win.TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    win._set_token_default_dacl(token, acl.value)


def own_default_dacl() -> bytes:
    """Os bytes do DACL padrão atual deste processo."""
    token = win.HANDLE()
    if not win.advapi32.OpenProcessToken(win.kernel32.GetCurrentProcess(), win.TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = win._token_default_dacl(token)
        acl = win.TOKEN_DEFAULT_DACL.from_buffer(buffer).DefaultDacl
        size = ctypes.c_ushort.from_address(acl + 2).value          # ACL.AclSize
        return ctypes.string_at(acl, size)
    finally:
        win.kernel32.CloseHandle(token)


use_elevated_default_dacl()
installed = own_default_dacl()
if without_fix:
    win._creator_default_dacl = lambda acl: contextlib.nullcontext()
if mode == "explorer-sddl":
    def no_explorer_token(*args):
        raise OSError("sem acesso ao token do Explorer (simulado)")
    win._shell_token = no_explorer_token

arguments = [os.path.join(os.path.dirname(exe), "fakechat.py"), "--proxy-server=socks5://127.0.0.1:9"]
if mode == "limitado":
    pid = win.spawn_with_limited_token(exe, arguments)
elif mode == "pelo-explorer":
    pid = win.spawn_via_explorer(exe, arguments)
else:
    pid = win.spawn_as_desktop_user(exe, arguments)
print(pid)
# Depois de abrir o programa, o launcher precisa ter voltado ao próprio DACL.
print(own_default_dacl() == installed)
