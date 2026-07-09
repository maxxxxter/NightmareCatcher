"""Netzwerk-Diagnose-Agent.

Läuft auf jedem PC/Notebook, misst regelmäßig Ping/Jitter/Paketverlust zu
Gateway, Etagen-Switch und Internet-Zielen, erkennt WLAN vs. LAN und meldet
alles an den zentralen Diagnose-Server. Bei Auffälligkeiten wird zusätzlich
ein Traceroute mitgeschickt.

Nutzung:
    python agent.py config.yaml
"""
from __future__ import annotations

import sys
import time
from typing import Callable, Optional

import httpx
import yaml

from ping_utils import ping_host, traceroute
from wlan_utils import get_wlan_info


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


def run_cycle(client: httpx.Client, cfg: dict, targets: list[tuple[str, str]],
              log: Callable[[str], None] = print) -> None:
    a = cfg["agent"]
    thr = cfg["anomaly"]
    ts = time.time()

    wlan = get_wlan_info()
    connection_type = wlan["connection_type"] if wlan else "LAN"

    measurements = []
    events = []

    for label, ip in targets:
        r = ping_host(ip, count=a.get("ping_count", 8))
        loss = 0.0 if r["sent"] == 0 else (1 - r["received"] / r["sent"]) * 100

        measurements.append({
            "target_label": label, "target_ip": ip,
            "sent": r["sent"], "received": r["received"],
            "avg_latency_ms": r["avg_latency_ms"], "jitter_ms": r["jitter_ms"],
        })

        jitter = r["jitter_ms"] or 0
        loss_anomaly = loss >= thr["loss_percent_trigger"]
        jitter_anomaly = jitter >= thr["jitter_ms_trigger"]

        if loss_anomaly or jitter_anomaly:
            detail = {
                "loss_percent": round(loss, 1), "jitter_ms": r["jitter_ms"],
                "avg_latency_ms": r["avg_latency_ms"],
            }
            if thr.get("traceroute_on_anomaly") and label.startswith("Internet"):
                detail["traceroute"] = traceroute(ip)

            severity = "critical" if loss >= 2 * thr["loss_percent_trigger"] else "warning"
            category = "packet_loss" if loss_anomaly else "jitter"
            events.append({
                "severity": severity,
                "category": category,
                "message": f"Auffällig zu {label} ({ip}): {loss:.1f}% Verlust, "
                           f"Jitter {r['jitter_ms']} ms, Latenz {r['avg_latency_ms']} ms",
                "detail": detail,
            })

    payload = {
        "device_name": a["device_name"],
        "floor": a["floor"],
        "ts": ts,
        "connection_type": connection_type,
        "wlan_signal_percent": wlan["wlan_signal_percent"] if wlan else None,
        "wlan_channel": wlan["wlan_channel"] if wlan else None,
        "measurements": measurements,
        "events": events,
    }

    try:
        resp = client.post(f"{a['server_url']}/api/report", json=payload)
        resp.raise_for_status()
        status = "OK" if not events else f"{len(events)} Auffälligkeit(en) gemeldet"
        log(f"[{time.strftime('%H:%M:%S')}] Messung übertragen ({status})")
    except Exception as e:
        log(f"[{time.strftime('%H:%M:%S')}] Übertragung an Server fehlgeschlagen: {e}")


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

    with httpx.Client(timeout=10.0) as client:
        while True:
            run_cycle(client, cfg, targets)
            time.sleep(a.get("report_interval_seconds", 10))


if __name__ == "__main__":
    main()
