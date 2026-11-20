"""Lógica da interface com uma sessão de mentira: nenhuma conexão é feita.

A janela é criada e escondida em seguida; os cliques são simulados chamando os
próprios botões.
"""
from __future__ import annotations

import gc
import time
import tkinter as tk
import unittest

import fakes  # noqa: F401  (ajusta o sys.path)
from launcher import config, dconnect, session, ui

DEDICATED = dconnect.DedicatedServer("wss://dconnect.example.test/t/abcdef012345", "t" * 43)


class FakeSession:
    """Fica no lugar de SshSession/PublicSession e registra o que a interface pediu."""

    created: list["FakeSession"] = []

    def __init__(self, *args, on_status, on_finish, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.on_status = on_status
        self.on_finish = on_finish
        self.on_notice = kwargs.get("on_notice", lambda kind: None)
        self.started = False
        self.stop_requested = False
        self.abandoned = False
        FakeSession.created.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stop_requested = True

    def abandon(self):
        self.abandoned = True


class UiTest(unittest.TestCase):
    dedicated = DEDICATED           # não depende do dconnect.json desta máquina

    def setUp(self):
        original = config.load_dedicated
        config.load_dedicated = lambda: self.dedicated
        self.addCleanup(setattr, config, "load_dedicated", original)
        FakeSession.created = []
        for name in ("SshSession", "PublicSession"):
            original = getattr(session, name)
            setattr(session, name, FakeSession)
            self.addCleanup(setattr, session, name, original)
        self.app = ui.App()
        self.app.root.withdraw()
        self.addCleanup(self._destroy)

    def _destroy(self):
        if not self.app._destroyed:
            self.app._destroy()
        # As variáveis do Tk (StringVar...) precisam ser coletadas aqui, na thread
        # principal: coletadas depois, numa thread qualquer de outro teste, cada uma
        # trava aquela thread por ~1 s esperando um mainloop que não existe.
        self.app = None
        gc.collect()

    def pump(self, seconds: float = 0.25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.app._destroyed:
            self.app.root.update()
            time.sleep(0.01)

    # -- atalhos --

    def status(self):
        return self.app._status_label.cget("text")

    def dot(self):
        return self.app._status_dot.cget("fg")

    def buttons(self):
        """(Iniciar habilitado?, Parar habilitado?) da tela atual."""
        index = {"ssh": 0, "public": 1}[self.app._current]
        return (
            str(self.app._start_buttons[index]["state"]) == "normal",
            str(self.app._stop_buttons[index]["state"]) == "normal",
        )

    def click_start(self):
        index = {"ssh": 0, "public": 1}[self.app._current]
        self.app._start_buttons[index].invoke()
        self.pump(0.05)

    def click_stop(self):
        index = {"ssh": 0, "public": 1}[self.app._current]
        self.app._stop_buttons[index].invoke()
        self.pump(0.05)

    def visible_screen(self):
        return [name for name, frame in self.app._screens.items() if frame.winfo_manager()]

    # -- testes --

    def test_starts_on_the_mode_choice_disconnected(self):
        self.assertEqual(self.visible_screen(), ["home"])
        self.assertEqual(self.status(), "Desconectado")
        self.assertEqual(self.dot(), ui.STATUS_COLORS[session.WHITE])

    def test_navigation_between_screens(self):
        self.app._show("ssh")
        self.assertEqual(self.visible_screen(), ["ssh"])
        self.assertEqual(self.buttons(), (True, False))
        self.app._go_home()
        self.assertEqual(self.visible_screen(), ["home"])
        self.app._show("public")
        self.assertEqual(self.visible_screen(), ["public"])
        self.assertEqual(self.buttons(), (True, False))

    def test_ssh_form_defaults_and_required_host(self):
        self.app._show("ssh")
        self.assertEqual((self.app._port.get(), self.app._user.get(), self.app._password.get()), ("22", "root", ""))
        self.click_start()
        self.assertEqual(FakeSession.created, [])
        self.assertEqual(self.status(), "Erro: informe o IP ou host do servidor")
        self.assertEqual(self.dot(), ui.STATUS_COLORS[session.RED])
        self.assertEqual(self.buttons(), (True, False))

    def test_ssh_form_rejects_invalid_values(self):
        self.app._show("ssh")
        self.app._host.set("203.0.113.9")
        self.app._port.set("99999")
        self.click_start()
        self.assertEqual(FakeSession.created, [])
        self.assertEqual(self.status(), "Erro: a porta SSH deve ser um número entre 1 e 65535")

    def test_ssh_session_receives_the_form_values(self):
        self.app._show("ssh")
        self.app._host.set("  ana@vps.exemplo.com:2200 ")
        self.app._password.set("segredo")
        self.click_start()
        (created,) = FakeSession.created
        self.assertEqual(created.args, ("vps.exemplo.com", 2200, "ana", "segredo"))
        self.assertTrue(created.started)
        self.assertEqual(self.status(), "Conectando...")
        self.assertEqual(self.buttons(), (False, True))
        self.assertEqual(str(self.app._host_entry["state"]), "disabled")
        self.app._go_home()                              # com a conexão ativa não troca de tela
        self.assertEqual(self.visible_screen(), ["ssh"])

    def test_empty_password_means_key_login(self):
        self.app._show("ssh")
        self.app._host.set("203.0.113.9")
        self.click_start()
        self.assertEqual(FakeSession.created[0].args, ("203.0.113.9", 22, "root", None))

    def test_enter_key_starts_and_double_start_is_ignored(self):
        self.app._show("ssh")
        self.app._host.set("203.0.113.9")
        self.app._start_ssh()
        self.app._start_ssh()
        self.assertEqual(len(FakeSession.created), 1)

    def test_status_updates_come_through_the_queue(self):
        self.app._show("public")
        self.click_start()
        (created,) = FakeSession.created
        self.assertEqual(created.args, ())
        for color, text in (
            (session.YELLOW, "Testando proxies... (10/200) — 1 aprovado"),
            (session.GREEN, "Proxy ativo — 203.0.113.5:1080 (120ms)"),
            (session.ORANGE, "Instabilidade detectada — trocando proxy..."),
        ):
            created.on_status(color, text)
            self.pump()
            self.assertEqual((self.status(), self.dot()), (text, ui.STATUS_COLORS[color]))

    def test_stop_waits_for_the_session_and_shows_only_its_final_status(self):
        self.app._show("public")
        self.click_start()
        (created,) = FakeSession.created
        created.on_status(session.GREEN, "Proxy ativo — 203.0.113.5:1080 (120ms)")
        self.pump()
        self.click_stop()
        self.assertTrue(created.stop_requested)
        self.assertEqual(self.status(), "Encerrando...")
        self.assertEqual(self.buttons(), (False, False))
        created.on_status(session.GREEN, "Proxy ativo — atrasado")     # chegou tarde: não deve aparecer
        self.pump()
        self.assertEqual(self.status(), "Encerrando...")
        created.on_status(session.WHITE, "Desconectado")
        created.on_finish()
        self.pump()
        self.assertEqual((self.status(), self.dot()), ("Desconectado", ui.STATUS_COLORS[session.WHITE]))
        self.assertEqual(self.buttons(), (True, False))
        self.app._go_home()
        self.assertEqual(self.visible_screen(), ["home"])

    def test_error_keeps_the_message_and_allows_a_new_start(self):
        self.app._show("ssh")
        self.app._host.set("203.0.113.9")
        self.click_start()
        (created,) = FakeSession.created
        created.on_status(session.RED, "Erro: usuário ou senha SSH incorretos")
        created.on_finish()
        self.pump()
        self.assertEqual(self.status(), "Erro: usuário ou senha SSH incorretos")
        self.assertEqual(self.buttons(), (True, False))
        self.assertEqual(str(self.app._host_entry["state"]), "normal")
        self.click_start()
        self.assertEqual(len(FakeSession.created), 2)

    def test_closing_the_window_while_idle_just_closes(self):
        self.app._on_close()
        self.assertTrue(self.app._destroyed)

    def test_closing_while_connected_asks_first(self):
        answers = []
        original = ui.messagebox.askyesno
        ui.messagebox.askyesno = lambda *args, **kwargs: answers.append(args) or False
        self.addCleanup(setattr, ui.messagebox, "askyesno", original)
        self.app._show("public")
        self.click_start()
        (created,) = FakeSession.created
        self.app._on_close()                             # resposta: Não
        self.assertEqual(len(answers), 1)
        self.assertFalse(created.stop_requested)
        self.assertFalse(self.app._destroyed)

        ui.messagebox.askyesno = lambda *args, **kwargs: True
        self.app._on_close()                             # resposta: Sim
        self.assertTrue(created.stop_requested)
        self.assertFalse(self.app._destroyed)             # espera a sessão encerrar...
        self.assertEqual(self.app.root.state(), "withdrawn")   # ...com a janela já fora da tela
        self.app._on_close()                             # clicar no X de novo não faz nada
        created.on_status(session.WHITE, "Desconectado")
        created.on_finish()
        self.pump()
        self.assertTrue(self.app._destroyed)
        self.assertFalse(created.abandoned)               # encerrou sozinha: sem medida de emergência

    def test_closing_does_not_wait_forever_for_a_stuck_session(self):
        original = ui.messagebox.askyesno
        ui.messagebox.askyesno = lambda *args, **kwargs: True
        self.addCleanup(setattr, ui.messagebox, "askyesno", original)
        timeout = ui.FORCE_CLOSE_AFTER_MS
        ui.FORCE_CLOSE_AFTER_MS = 200
        self.addCleanup(setattr, ui, "FORCE_CLOSE_AFTER_MS", timeout)
        self.app._show("ssh")
        self.app._host.set("203.0.113.9")
        self.click_start()
        (created,) = FakeSession.created
        self.app._on_close()                             # a sessão nunca responde ao pedido de parar
        self.pump(0.6)
        self.assertTrue(created.abandoned)                # o túnel e o Discord são fechados daqui mesmo
        self.assertTrue(self.app._destroyed)

    def visible_notice(self):
        return [kind for kind, panel in self.app._notice_panels.items() if panel.winfo_manager()]

    def test_window_title_credits_the_author(self):
        self.assertEqual(self.app.root.title(), "Discord Proxy Launcher - By: pedrostyxx")
        self.assertEqual(config.APP_NAME, "Discord Proxy Launcher")   # mensagens continuam com o nome

    def test_public_session_gets_the_dedicated_server_and_the_notice_callback(self):
        self.app._show("public")
        self.click_start()
        (created,) = FakeSession.created
        self.assertIn("dedicated", created.kwargs)
        self.assertIn("on_notice", created.kwargs)

    def test_notice_panels_follow_the_session(self):
        self.app._show("public")
        self.assertEqual(self.visible_notice(), [None])              # o aviso das listas grátis
        self.click_start()
        (created,) = FakeSession.created
        created.on_notice("vps")                                      # vem da thread da sessão
        self.pump(0.2)
        self.assertEqual(self.visible_notice(), ["vps"])
        holder_height = self.app._notice_holder.winfo_height()
        created.on_notice("vps_update")
        self.pump(0.2)
        self.assertEqual(self.visible_notice(), ["vps_update"])
        self.assertEqual(self.app._notice_holder.winfo_height(), holder_height)   # o resto da tela não se mexe
        created.on_notice("vps")
        self.click_stop()
        created.on_finish()
        self.pump(0.2)
        self.assertEqual(self.visible_notice(), [None])              # parou: volta ao aviso padrão

    def test_panels_fit_with_the_buttons_visible(self):
        self.app._show("public")
        for kind in (None, "vps", "vps_update"):
            self.app._apply_notice(kind)
            self.pump(0.1)
            button = self.app._start_buttons[1]
            bottom = button.winfo_rooty() + button.winfo_height() - self.app._container.winfo_rooty()
            self.assertLessEqual(bottom, self.app._container.winfo_height(), kind)

    def test_link_opens_through_win_open_url(self):
        opened = []
        original = ui.win.open_url
        ui.win.open_url = opened.append
        self.addCleanup(setattr, ui.win, "open_url", original)
        self.app._open_link(config.INSTAGRAM_URL)
        deadline = time.monotonic() + 5
        while not opened and time.monotonic() < deadline:
            self.pump(0.05)
        self.assertEqual(opened, [config.INSTAGRAM_URL])

    def test_link_failure_copies_the_address(self):
        def broken(url):
            raise OSError("sem Explorer")

        original = ui.win.open_url
        ui.win.open_url = broken
        self.addCleanup(setattr, ui.win, "open_url", original)
        self.app._set_status(session.GREEN, "Proxy ativo — servidor dedicado")
        link = self.app._links[config.INSTAGRAM_URL]
        self.app._open_link(config.INSTAGRAM_URL)
        deadline = time.monotonic() + 5
        while "copiado" not in link.cget("text") and time.monotonic() < deadline:
            self.pump(0.05)
        self.assertIn("copiado", link.cget("text"))              # avisa no próprio link...
        self.assertEqual(self.status(), "Proxy ativo — servidor dedicado")    # ...sem mexer no status
        self.assertEqual(self.app.root.clipboard_get(), config.INSTAGRAM_URL)

    def test_update_notice_survives_the_end_of_the_session(self):
        self.app._show("public")
        self.click_start()
        (created,) = FakeSession.created
        created.on_notice("vps_update")
        created.on_finish()
        self.pump(0.2)
        self.assertEqual(self.visible_notice(), ["vps_update"])
        self.click_start()                                    # uma nova tentativa recomeça do aviso padrão
        self.assertEqual(self.visible_notice(), [None])

    def test_every_widget_was_built_without_tk_errors(self):
        for name in ("home", "ssh", "public"):
            self.app._show(name)
            self.pump(0.05)
        self.assertIsInstance(self.app.root, tk.Tk)
        self.assertGreater(self.app._container.winfo_reqheight(), 100)



class WithoutDedicatedServerTest(UiTest):
    """Build sem o servidor dedicado (forks, -NoDedicated): só o aviso das listas, sem espaço sobrando."""

    dedicated = None

    def test_only_the_free_list_warning_exists(self):
        self.assertEqual(list(self.app._notice_panels), [None])
        self.assertEqual(self.app._notice_holder.winfo_reqheight(), self.app._notice_panels[None].winfo_reqheight())

    # Os testes dos painéis do servidor dedicado não se aplicam aqui.
    test_notice_panels_follow_the_session = None
    test_link_opens_through_win_open_url = None
    test_link_failure_copies_the_address = None
    test_update_notice_survives_the_end_of_the_session = None
    test_panels_fit_with_the_buttons_visible = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
