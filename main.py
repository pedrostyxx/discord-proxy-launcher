"""Discord Proxy Launcher — ponto de entrada."""
import os
import sys


def main() -> int:
    # Em um executável sem console, stdout e stderr não existem.
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))

    from launcher import log

    log.setup()

    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        from launcher import selftest

        return selftest.run(sys.argv[2] if len(sys.argv) > 2 else None)

    from launcher import config, win

    win.set_dpi_awareness()
    if not win.acquire_single_instance("DiscordProxyLauncher"):
        win.user32.MessageBoxW(None, f"O {config.APP_NAME} já está aberto.", config.APP_NAME, 0x40)
        return 0

    from launcher.ui import App

    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
