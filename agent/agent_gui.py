"""GUI zur dauerhaften Konfiguration und Steuerung des Netzwerk-Diagnose-Agenten.

Nutzt nur die Python-Standardbibliothek (tkinter) - keine zusätzliche
Abhängigkeit nötig. Speichert die Einstellungen in config.yaml im selben
Verzeichnis, sodass sie beim nächsten Start automatisch wieder geladen werden.

Nutzung:
    python agent_gui.py
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

import httpx
import yaml

from agent import build_targets, run_cycle

CONFIG_PATH = Path(__file__).parent / "config.yaml"

DEFAULT_CFG = {
    "agent": {
        "device_name": "PC-Neu",
        "floor": "EG",
        "server_url": "http://192.168.1.50:8000",
        "report_interval_seconds": 10,
        "ping_count": 8,
    },
    "targets": {
        "gateway": "192.168.1.1",
        "floor_switch": "",
        "internet": ["1.1.1.1", "8.8.8.8"],
    },
    "anomaly": {
        "loss_percent_trigger": 2.0,
        "jitter_ms_trigger": 15,
        "traceroute_on_anomaly": True,
    },
}

FLOORS = ["Keller", "EG", "1.OG", "2.OG"]


def load_or_default() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        merged = {k: dict(v) for k, v in DEFAULT_CFG.items()}
        for section, values in loaded.items():
            merged.setdefault(section, {}).update(values)
        return merged
    return {k: dict(v) for k, v in DEFAULT_CFG.items()}


class AgentGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("NightmareCatcher - Agent")
        self.root.geometry("520x620")

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.stop_event = threading.Event()

        cfg = load_or_default()
        self.vars = {
            "device_name": tk.StringVar(value=cfg["agent"]["device_name"]),
            "floor": tk.StringVar(value=cfg["agent"]["floor"]),
            "server_url": tk.StringVar(value=cfg["agent"]["server_url"]),
            "report_interval_seconds": tk.IntVar(value=cfg["agent"].get("report_interval_seconds", 10)),
            "ping_count": tk.IntVar(value=cfg["agent"].get("ping_count", 8)),
            "gateway": tk.StringVar(value=cfg["targets"]["gateway"]),
            "floor_switch": tk.StringVar(value=cfg["targets"].get("floor_switch", "")),
            "internet": tk.StringVar(value=", ".join(cfg["targets"].get("internet", []))),
            "loss_percent_trigger": tk.DoubleVar(value=cfg["anomaly"].get("loss_percent_trigger", 2.0)),
            "jitter_ms_trigger": tk.DoubleVar(value=cfg["anomaly"].get("jitter_ms_trigger", 15)),
            "traceroute_on_anomaly": tk.BooleanVar(value=cfg["anomaly"].get("traceroute_on_anomaly", True)),
        }

        self._build_menu()
        self._build_form()
        self.tray = None
        self._setup_tray()
        self.root.after(200, self._drain_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="In Tray minimieren", command=self._minimize_to_tray)
        file_menu.add_command(label="Beenden", command=self._quit_app)
        menubar.add_cascade(label="Datei", menu=file_menu)
        self.root.config(menu=menubar)

    def _setup_tray(self) -> None:
        """Tray-Symbol: Fenster-Schließen minimiert nur noch ins Tray, beendet
        wird über das Tray-Menü (oder Datei -> Beenden). Ohne pystray verhält
        sich das Fenster wie bisher."""
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
            "NightmareCatcher-Agent",
            make_icon_image(),
            "NightmareCatcher Agent",
            menu=pystray.Menu(
                pystray.MenuItem("Fenster anzeigen", on_show, default=True),
                pystray.MenuItem("Beenden", on_quit),
            ),
        )
        threading.Thread(target=self.tray.run, daemon=True).start()

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

    def _build_form(self) -> None:
        pad = {"padx": 10, "pady": 4}

        agent_frame = ttk.LabelFrame(self.root, text="Gerät")
        agent_frame.pack(fill="x", **pad)
        self._row(agent_frame, "Gerätename", ttk.Entry(agent_frame, textvariable=self.vars["device_name"]))
        floor_combo = ttk.Combobox(agent_frame, textvariable=self.vars["floor"], values=FLOORS, state="readonly")
        self._row(agent_frame, "Stockwerk", floor_combo)
        self._row(agent_frame, "Server-URL", ttk.Entry(agent_frame, textvariable=self.vars["server_url"]))
        self._row(agent_frame, "Meldeintervall (s)", ttk.Spinbox(agent_frame, from_=5, to=300,
                                                                   textvariable=self.vars["report_interval_seconds"]))
        self._row(agent_frame, "Ping-Anzahl je Messung", ttk.Spinbox(agent_frame, from_=3, to=30,
                                                                       textvariable=self.vars["ping_count"]))

        targets_frame = ttk.LabelFrame(self.root, text="Messziele")
        targets_frame.pack(fill="x", **pad)
        self._row(targets_frame, "Gateway-IP (UniFi Router)", ttk.Entry(targets_frame, textvariable=self.vars["gateway"]))
        self._row(targets_frame, "Etagen-Switch-IP (optional)", ttk.Entry(targets_frame, textvariable=self.vars["floor_switch"]))
        self._row(targets_frame, "Internet-Ziele (Komma-getrennt)", ttk.Entry(targets_frame, textvariable=self.vars["internet"]))

        anomaly_frame = ttk.LabelFrame(self.root, text="Auffälligkeits-Schwellwerte")
        anomaly_frame.pack(fill="x", **pad)
        self._row(anomaly_frame, "Paketverlust-Schwelle (%)",
                  ttk.Spinbox(anomaly_frame, from_=0.5, to=50, increment=0.5,
                              textvariable=self.vars["loss_percent_trigger"]))
        self._row(anomaly_frame, "Jitter-Schwelle (ms)",
                  ttk.Spinbox(anomaly_frame, from_=1, to=200, textvariable=self.vars["jitter_ms_trigger"]))
        ttk.Checkbutton(anomaly_frame, text="Bei Auffälligkeit automatisch Traceroute mitschicken",
                         variable=self.vars["traceroute_on_anomaly"]).pack(anchor="w", padx=10, pady=4)

        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", **pad)
        ttk.Button(btn_frame, text="Speichern", command=self.save).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Verbindung zum Server testen", command=self.test_connection).pack(side="left", padx=4)
        self.start_stop_btn = ttk.Button(btn_frame, text="Agent starten", command=self.toggle_agent)
        self.start_stop_btn.pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Dashboard öffnen", command=self.open_dashboard).pack(side="left", padx=4)

        self.status_label = ttk.Label(self.root, text="Agent gestoppt", foreground="#791f1f")
        self.status_label.pack(anchor="w", padx=10)

        log_frame = ttk.LabelFrame(self.root, text="Protokoll")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=10, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    @staticmethod
    def _row(parent: ttk.LabelFrame, label: str, widget: tk.Widget) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=10, pady=3)
        ttk.Label(row, text=label, width=28).pack(side="left")
        widget.pack(in_=row, side="left", fill="x", expand=True)

    def _current_cfg(self) -> dict:
        internet = [ip.strip() for ip in self.vars["internet"].get().split(",") if ip.strip()]
        return {
            "agent": {
                "device_name": self.vars["device_name"].get().strip(),
                "floor": self.vars["floor"].get().strip(),
                "server_url": self.vars["server_url"].get().strip().rstrip("/"),
                "report_interval_seconds": int(self.vars["report_interval_seconds"].get()),
                "ping_count": int(self.vars["ping_count"].get()),
            },
            "targets": {
                "gateway": self.vars["gateway"].get().strip(),
                "floor_switch": self.vars["floor_switch"].get().strip(),
                "internet": internet,
            },
            "anomaly": {
                "loss_percent_trigger": float(self.vars["loss_percent_trigger"].get()),
                "jitter_ms_trigger": float(self.vars["jitter_ms_trigger"].get()),
                "traceroute_on_anomaly": bool(self.vars["traceroute_on_anomaly"].get()),
            },
        }

    def save(self) -> None:
        cfg = self._current_cfg()
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        self._log(f"Einstellungen gespeichert ({CONFIG_PATH}).")

    def open_dashboard(self) -> None:
        url = self.vars["server_url"].get().strip().rstrip("/")
        if url:
            webbrowser.open(url)
            self._log(f"Dashboard geöffnet: {url}")

    def test_connection(self) -> None:
        url = self.vars["server_url"].get().strip().rstrip("/")
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
            self.status_label.config(text="Agent wird gestoppt...", foreground="#854f0b")
            return

        self.save()
        cfg = self._current_cfg()
        targets = build_targets(cfg)
        self.stop_event.clear()
        self.worker_thread = threading.Thread(target=self._run_loop, args=(cfg, targets), daemon=True)
        self.worker_thread.start()
        self.start_stop_btn.config(text="Agent stoppen")
        self.status_label.config(text="Agent läuft", foreground="#3b6d11")
        self._log(f"Agent gestartet. Ziele: {', '.join(label for label, _ in targets)}")
        self.open_dashboard()

    def _run_loop(self, cfg: dict, targets: list[tuple[str, str]]) -> None:
        interval = cfg["agent"].get("report_interval_seconds", 10)
        with httpx.Client(timeout=10.0) as client:
            while not self.stop_event.is_set():
                run_cycle(client, cfg, targets, log=self.log_queue.put)
                self.stop_event.wait(interval)
        self.log_queue.put("Agent gestoppt.")

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
                self.status_label.config(text="Agent gestoppt", foreground="#791f1f")
        self.root.after(200, self._drain_log_queue)

    def _on_close(self) -> None:
        # X-Button: mit Tray-Symbol nur minimieren (Agent misst weiter),
        # ohne Tray wie gehabt nachfragen und beenden.
        if self.tray:
            self._minimize_to_tray()
            return
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("Beenden", "Der Agent läuft noch. Trotzdem beenden?"):
                return
            self.stop_event.set()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    AgentGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
