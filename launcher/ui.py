"""Interface (tkinter): tela inicial, os dois modos e a linha de status."""
from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox

from . import config, session, win
from .log import log
from .target import parse_target

BG = "#1e1f22"
SURFACE = "#2b2d31"
SURFACE_HOVER = "#35373c"
BORDER = "#3f4147"
TEXT = "#f2f3f5"
MUTED = "#b5bac1"
FAINT = "#80848e"
ACCENT = "#5865f2"
ACCENT_HOVER = "#4752c4"
NEUTRAL = "#4e5058"
NEUTRAL_HOVER = "#6d6f78"
DISABLED = "#383a40"
WARNING = "#f0b232"
WARNING_BG = "#352d1c"
SUCCESS = "#3ba55c"
SUCCESS_BG = "#1b2f22"
LINK = "#00a8fc"
VPS_CALL_NOTE = (
    "Numa ligação, cada pessoa se conecta ao Discord pela própria internet: para que tudo funcione "
    "para todos, quem também estiver com o Discord bloqueado ou instável precisa estar com este app aberto."
)
STATUS_COLORS = {
    session.BLUE: "#3b9eff",
    session.YELLOW: "#f5d90a",
    session.GREEN: "#23a55a",
    session.ORANGE: "#ff8c1a",
    session.RED: "#f23f43",
    session.WHITE: "#e3e5e8",
}
FONT = "Segoe UI"
CONTENT_WIDTH = 400
FORCE_CLOSE_AFTER_MS = 20000    # espera máxima pelo encerramento da sessão ao fechar a janela


def resource_path(relative: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, relative)


