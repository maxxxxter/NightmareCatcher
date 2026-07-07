"""Startpunkt für den NightmareCatcher-Server.

Läuft mit Tray-Symbol in der Windows-Taskleiste: Rechtsklick bietet
"Dashboard öffnen" und "Beenden". Ohne pystray (oder ohne Desktop) läuft der
Server klassisch blockierend in der Konsole.

Nutzung:
    python run.py                      # config.yaml optional (Standard: Port 8000)
    python run.py --no-browser         # ohne automatisches Browserfenster
    python run.py --no-tray            # ohne Tray-Symbol (z.B. als Dienst)
    NETDIAG_CONFIG=pfad.yaml python run.py
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser

import uvicorn

from config import load_config


def main() -> None:
    # Bei --noconsole-EXE-Builds gibt es kein stdout/stderr - Logausgaben
    # landen dann in einer Logdatei neben der EXE statt ins Leere zu laufen.
    if getattr(sys, "frozen", False) and (sys.stdout is None or sys.stderr is None):
        log_file = open("nightmarecatcher-server.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log_file

    cfg_path = os.environ.get("NETDIAG_CONFIG", "config.yaml")
    cfg = load_config(cfg_path)

    url_host = "127.0.0.1" if cfg.server.host in ("0.0.0.0", "::") else cfg.server.host
    url = f"http://{url_host}:{cfg.server.port}"

    if "--no-browser" not in sys.argv:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()

    from main import app
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.server.host, port=cfg.server.port))

    use_tray = "--no-tray" not in sys.argv
    if use_tray:
        try:
            import pystray
            from tray_icon import make_icon_image
        except Exception:
            use_tray = False

    if not use_tray:
        server.run()
        return

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    def on_open(icon, item) -> None:
        webbrowser.open(url)

    def on_quit(icon, item) -> None:
        server.should_exit = True
        icon.stop()

    def watchdog() -> None:
        # Stirbt der Server (z.B. Port schon belegt), darf kein verwaistes
        # Tray-Symbol ohne laufenden Server übrig bleiben.
        server_thread.join()
        print("Server-Thread beendet - Tray-Symbol wird entfernt. "
              "Falls unerwartet: Läuft bereits eine andere Instanz auf dem Port?")
        try:
            icon.stop()
        except Exception:
            pass

    icon = pystray.Icon(
        "NightmareCatcher",
        make_icon_image(),
        f"NightmareCatcher Server ({url})",
        menu=pystray.Menu(
            pystray.MenuItem("Dashboard öffnen", on_open, default=True),
            pystray.MenuItem("Beenden", on_quit),
        ),
    )
    threading.Thread(target=watchdog, daemon=True).start()
    icon.run()  # blockiert bis "Beenden"
    server_thread.join(timeout=10)


if __name__ == "__main__":
    main()
