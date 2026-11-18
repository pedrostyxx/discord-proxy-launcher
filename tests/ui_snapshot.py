"""Gera imagens de cada tela do app para conferência visual (ferramenta de desenvolvimento).

Uso: python tests/ui_snapshot.py <pasta_de_saida> [escala, ex.: 1.5]
A janela aparece por alguns segundos no canto da tela; nenhuma conexão é iniciada. Requer Pillow.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import ImageGrab  # noqa: E402

from launcher import session, ui, win  # noqa: E402


def main() -> None:
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    win.set_dpi_awareness()
    if len(sys.argv) > 2:
        # Simula outra escala de tela (ex.: 1.5 = 150%) para conferir o layout.
        factor = float(sys.argv[2])
        real_tk = ui.tk.Tk

        class ScaledTk(real_tk):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.tk.call("tk", "scaling", factor * 96 / 72)

        ui.tk.Tk = ScaledTk
    app = ui.App()
    root = app.root
    # O Tk só desenha a janela quando ela está visível, então ela aparece por
    # alguns segundos no canto da tela, por cima das outras, enquanto é fotografada.
    root.geometry("+40+40")
    root.attributes("-topmost", True)
    hwnd = int(root.frame(), 16)
    root.focus_force()

    def bounds() -> tuple[int, int, int, int]:
        """Contorno visível da janela, com a barra de título e sem a sombra."""
        rect = ctypes.wintypes.RECT()
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)   # DWMWA_EXTENDED_FRAME_BOUNDS
        )
        return rect.left, rect.top, rect.right, rect.bottom

    def shot(name: str) -> None:
        for _ in range(4):
            root.update()
            time.sleep(0.05)
        ImageGrab.grab(bbox=bounds(), all_screens=True).save(os.path.join(out, f"{name}.png"))

    shot("1-inicio")

    app._show("ssh")
    app._host.set("203.0.113.25")
    app._password.set("segredo123")
    shot("2-meu-servidor")

    app._set_running(True)
    app._set_status(session.GREEN, "Proxy ativo — 203.0.113.25:22 (48ms)")
    shot("3-meu-servidor-ativo")

    app._set_running(False)
    app._show("public")
    app._set_status(session.WHITE, "Desconectado")
    shot("4-proxy-publico")

    app._set_running(True)
    app._set_status(session.YELLOW, "Testando proxies... (87/1114) — 4 aprovados")
    shot("5-proxy-publico-testando")

    app._set_status(session.GREEN, "Proxy ativo — 203.0.113.77:1080 (412ms)")
    shot("6-proxy-publico-ativo")

    app._set_status(session.ORANGE, "Instabilidade detectada — trocando proxy...")
    shot("7-proxy-publico-instavel")

    app._set_running(False)
    app._show("ssh")
    app._set_status(session.RED, "Erro: a chave do servidor mudou — se você o reinstalou, rode: ssh-keygen -R 203.0.113.25")
    shot("8-meu-servidor-erro")

    app._set_running(True)                      # como no app: "Conectando..." já trava os campos e o Iniciar
    app._set_status(session.BLUE, "Conectando...")
    shot("9-conectando")
    app._destroy()
    print("scale", app._scale)


if __name__ == "__main__":
    main()
