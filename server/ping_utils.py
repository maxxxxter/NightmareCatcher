from __future__ import annotations

import platform
import re
import socket
import subprocess
import time
from typing import Optional


def tcp_check(host: str, port: int, timeout_s: float = 2.0) -> Optional[float]:
    """TCP-Verbindungstest: misst, wie schnell der Dienst auf host:port
    antwortet. Gibt die Zeit in ms zurück oder None, wenn der Dienst nicht
    erreichbar ist. Prüft damit die Anwendung selbst (z.B. einen Server-Dienst),
    nicht nur die Netzwerk-Erreichbarkeit des Geräts."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return round((time.perf_counter() - start) * 1000, 1)
    except OSError:
        return None


def ping_once(ip: str, timeout_ms: int = 1000) -> Optional[float]:
    """Einzelner Ping über das System-Kommando; gibt die RTT in ms zurück
    oder None bei Timeout/Fehler. Kein Raw-Socket, daher keine Admin-Rechte nötig."""
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]

    try:
        # errors="replace": deutsche Windows-Ausgabe ist cp850, Python nimmt
        # cp1252 an - Umlaute wären sonst ein fataler Dekodierfehler.
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace",
            timeout=timeout_ms / 1000 + 2,
            creationflags=subprocess.CREATE_NO_WINDOW if system == "windows" else 0,
        )
        m = re.search(r"(?:zeit|time)[=<]([\d.]+)\s*ms", proc.stdout, re.IGNORECASE)
        if m and proc.returncode == 0:
            return float(m.group(1))
        return None
    except Exception:
        return None