class App:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(config.WINDOW_TITLE)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(default=resource_path(os.path.join("assets", "icon.ico")))
        except tk.TclError:
            pass
        self._scale = self.root.winfo_fpixels("1i") / 96.0

        self._events: queue.Queue = queue.Queue()
        self._session: session.Session | None = None
        self._stopping = False
        self._closing = False
        self._destroyed = False
        self._pending_status: tuple[str, str] | None = None
        self._start_buttons: list[tk.Button] = []
        self._stop_buttons: list[tk.Button] = []
        self._locked_while_running: list[tk.Widget] = []
        self._notice_panels: dict[str | None, tk.Frame] = {}
        self._notice: str | None = None
        self._links: dict[str, tk.Label] = {}
        self._opening_link = False
        self._dedicated = config.load_dedicated()

        self._build()
        self._show("home")
        self._set_running(False)
        self._set_status(session.WHITE, "Desconectado")
        self._center()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.deiconify()
        self._timers = {"drain": self.root.after(60, self._drain_events)}

    def run(self) -> None:
        self.root.mainloop()

    def px(self, value: float) -> int:
        return int(round(value * self._scale))

    # -- construção --

    def _build(self) -> None:
        pad = self.px(22)
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=pad, pady=(self.px(20), self.px(14)))
        tk.Label(header, text=config.APP_NAME, font=(FONT, 15, "bold"), fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(
            header, text="Abre o Discord através de um proxy SOCKS5",
            font=(FONT, 9), fg=MUTED, bg=BG,
        ).pack(anchor="w")

        self._container = tk.Frame(self.root, bg=BG)
        self._container.pack(fill="both", expand=True, padx=pad)
        self._screens = {
            "home": self._build_home(self._container),
            "ssh": self._build_ssh(self._container),
            "public": self._build_public(self._container),
        }
        # Todas as telas ocupam o mesmo espaço: a janela não muda de tamanho.
        for frame in self._screens.values():
            frame.grid(row=0, column=0, sticky="nsew")
        self.root.update_idletasks()
        # Medido com todos os avisos do modo público no mesmo lugar (vale a altura do
        # maior); depois o lugar fica com essa altura e só o aviso do momento aparece.
        self._notice_holder.configure(height=self._notice_holder.winfo_reqheight())
        self._notice_holder.grid_propagate(False)
        height = max(frame.winfo_reqheight() for frame in self._screens.values())
        self._container.configure(width=self.px(CONTENT_WIDTH), height=height)
        self._container.grid_propagate(False)
        self._container.grid_rowconfigure(0, weight=1)
        self._container.grid_columnconfigure(0, weight=1)
        self._apply_notice(None)

        bar = tk.Frame(self.root, bg=SURFACE)
        bar.pack(fill="x", side="bottom", pady=(self.px(16), 0))
        self._status_dot = tk.Label(bar, text="●", font=(FONT, 13), bg=SURFACE, fg=STATUS_COLORS[session.WHITE])
        self._status_dot.pack(side="left", padx=(pad, self.px(8)), pady=self.px(10))
        self._status_label = tk.Label(
            bar, text="", font=(FONT, 10), fg=TEXT, bg=SURFACE, anchor="w", justify="left",
            height=2, wraplength=self.px(CONTENT_WIDTH - 24),
        )
        self._status_label.pack(side="left", fill="x", expand=True, padx=(0, pad), pady=self.px(8))

    def _build_home(self, parent: tk.Frame) -> tk.Frame:
        frame = tk.Frame(parent, bg=BG)
        tk.Label(frame, text="Como você quer conectar?", font=(FONT, 10), fg=MUTED, bg=BG).pack(
            anchor="w", pady=(0, self.px(10))
        )
        # Os dois cartões dividem toda a altura disponível: botões grandes, bem visíveis.
        self._card(
            frame, "\U0001F5A5", "Meu servidor", "Tenho um VPS ou servidor SSH",
            lambda: self._show("ssh"),
        ).pack(fill="both", expand=True, pady=(0, self.px(12)))
        public_subtitle = (
            "Não tenho servidor: usar o servidor grátis do app" if self._dedicated
            else "Não tenho servidor, usar lista grátis"
        )
        self._card(
            frame, "\U0001F310", "Proxy público", public_subtitle,
            lambda: self._show("public"),
        ).pack(fill="both", expand=True)
        return frame

    def _card(self, parent, icon: str, title: str, subtitle: str, command) -> tk.Frame:
        card = tk.Frame(
            parent, bg=SURFACE, cursor="hand2", takefocus=1,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        icon_label = tk.Label(card, text=icon, font=("Segoe UI Emoji", 22), bg=SURFACE, fg=TEXT)
        icon_label.pack(side="left", padx=(self.px(18), self.px(14)), pady=self.px(18))
        texts = tk.Frame(card, bg=SURFACE)
        texts.pack(side="left", fill="x", expand=True, pady=self.px(18))
        title_label = tk.Label(texts, text=title, font=(FONT, 13, "bold"), bg=SURFACE, fg=TEXT, anchor="w")
        title_label.pack(fill="x")
        subtitle_label = tk.Label(texts, text=subtitle, font=(FONT, 9), bg=SURFACE, fg=MUTED, anchor="w")
        subtitle_label.pack(fill="x")
        arrow = tk.Label(card, text="›", font=(FONT, 18), bg=SURFACE, fg=FAINT)
        arrow.pack(side="right", padx=self.px(16))
        parts = (card, icon_label, texts, title_label, subtitle_label, arrow)

        def paint(color: str) -> None:
            for part in parts:
                part.configure(bg=color)

        for part in parts:
            part.configure(cursor="hand2")
            part.bind("<Enter>", lambda _event: paint(SURFACE_HOVER))
            part.bind("<Leave>", lambda _event: paint(SURFACE))
            part.bind("<Button-1>", lambda _event: command())
        card.bind("<Return>", lambda _event: command())
        card.bind("<space>", lambda _event: command())
        return card

    def _screen_header(self, frame: tk.Frame, title: str) -> None:
        back = tk.Label(frame, text="‹ Voltar", font=(FONT, 9), fg=MUTED, bg=BG, cursor="hand2")
        back.pack(anchor="w")
        back.bind("<Button-1>", lambda _event: self._go_home())
        back.bind("<Enter>", lambda _event: back.configure(fg=TEXT) if self._session is None else None)
        back.bind("<Leave>", lambda _event: back.configure(fg=MUTED))
        self._locked_while_running.append(back)
        tk.Label(frame, text=title, font=(FONT, 13, "bold"), fg=TEXT, bg=BG).pack(
            anchor="w", pady=(self.px(6), self.px(10))
        )

    def _build_ssh(self, parent: tk.Frame) -> tk.Frame:
        frame = tk.Frame(parent, bg=BG)
        self._screen_header(frame, "Meu servidor")

        form = tk.Frame(frame, bg=BG)
        form.pack(fill="x")
        form.grid_columnconfigure(0, weight=1, uniform="half")
        form.grid_columnconfigure(1, weight=1, uniform="half")
        self._host = tk.StringVar()
        self._port = tk.StringVar(value=str(config.SSH_DEFAULT_PORT))
        self._user = tk.StringVar(value=config.SSH_DEFAULT_USER)
        self._password = tk.StringVar()
        gap = self.px(10)
        self._host_entry = self._field(form, "IP / Host do servidor", self._host, row=0, column=0, columnspan=2)
        self._field(form, "Porta SSH", self._port, row=2, column=0, padx=(0, gap // 2))
        self._field(form, "Usuário SSH", self._user, row=2, column=1, padx=(gap // 2, 0))
        self._field(form, "Senha SSH", self._password, row=4, column=0, columnspan=2, show="•")
        tk.Label(
            form, text="Deixe a senha em branco para usar sua chave SSH (.ssh\\id_rsa).",
            font=(FONT, 8), fg=FAINT, bg=BG,
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(self.px(4), 0))

        self._buttons(frame, self._start_ssh)
        return frame

    def _field(self, form, label: str, variable: tk.StringVar, row: int, column: int,
               columnspan: int = 1, padx=(0, 0), show: str = "") -> tk.Entry:
        tk.Label(form, text=label, font=(FONT, 9), fg=MUTED, bg=BG).grid(
            row=row, column=column, columnspan=columnspan, sticky="w", padx=padx,
            pady=(self.px(8) if row else 0, self.px(3)),
        )
        entry = tk.Entry(
            form, textvariable=variable, show=show, font=(FONT, 10),
            bg=SURFACE, fg=TEXT, insertbackground=TEXT, relief="flat",
            disabledbackground=DISABLED, disabledforeground=MUTED,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        entry.grid(row=row + 1, column=column, columnspan=columnspan, sticky="ew", padx=padx, ipady=self.px(5))
        entry.bind("<Return>", lambda _event: self._start_ssh())
        self._locked_while_running.append(entry)
        return entry

    def _build_public(self, parent: tk.Frame) -> tk.Frame:
        frame = tk.Frame(parent, bg=BG)
        self._screen_header(frame, "Proxy público")

        # Os avisos ocupam o mesmo lugar e a altura do maior deles: trocar de aviso
        # não mexe no resto da tela, e a janela já nasce com o tamanho certo.
        holder = tk.Frame(frame, bg=BG)
        holder.pack(fill="x")
        holder.grid_columnconfigure(0, weight=1)
        self._notice_panels = {None: self._warning_panel(holder)}
        if self._dedicated:             # sem servidor dedicado neste build, esses avisos nunca aparecem
            self._notice_panels["vps"] = self._vps_panel(holder)
            self._notice_panels["vps_update"] = self._update_panel(holder)
        for panel in self._notice_panels.values():
            panel.grid(row=0, column=0, sticky="new")
        self._notice_holder = holder

        description = (
            "Nada para preencher: o app usa o servidor dedicado gratuito e, se ele estiver fora do ar, "
            "testa os proxies públicos e usa o mais rápido."
            if self._dedicated else
            "Nada para preencher: o app baixa a lista, testa os proxies e usa o mais rápido."
        )
        tk.Label(
            frame, text=description,
            font=(FONT, 9), fg=MUTED, bg=BG, justify="left", anchor="w",
            wraplength=self.px(CONTENT_WIDTH),
        ).pack(fill="x", pady=(self.px(12), 0))

        self._buttons(frame, self._start_public)
        return frame

    def _panel(self, parent, accent: str, background: str) -> tuple[tk.Frame, tk.Frame]:
        panel = tk.Frame(parent, bg=background)
        tk.Frame(panel, bg=accent, width=self.px(4)).pack(side="left", fill="y")
        body = tk.Frame(panel, bg=background)
        body.pack(side="left", fill="both", expand=True, padx=self.px(12), pady=self.px(10))
        return panel, body

    def _text(self, body, text: str, background: str, size: int = 9, color: str = TEXT, top: int = 4) -> None:
        tk.Label(
            body, text=text, font=(FONT, size), fg=color, bg=background, justify="left", anchor="w",
            wraplength=self.px(CONTENT_WIDTH - 44),
        ).pack(fill="x", pady=(self.px(top), 0))

    def _link(self, body, text: str, url: str, background: str) -> None:
        link = tk.Label(body, text=text, font=(FONT, 9, "bold", "underline"), fg=LINK, bg=background,
                        cursor="hand2", anchor="w")
        link.pack(fill="x", pady=(self.px(2), 0))
        link.bind("<Button-1>", lambda _event: self._open_link(url))
        link.original_text = text
        self._links[url] = link

    def _warning_panel(self, parent) -> tk.Frame:
        panel, body = self._panel(parent, WARNING, WARNING_BG)
        tk.Label(
            body, text="⚠  Proxies gratuitos podem ser instáveis.",
            font=(FONT, 10, "bold"), fg=WARNING, bg=WARNING_BG, anchor="w",
        ).pack(fill="x")
        self._text(body, (
            "O app monitora a conexão e troca de proxy automaticamente se detectar "
            "queda, mas você pode perceber uma breve interrupção no Discord durante a troca."
        ), WARNING_BG)
        return panel

    def _vps_panel(self, parent) -> tk.Frame:
        panel, body = self._panel(parent, SUCCESS, SUCCESS_BG)
        tk.Label(
            body, text=f"✓  Servidor dedicado de {config.DEDICATED_OWNER}",
            font=(FONT, 10, "bold"), fg=SUCCESS, bg=SUCCESS_BG, anchor="w",
        ).pack(fill="x")
        self._text(body, (
            f"Você está usando um servidor dedicado fornecido gratuitamente por {config.DEDICATED_OWNER}. "
            "Como agradecimento, siga no Instagram:"
        ), SUCCESS_BG)
        self._link(body, f"@{config.DEDICATED_OWNER}  ↗", config.INSTAGRAM_URL, SUCCESS_BG)
        self._text(body, VPS_CALL_NOTE, SUCCESS_BG, size=8, color=MUTED, top=6)
        return panel

    def _update_panel(self, parent) -> tk.Frame:
        panel, body = self._panel(parent, WARNING, WARNING_BG)
        tk.Label(
            body, text="⚠  Atualize o app para usar o servidor dedicado",
            font=(FONT, 10, "bold"), fg=WARNING, bg=WARNING_BG, anchor="w",
        ).pack(fill="x")
        self._text(body, (
            "O servidor dedicado não aceita mais esta versão do app. Enquanto isso, ele usa os proxies "
            "públicos. Baixe a versão nova:"
        ), WARNING_BG)
        self._link(body, "Página de downloads  ↗", config.RELEASES_URL, WARNING_BG)
        return panel

    def _apply_notice(self, kind: str | None) -> None:
        kind = kind if kind in self._notice_panels else None
        self._notice = kind
        for key, panel in self._notice_panels.items():
            if key == kind:
                panel.grid()
            else:
                panel.grid_remove()

    def _copied(self, url: str) -> None:
        """Não abriu o navegador: copia o link e avisa no próprio link (a linha de status é da sessão)."""
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        link = self._links.get(url)
        if link is None:
            return
        link.configure(text="Link copiado — cole no navegador")
        self._timers["link"] = self.root.after(4000, lambda: link.configure(text=link.original_text))

    def _open_link(self, url: str) -> None:
        """Abre o link no navegador do usuário — pelo Explorer, nunca como administrador."""
        if self._opening_link:
            return
        self._opening_link = True

        def worker() -> None:
            try:
                win.open_url(url)
            except OSError as exc:
                log.info("não foi possível abrir %s: %s", url, exc)
                self._events.put(("copy_link", url))
            finally:
                self._opening_link = False

        threading.Thread(target=worker, name="open-link", daemon=True).start()

    def _buttons(self, frame: tk.Frame, start_command) -> None:
        row = tk.Frame(frame, bg=BG)
        row.pack(fill="x", pady=(self.px(16), 0))
        start = self._button(row, "Iniciar", start_command, ACCENT, ACCENT_HOVER)
        start.pack(side="left", fill="x", expand=True, padx=(0, self.px(5)))
        stop = self._button(row, "Parar", self._stop, NEUTRAL, NEUTRAL_HOVER)
        stop.pack(side="left", fill="x", expand=True, padx=(self.px(5), 0))
        self._start_buttons.append(start)
        self._stop_buttons.append(stop)

    def _button(self, parent, text: str, command, color: str, hover: str) -> tk.Button:
        button = tk.Button(
            parent, text=text, command=command, font=(FONT, 10, "bold"),
            fg="#ffffff", bg=color, activeforeground="#ffffff", activebackground=hover,
            disabledforeground=FAINT, relief="flat", bd=0, cursor="hand2", pady=self.px(7),
        )
        button.colors = (color, hover)
        button.bind("<Enter>", lambda _event: self._hover(button, True))
        button.bind("<Leave>", lambda _event: self._hover(button, False))
        return button

    @staticmethod
    def _hover(button: tk.Button, inside: bool) -> None:
        if str(button["state"]) == "normal":
            button.configure(bg=button.colors[1 if inside else 0])

    @staticmethod
    def _enable(button: tk.Button, enabled: bool) -> None:
        button.configure(
            state="normal" if enabled else "disabled",
            bg=button.colors[0] if enabled else DISABLED,
            cursor="hand2" if enabled else "arrow",
        )

    def _center(self) -> None:
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 3
        self.root.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # -- navegação --

    def _show(self, name: str) -> None:
        self._current = name
        for key, frame in self._screens.items():
            if key == name:
                frame.grid()
            else:
                frame.grid_remove()
        if name == "ssh":
            self._host_entry.focus_set()
        else:
            self.root.focus_set()

    def _go_home(self) -> None:
        if self._session is None:           # não dá para trocar de modo com a conexão ativa
            self._show("home")

    # -- sessão --

    def _start_ssh(self) -> None:
        if self._session is not None:
            return
        try:
            host, port, user = parse_target(self._host.get(), self._port.get(), self._user.get())
        except ValueError as exc:
            self._set_status(session.RED, f"Erro: {exc}")
            self._host_entry.focus_set()
            return
        self._begin(session.SshSession(
            host, port, user, self._password.get() or None,
            on_status=self._post_status, on_finish=self._post_finish,
        ))

    def _start_public(self) -> None:
        if self._session is not None:
            return
        self._begin(session.PublicSession(
            dedicated=self._dedicated,
            on_status=self._post_status, on_finish=self._post_finish, on_notice=self._post_notice,
        ))

    def _begin(self, new_session: session.Session) -> None:
        self._session = new_session
        self._stopping = False
        self._pending_status = None
        self._apply_notice(None)
        self._set_running(True)
        self._set_status(session.BLUE, "Conectando...")
        new_session.start()

    def _stop(self) -> None:
        if self._session is None or self._stopping:
            return
        self._stopping = True
        self._session.stop()
        for button in self._stop_buttons:
            self._enable(button, False)
        self._set_status(session.BLUE, "Encerrando...")

    def _set_running(self, running: bool) -> None:
        for button in self._start_buttons:
            self._enable(button, not running)
        for button in self._stop_buttons:
            self._enable(button, running)
        for widget in self._locked_while_running:
            if isinstance(widget, tk.Entry):
                widget.configure(state="disabled" if running else "normal")
            else:
                widget.configure(cursor="arrow" if running else "hand2", fg=FAINT if running else MUTED)

    def _set_status(self, color: str, text: str) -> None:
        self._status_dot.configure(fg=STATUS_COLORS.get(color, STATUS_COLORS[session.WHITE]))
        self._status_label.configure(text=text)

    # Chamados pela thread da sessão: só enfileiram; quem mexe na tela é a thread da interface.
    def _post_status(self, color: str, text: str) -> None:
        self._events.put(("status", color, text))

    def _post_finish(self) -> None:
        self._events.put(("finished",))

    def _post_notice(self, kind: str | None) -> None:
        self._events.put(("notice", kind))

    def _drain_events(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                if event[0] == "status":
                    if self._stopping:
                        self._pending_status = event[1:]     # só o último importa
                    else:
                        self._set_status(*event[1:])
                elif event[0] == "notice":
                    self._apply_notice(event[1])          # vale mesmo encerrando (o último é "nenhum")
                elif event[0] == "copy_link":
                    self._copied(event[1])
                elif event[0] == "finished":
                    self._session = None
                    if self._notice == "vps":                 # a sessão acabou: o servidor não está mais em uso
                        self._apply_notice(None)
                    if self._closing:
                        self._destroy()
                        return
                    if self._pending_status is not None:
                        self._set_status(*self._pending_status)
                    self._stopping = False
                    self._pending_status = None
                    self._set_running(False)
        except queue.Empty:
            pass
        if not self._destroyed:
            self._timers["drain"] = self.root.after(60, self._drain_events)

    def _destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        for timer in self._timers.values():          # nada agendado sobrevive à janela
            try:
                self.root.after_cancel(timer)
            except tk.TclError:
                pass
        self.root.destroy()

    def _on_close(self) -> None:
        if self._session is None:
            self._destroy()
            return
        if self._closing:
            return
        if not messagebox.askyesno(
            config.APP_NAME,
            "Fechar o launcher encerra a conexão e fecha o Discord aberto por ele.\n\nDeseja sair?",
            parent=self.root,
        ):
            return
        if self._session is None:           # terminou enquanto a pergunta estava aberta
            self._destroy()
            return
        self._closing = True
        self._stop()
        # A janela some na hora; o processo só termina quando a sessão acabar de
        # fechar o túnel e o Discord. Se ela estiver presa em uma operação de rede,
        # depois de um prazo o encerramento é feito daqui mesmo.
        self.root.withdraw()
        self._timers["force_close"] = self.root.after(FORCE_CLOSE_AFTER_MS, self._force_close)

    def _force_close(self) -> None:
        if self._session is not None:
            self._session.abandon()
        self._destroy()
