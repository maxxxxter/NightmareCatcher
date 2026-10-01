"""Netzwerk-Diagnose-Agent.

Läuft auf jedem PC/Notebook, misst regelmäßig Ping/Jitter/Paketverlust zu
Gateway, Etagen-Switch und Internet-Zielen, erkennt WLAN vs. LAN und meldet
alles an den zentralen Diagnose-Server. Bei Auffälligkeiten wird zusätzlich
ein Traceroute mitgeschickt.

Alle Ziele werden gleichzeitig gemessen, damit jedes Ziel lückenlos beobachtet
wird. Ist der Server nicht erreichbar, werden die Meldungen zwischengespeichert
(outbox.py) und später nachgeliefert, samt einem Ereignis zum Ausfall.

Nutzung:
    python agent.py config.yaml
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import httpx
import yaml

from outbox import Outbox
from ping_utils import ping_host, traceroute
from wlan_utils import get_wlan_info

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent  # neben der EXE
else:
    BASE_DIR = Path(__file__).parent
OUTBOX_PATH = BASE_DIR / "agent-outbox.db"


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_targets(cfg: dict) -> list[tuple[str, str]]:
    targets = [("Gateway", cfg["targets"]["gateway"])]
    floor_switch = cfg["targets"].get("floor_switch")
    if floor_switch:
        targets.append(("Etagen-Switch", floor_switch))
    for i, ip in enumerate(cfg["targets"].get("internet", [])):
        targets.append((f"Internet-{i + 1}", ip))
    return targets


def _measure(label: str, ip: str, cfg: dict) -> tuple[dict, Optional[dict]]:
    """Misst ein Ziel und liefert (Messwert, Ereignis oder None)."""
    a = cfg["agent"]
    thr = cfg["anomaly"]
    r = ping_host(ip, count=a.get("ping_count", 8))
    loss = 0.0 if r["sent"] == 0 else (1 - r["received"] / r["sent"]) * 100

    measurement = {
        "target_label": label, "target_ip": ip,
        "sent": r["sent"], "received": r["received"],
        "avg_latency_ms": r["avg_latency_ms"], "jitter_ms": r["jitter_ms"],
    }

    jitter = r["jitter_ms"] or 0
    loss_anomaly = loss >= thr["loss_percent_trigger"]
    jitter_anomaly = jitter >= thr["jitter_ms_trigger"]
    if not (loss_anomaly or jitter_anomaly):
        return measurement, None

    detail = {
        "loss_percent": round(loss, 1), "jitter_ms": r["jitter_ms"],
        "avg_latency_ms": r["avg_latency_ms"],
    }
    if thr.get("traceroute_on_anomaly") and label.startswith("Internet"):
        detail["traceroute"] = traceroute(ip)

    severity = "critical" if loss >= 2 * thr["loss_percent_trigger"] else "warning"
    category = "packet_loss" if loss_anomaly else "jitter"
    event = {
        "severity": severity,
        "category": category,
        "message": f"Auffällig zu {label} ({ip}): {loss:.1f}% Verlust, "
                   f"Jitter {r['jitter_ms']} ms, Latenz {r['avg_latency_ms']} ms",
        "detail": detail,
    }
    return measurement, event


def _post(client: httpx.Client, url: str, payload: dict, log: Callable[[str], None]) -> None:
    """Überträgt eine Meldung. Verbindungs- und Serverfehler (5xx) werden
    weitergereicht (-> Zwischenspeicher); lehnt der Server die Meldung
    inhaltlich ab (4xx), wird sie verworfen, damit sie den Speicher nicht
    dauerhaft blockiert."""
    resp = client.post(f"{url}/api/report", json=payload)
    if 400 <= resp.status_code < 500:
        log(f"[{time.strftime('%H:%M:%S')}] Server lehnt Meldung ab (HTTP {resp.status_code}) - verworfen")
        return
    resp.raise_for_status()


def _deliver(client: httpx.Client, cfg: dict, payload: dict, outbox: Optional[Outbox],
             log: Callable[[str], None]) -> None:
    a = cfg["agent"]
    url = a["server_url"]
    now_str = time.strftime('%H:%M:%S')
    status = "OK" if not payload["events"] else f"{len(payload['events'])} Auffälligkeit(en) gemeldet"

    try:
        _post(client, url, payload, log)
    except Exception as e:
        if outbox is None:
            log(f"[{now_str}] Übertragung an Server fehlgeschlagen: {e}")
            return
        outbox.put(payload)
        if outbox.offline_since is None:
            outbox.offline_since = payload["ts"]
            log(f"[{now_str}] Server nicht erreichbar ({e}) - Messungen werden zwischengespeichert")
        return

    log(f"[{now_str}] Messung übertragen ({status})")
    if outbox is None:
        return

    if outbox.offline_since is not None:
        start = outbox.offline_since
        duration = payload["ts"] - start
        outbox.offline_since = None
        outbox.put({
            "device_name": a["device_name"], "floor": a["floor"], "ts": start,
            "connection_type": payload.get("connection_type"),
            "wlan_signal_percent": None, "wlan_channel": None, "measurements": [],
            "events": [{
                "severity": "critical" if duration >= 60 else "warning",
                "category": "disconnect",
                "message": f"Diagnose-Server {duration:.0f} s nicht erreichbar "
                           f"({time.strftime('%H:%M:%S', time.localtime(start))} bis "
                           f"{time.strftime('%H:%M:%S', time.localtime(payload['ts']))}) - "
                           f"Messwerte aus dieser Zeit wurden nachgeliefert",
                "detail": {"duration_s": round(duration, 1)},
            }],
        })
        log(f"[{now_str}] Server wieder erreichbar nach {duration:.0f} s")

    try:
        sent = outbox.flush(lambda p: _post(client, url, p, log))
        if sent:
            rest = outbox.count()
            log(f"[{now_str}] {sent} zwischengespeicherte Meldung(en) nachgeliefert"
                + (f", {rest} ausstehend" if rest else ""))
    except Exception as e:
        log(f"[{now_str}] Nachliefern unterbrochen: {e}")


def run_cycle(client: httpx.Client, cfg: dict, targets: list[tuple[str, str]],
              log: Callable[[str], None] = print, outbox: Optional[Outbox] = None) -> None:
    a = cfg["agent"]
    ts = time.time()

    # Alle Ziele gleichzeitig messen (und die WLAN-Abfrage nebenher), damit
    # jedes Ziel lückenlos beobachtet wird und kurze Lags nicht durchrutschen.
    with ThreadPoolExecutor(max_workers=len(targets) + 1) as pool:
        wlan_future = pool.submit(get_wlan_info)
        results = list(pool.map(lambda t: _measure(t[0], t[1], cfg), targets))
        wlan = wlan_future.result()
    connection_type = wlan["connection_type"] if wlan else "LAN"

    payload = {
        "device_name": a["device_name"],
        "floor": a["floor"],
        "ts": ts,
        "connection_type": connection_type,
        "wlan_signal_percent": wlan["wlan_signal_percent"] if wlan else None,
        "wlan_channel": wlan["wlan_channel"] if wlan else None,
        "measurements": [m for m, _ in results],
        "events": [e for _, e in results if e is not None],
    }
    _deliver(client, cfg, payload, outbox, log)


def sleep_until_next_cycle(started: float, interval: float, wait: Callable[[float], object] = time.sleep) -> None:
    """Das Intervall zählt ab Beginn der Messung - so entstehen keine
    unbeobachteten Pausen zwischen den Durchläufen."""
    wait(max(0.5, interval - (time.time() - started)))


def main() -> None:
    if len(sys.argv) >= 2:
        cfg = load_config(sys.argv[1])
    else:
        # Ohne Argument: Einstellungen aus agent.db (gepflegt über die GUI)
        import agent_settings
        agent_settings.init()
        cfg = agent_settings.to_agent_cfg(agent_settings.load())
        print("[netdiag-agent] Einstellungen aus agent.db geladen")
    a = cfg["agent"]
    targets = build_targets(cfg)

    print(f"[netdiag-agent] Gerät '{a['device_name']}' ({a['floor']}) -> {a['server_url']}")
    print(f"[netdiag-agent] Ziele: {', '.join(label for label, _ in targets)}")

    outbox = Outbox(OUTBOX_PATH)
    with httpx.Client(timeout=10.0) as client:
        while True:
            started = time.time()
            run_cycle(client, cfg, targets, outbox=outbox)
            sleep_until_next_cycle(started, a.get("report_interval_seconds", 10))


if __name__ == "__main__":
    main()
