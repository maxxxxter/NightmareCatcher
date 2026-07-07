from __future__ import annotations

import platform
import re
import statistics
import subprocess
from typing import Optional, TypedDict


class PingResult(TypedDict):
    sent: int
    received: int
    avg_latency_ms: Optional[float]
    jitter_ms: Optional[float]
    rtts: list[float]


def ping_host(ip: str, count: int = 8, timeout_ms: int = 1000) -> PingResult:
    """Führt einen Ping über das jeweilige Betriebssystem-Kommando aus.

    Nutzt bewusst das System-Ping statt Raw-Sockets, damit auf Windows keine
    Administratorrechte nötig sind.
    """
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", str(count), "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(max(1, timeout_ms // 1000)), ip]

    try:
        # errors="replace": deutsche Windows-Ausgabe ist cp850, Python nimmt
        # cp1252 an - Umlaute wären sonst ein fataler Dekodierfehler.
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace",
            timeout=count * (timeout_ms / 1000 + 1) + 5,
        )
        text = proc.stdout
    except Exception:
        return {"sent": count, "received": 0, "avg_latency_ms": None, "jitter_ms": None, "rtts": []}

    rtts = [float(x) for x in re.findall(r"(?:zeit|time)[=<]([\d.]+)\s*ms", text, re.IGNORECASE)]

    if system == "windows":
        m = re.search(r"(?:Gesendet|Sent)\s*=\s*(\d+),\s*(?:Empfangen|Received)\s*=\s*(\d+)", text, re.IGNORECASE)
        sent, received = (int(m.group(1)), int(m.group(2))) if m else (count, len(rtts))
    else:
        m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
        sent, received = (int(m.group(1)), int(m.group(2))) if m else (count, len(rtts))

    avg = round(statistics.mean(rtts), 2) if rtts else None
    if len(rtts) > 1:
        jitter = round(statistics.pstdev(rtts), 2)
    elif len(rtts) == 1:
        jitter = 0.0
    else:
        jitter = None

    return {"sent": sent, "received": received, "avg_latency_ms": avg, "jitter_ms": jitter, "rtts": rtts}


def traceroute(ip: str, max_hops: int = 15) -> str:
    system = platform.system().lower()
    if system == "windows":
        cmd = ["tracert", "-d", "-h", str(max_hops), ip]
    else:
        cmd = ["traceroute", "-n", "-m", str(max_hops), ip]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)
        return proc.stdout
    except Exception as e:
        return f"Traceroute fehlgeschlagen: {e}"
