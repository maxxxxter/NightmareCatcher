"""NightmareCatcher Agent - Desktop-Programm mit Einstellungsmenü.

Alle Einstellungen werden über das Menü "Einstellungen" gepflegt und dauerhaft
in agent.db neben dem Programm gespeichert - keine config.yaml mehr nötig
(eine vorhandene wird beim ersten Start automatisch importiert).

Nutzung:
    python agent_gui.py
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

import httpx

import agent_settings
import autostart
from agent import build_targets, run_cycle

FLOORS = ["Keller", "EG", "1.OG", "2.OG"]

# Farbpalette wie das Server-Dashboard (Home-Assistant-Stil)
BG = "#111318"
CARD = "#1c1e26"
BORDER = "#2c2e38"
TEXT = "#e1e3e8"
MUTED = "#9a9da8"
ACCENT = "#03a9f4"
OK = "#4caf50"
WARN = "#ffb300"
CRIT = "#f44336"


def apply_dark_theme(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(bg=BG)
    style.configure(".", background=BG, foreground=TEXT, fieldbackground=CARD,
                    bordercolor=BORDER, lightcolor=BG, darkcolor=BG, focuscolor=ACCENT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 8))
    style.configure("Status.TLabel", background=BG, font=("Segoe UI", 10, "bold"))
    style.configure("TButton", background=CARD, foreground=TEXT, bordercolor=BORDER, padding=6)
    style.map("TButton", background=[("active", "#23252e")])
    style.configure("TEntry", fieldbackground=CARD, foreground=TEXT, insertcolor=TEXT)
    style.configure("TSpinbox", fieldbackground=CARD, foreground=TEXT, insertcolor=TEXT,
                    arrowcolor=TEXT, background=CARD)
    style.configure("TCombobox", fieldbackground=CARD, foreground=TEXT, background=CARD,
                    arrowcolor=TEXT)
    style.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", TEXT)])
    style.configure("TCheckbutton", background=BG, foreground=TEXT)
    style.map("TCheckbutton", background=[("active", BG)])
    style.configure("TLabelframe", background=BG, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=MUTED)
    root.option_add("*TCombobox*Listbox*Background", CARD)
    root.option_add("*TCombobox*Listbox*Foreground", TEXT)
    root.option_add("*TCombobox*Listbox*selectBackground", ACCENT)


class SettingsDialog(tk.Toplevel):
    """Einstellungen im Server-Stil: gruppiert, mit 'Empfohlen: X' unter jedem
    Feld. Speichern schreibt in die Datenbank; leere Felder überschreiben
    keine Voreinstellungen."""

    def __init__(self, parent: tk.Tk, on_saved) -> None:
        super().__init__(parent)
        self.title("Einstellungen")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.on_saved = on_saved

        s = agent_settings.load()
        self.vars = {
            "device_name": tk.StringVar(value=s["device_name"]),
            "floor": tk.StringVar(value=s["floor"]),
            "server_url": tk.StringVar(value=s["server_url"]),
            "report_interval_seconds": tk.StringVar(value=str(s["report_interval_seconds"])),
            "ping_count": tk.StringVar(value=str(s["ping_count"])),
            "gateway": tk.StringVar(value=s["gateway"]),
            "floor_switch": tk.StringVar(value=s["floor_switch"]),
            "internet": tk.StringVar(value=s["internet"]),
            "loss_percent_trigger": tk.StringVar(value=str(s["loss_percent_trigger"])),
            "jitter_ms_trigger": tk.StringVar(value=str(s["jitter_ms_trigger"])),
            "traceroute_on_anomaly": tk.BooleanVar(value=s["traceroute_on_anomaly"]),
        }

        device = ttk.LabelFrame(self, text="Gerät")
        device.pack(fill="x", padx=12, pady=(12, 4))
        self._field(device, "Gerätename", ttk.Entry(device, textvariable=self.vars["device_name"], width=34), "device_name")
        self._field(device, "Stockwerk", ttk.Combobox(device, textvariable=self.vars["floor"], values=FLOORS, state="readonly", width=32), "floor")
        self._field(device, "Server-URL", ttk.Entry(device, textvariable=self.vars["server_url"], width=34), "server_url")
        self._field(device, "Meldeintervall (s)", ttk.Spinbox(device, from_=5, to=300, textvariable=self.vars["report_interval_seconds"], width=32), "report_interval_seconds")
        self._field(device, "Pings je Messung", ttk.Spinbox(device, from_=3, to=30, textvariable=self.vars["ping_count"], width=32), "ping_count")

        targets = ttk.LabelFrame(self, text="Messziele")
        targets.pack(fill="x", padx=12, pady=4)
        self._field(targets, "Gateway-IP (Router)", ttk.Entry(targets, textvariable=self.vars["gateway"], width=34), "gateway")
        self._field(targets, "Etagen-Switch-IP (optional)", ttk.Entry(targets, textvariable=self.vars["floor_switch"], width=34), "floor_switch")
        self._field(targets, "Internet-Ziele (Komma-getrennt)", ttk.Entry(targets, textvariable=self.vars["internet"], width=34), "internet")

        anomaly = ttk.LabelFrame(self, text="Auffälligkeits-Schwellwerte")
        anomaly.pack(fill="x", padx=12, pady=4)
        self._field(anomaly, "Paketverlust-Schwelle (%)", ttk.Spinbox(anomaly, from_=0.5, to=50, increment=0.5, textvariable=self.vars["loss_percent_trigger"], width=32), "loss_percent_trigger")
        self._field(anomaly, "Jitter-Schwelle (ms)", ttk.Spinbox(anomaly, from_=1, to=200, textvariable=self.vars["jitter_ms_trigger"], width=32), "jitter_ms_trigger")
        ttk.Checkbutton(anomaly, text="Bei Auffälligkeit automatisch Traceroute mitschicken",
                        variable=self.vars["traceroute_on_anomaly"]).pack(anchor="w", padx=10, pady=(2, 8))

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=12, pady=(8, 12))
        ttk.Button(buttons, text="Speichern", command=self._save).pack(side="left")
        ttk.Button(buttons, text="Abbrechen", command=self.destroy).pack(side="left", padx=8)
        self.status = ttk.Label(buttons, text="", style="Muted.TLabel")
        self.status.pack(side="left", padx=8)

    def _field(self, parent: ttk.LabelFrame, label: str, widget: tk.Widget, key: str) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=10, pady=(4, 0))
        ttk.Label(row, text=label, width=28).pack(side="left", anchor="n")
        col = ttk.Frame(row)
        col.pack(side="left", fill="x", expand=True)
        widget.pack(in_=col, fill="x")
        rec = agent_settings.DEFAULTS.get(key)
        if rec not in (None, ""):
            ttk.Label(col, text=f"Empfohlen: {rec}", style="Muted.TLabel").pack(anchor="w")

    @staticmethod
    def _num(raw: str, cast):
        try:
            return cast(str(raw).strip().replace(",", "."))
        except (TypeError, ValueError):
            return None  # ungültig -> gespeicherten Wert nicht anfassen

    def _save(self) -> None:
        agent_settings.save({
            "device_name": self.vars["device_name"].get().strip(),
            "floor": self.vars["floor"].get().strip(),
            "server_url": self.vars["server_url"].get().strip(),
            "report_interval_seconds": self._num(self.vars["report_interval_seconds"].get(), int),
            "ping_count": self._num(self.vars["ping_count"].get(), int),
            "gateway": self.vars["gateway"].get().strip(),
            "floor_switch": self.vars["floor_switch"].get().strip(),
            "internet": self.vars["internet"].get().strip(),
            "loss_percent_trigger": self._num(self.vars["loss_percent_trigger"].get(), float),
            "jitter_ms_trigger": self._num(self.vars["jitter_ms_trigger"].get(), float),
            "traceroute_on_anomaly": bool(self.vars["traceroute_on_anomaly"].get()),
        })
        self.on_saved()
        self.destroy()


class AgentGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("NightmareCatcher - Agent")
        self.root.geometry("560x420")
        apply_dark_theme(root)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self._pending_restart = False

        self._build_menu()
        self._build_main()
        self.tray = None
        self._setup_tray()
        self.root.after(200, self._drain_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_summary()

    # --- Aufbau -----------------------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root, bg=CARD, fg=TEXT, activebackground=ACCENT)
        file_menu = tk.Menu(menubar, tearoff=0, bg=CARD, fg=TEXT, activebackground=ACCENT)
        file_menu.add_command(label="In Tray minimieren", command=self._minimize_to_tray)
        if autostart.is_supported():
            self._autostart_var = tk.BooleanVar(value=autostart.is_enabled("NightmareCatcher-Agent"))
            file_menu.add_checkbutton(label="Automatisch mit Windows starten",
                                      variable=self._autostart_var, command=self._toggle_autostart)
        file_menu.add_separator()
        file_menu.add_command(label="Beenden", command=self._quit_app)
        menubar.add_cascade(label="Datei", menu=file_menu)
        menubar.add_command(label="Einstellungen", command=self.open_settings)
        self.root.config(menu=menubar)

    def _build_main(self) -> None:
        pad = {"padx": 12, "pady": 6}

        info = ttk.Frame(self.root)
        info.pack(fill="x", **pad)
        self.summary_label = ttk.Label(info, text="", style="Muted.TLabel")
        self.summary_label.pack(anchor="w")
        self.status_label = ttk.Label(info, text="Agent gestoppt", style="Status.TLabel", foreground=CRIT)
        self.status_label.pack(anchor="w", pady=(4, 0))

        buttons = ttk.Frame(self.root)
        buttons.pack(fill="x", **pad)
        self.start_stop_btn = ttk.Button(buttons, text="Agent starten", command=self.toggle_agent)
        self.start_stop_btn.pack(side="left")
        ttk.Button(buttons, text="Einstellungen…", command=self.open_settings).pack(side="left", padx=6)
        ttk.Button(buttons, text="Verbindung testen", command=self.test_connection).pack(side="left", padx=6)
        ttk.Button(buttons, text="Dashboard öffnen", command=self.open_dashboard).pack(side="left", padx=6)

        log_frame = ttk.LabelFrame(self.root, text="Protokoll")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word",
                                bg=CARD, fg=TEXT, insertbackground=TEXT,
                                relief="flat", highlightthickness=0)
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _refresh_summary(self) -> None:
        s = agent_settings.load()
        self.summary_label.config(
            text=f"{s['device_name']} · {s['floor']} · Server: {s['server_url']}")

    # --- Einstellungen ------------------------------------------------------

    def open_settings(self) -> None:
        SettingsDialog(self.root, on_saved=self._settings_saved)

    def _settings_saved(self) -> None:
        self._refresh_summary()
        if self.worker_thread and self.worker_thread.is_alive():
            self._log("Einstellungen gespeichert - Agent startet mit den neuen Werten neu...")
            self._pending_restart = True
            self.stop_event.set()
        else:
            self._log("Einstellungen gespeichert.")

    # --- Tray ---------------------------------------------------------------

    def _setup_tray(self) -> None:
        try:
            import pystray
            from tray_icon import make_icon_image
        except Exception:
            return

        def on_show(icon, item) -> None:
            self.root.after(0, self._restore_window)

        def on_quit(icon, item) -> None:
            self.root.after(0, self._quit_app)

        self.tray = pystray.Icon(
            "NightmareCatcher-Agent", make_icon_image(), "NightmareCatcher Agent",
            menu=pystray.Menu(
                pystray.MenuItem("Fenster anzeigen", on_show, default=True),
                pystray.MenuItem("Beenden", on_quit),
            ),
        )
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _toggle_autostart(self) -> None:
        state = autostart.set_enabled("NightmareCatcher-Agent", self._autostart_var.get())
        self._log("Autostart aktiviert." if state else "Autostart deaktiviert.")

    def _restore_window(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def _minimize_to_tray(self) -> None:
        if self.tray:
            self.root.withdraw()
            self._log("In das Tray-Symbol minimiert - Agent läuft weiter. Beenden über Rechtsklick auf das Symbol.")
        else:
            self.root.iconify()

    def _quit_app(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop_event.set()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    # --- Aktionen -------------------------------------------------------------

    def open_dashboard(self) -> None:
        url = agent_settings.load()["server_url"].rstrip("/")
        if url:
            webbrowser.open(url)
            self._log(f"Dashboard geöffnet: {url}")

    def test_connection(self) -> None:
        url = agent_settings.load()["server_url"].rstrip("/")
        self._log(f"Teste Verbindung zu {url} ...")

        def worker() -> None:
            try:
                resp = httpx.get(f"{url}/api/ping", timeout=5.0)
                resp.raise_for_status()
                self.log_queue.put(f"Verbindung erfolgreich ({url}).")
            except Exception as e:
                self.log_queue.put(f"Verbindung fehlgeschlagen: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def toggle_agent(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop_event.set()
            self.start_stop_btn.config(text="Agent starten")
            self.status_label.config(text="Agent wird gestoppt...", foreground=WARN)
            return
        self.start_agent()

    def start_agent(self) -> None:
        s = agent_settings.load()
        if not s["device_name"] or not s["server_url"]:
            messagebox.showwarning("Einstellungen unvollständig",
                                   "Bitte zuerst Gerätename und Server-URL in den Einstellungen hinterlegen.")
            return
        cfg = agent_settings.to_agent_cfg(s)
        targets = build_targets(cfg)
        self.stop_event.clear()
        self.worker_thread = threading.Thread(target=self._run_loop, args=(cfg, targets), daemon=True)
        self.worker_thread.start()
        self.start_stop_btn.config(text="Agent stoppen")
        self.status_label.config(text="Agent läuft", foreground=OK)
        self._log(f"Agent gestartet. Ziele: {', '.join(label for label, _ in targets)}")
        self.open_dashboard()

    def _run_loop(self, cfg: dict, targets: list[tuple[str, str]]) -> None:
        interval = cfg["agent"].get("report_interval_seconds", 10)
        with httpx.Client(timeout=10.0) as client:
            while not self.stop_event.is_set():
                run_cycle(client, cfg, targets, log=self.log_queue.put)
                self.stop_event.wait(interval)
        self.log_queue.put("Agent gestoppt.")

    # --- Protokoll -------------------------------------------------------------

    def _log(self, message: str) -> None:
        self.log_queue.put(message)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.config(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
            if message == "Agent gestoppt.":
                self.status_label.config(text="Agent gestoppt", foreground=CRIT)
                self.start_stop_btn.config(text="Agent starten")
                if self._pending_restart:
                    self._pending_restart = False
                    self.root.after(300, self.start_agent)
        self.root.after(200, self._drain_log_queue)

    def _on_close(self) -> None:
        if self.tray:
            self._minimize_to_tray()
            return
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("Beenden", "Der Agent läuft noch. Trotzdem beenden?"):
                return
            self.stop_event.set()
        self.root.destroy()


def main() -> None:
    agent_settings.init()
    root = tk.Tk()
    AgentGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
