"""Confere o executável compilado: python tests/check_exe.py [caminho_do_exe]

Roda o autoteste embutido (`--selftest`) e abre a janela por um instante.
O app é iniciado SEM elevação (RunAsInvoker), então o UAC não aparece; nada
é conectado e o Discord não é tocado.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TITLE = "Discord Proxy Launcher"                        # caixas de mensagem
WINDOW_TITLE = "Discord Proxy Launcher - By: pedrostyxx"  # a janela principal
WM_CLOSE = 0x0010


def main() -> int:
    exe = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist", "discord-proxy-launcher", "discord-proxy-launcher.exe"))
    env = dict(os.environ, __COMPAT_LAYER="RunAsInvoker")
    env.pop("DPL_DEBUG_LOG", None)

    report_path = os.path.join(tempfile.mkdtemp(prefix="dpl-exe-"), "selftest.json")
    try:
        code = subprocess.run([exe, "--selftest", report_path], env=env, timeout=180).returncode
    except OSError as exc:
        # 4551 = bloqueado pelo Smart App Control / política de Controle de Aplicativo
        print(f"NÃO EXECUTOU: {exc}")
        return 2
    with open(report_path, encoding="utf-8") as handle:
        report = json.load(handle)
    for name, check in report["checks"].items():
        print(f"  {'ok ' if check['ok'] else 'ERRO'} {name}: {check.get('info', check.get('erro'))}")
    print(f"autoteste: código {code}, empacotado={report['empacotado']}")
    if code != 0 or not report["ok"]:
        return 1

    user32 = ctypes.WinDLL("user32")
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]

    if user32.FindWindowW("TkTopLevel", WINDOW_TITLE) or user32.FindWindowW("TkTopLevel", TITLE):
        # Um launcher do usuário já está aberto: a instância única não deixaria
        # esta abrir, e o WM_CLOSE abaixo iria para a janela dele.
        print("janela: NÃO CONFERIDA — já há um Discord Proxy Launcher aberto; feche-o e rode de novo")
        return 3
    started = time.monotonic()
    app = subprocess.Popen([exe], env=env)
    window = None
    while time.monotonic() - started < 20:
        window = user32.FindWindowW("TkTopLevel", WINDOW_TITLE)
        if window and user32.IsWindowVisible(window):
            break
        time.sleep(0.1)
    else:
        app.kill()
        print("a janela não apareceu")
        return 1
    print(f"janela aberta em {time.monotonic() - started:.1f}s")

    # Uma segunda instância deve só avisar que o app já está aberto.
    second = subprocess.Popen([exe], env=env)
    notice = None
    for _ in range(100):
        notice = user32.FindWindowW("#32770", TITLE)
        if notice:
            break
        time.sleep(0.1)
    if notice:
        user32.PostMessageW(notice, WM_CLOSE, 0, 0)
    print(f"instância única: aviso {'apareceu' if notice else 'NÃO apareceu'}, saída {second.wait(15)}")

    user32.PostMessageW(window, WM_CLOSE, 0, 0)
    print(f"fechou com código {app.wait(15)}")
    return 0 if notice else 1


if __name__ == "__main__":
    sys.exit(main())
