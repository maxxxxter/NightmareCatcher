from __future__ import annotations

import platform
import re
import subprocess
from typing import Optional


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
