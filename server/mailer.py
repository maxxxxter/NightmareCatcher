"""E-Mail-Berichte: fasst die Ereignisse eines Zeitraums zusammen und nennt,
welche Geräte an Lags/Störungen beteiligt waren."""
from __future__ import annotations

import smtplib
import time
from collections import Counter, defaultdict
from email.message import EmailMessage

CATEGORY_LABELS = {
    "packet_loss": "Paketverlust",
    "jitter": "Jitter",
    "disconnect": "Verbindungsabbruch",
    "port_errors": "Port-Fehler",
    "device_offline": "Gerät offline",
    "wifi_quality": "WLAN-Qualität",
    "wifi_congestion": "WLAN-Auslastung",
    "gateway_load": "Router-Auslastung",
    "gateway_reboot": "Router-Neustart",
    "wan_latency": "Internet-Latenz",
    "wan_status": "WAN-Status",
    "ping_timeout": "Ping-Ausfall",
    "ping_latency": "Ping-Latenz",
    "controller_log": "UniFi-Log",
    "controller_alarm": "UniFi-Alarm",
    "device_renamed": "Namensänderung",
}

SEVERITY_LABELS = {"critical": "KRITISCH", "warning": "Warnung", "info": "Hinweis"}


def _fmt(ts: float) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))


def build_report(events: list[dict], since_ts: float, now_ts: float) -> tuple[str, str]:
    """Erzeugt (Betreff, Text) für den Berichtszeitraum."""
    period = f"{_fmt(since_ts)} bis {_fmt(now_ts)}"

    if not events:
        subject = "NightmareCatcher: keine Auffälligkeiten"
        body = (
            f"NightmareCatcher - Bericht\n"
            f"Zeitraum: {period}\n\n"
            f"Im Berichtszeitraum wurden keine Auffälligkeiten festgestellt.\n"
        )
        return subject, body

    critical = sum(1 for e in events if e["severity"] == "critical")
    infos = sum(1 for e in events if e["severity"] == "info")
    warnings = len(events) - critical - infos

    by_device: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_device[e.get("device_name") or "Unbekannt"].append(e)

    lines = [
        "NightmareCatcher - Bericht",
        f"Zeitraum: {period}",
        "",
        f"Zusammenfassung: {len(events)} Auffälligkeit(en), davon {critical} kritisch, "
        f"{warnings} Warnung(en), {infos} Hinweis(e).",
        "",
        "Beteiligte Geräte (sortiert nach Häufigkeit):",
    ]

    for device, dev_events in sorted(by_device.items(), key=lambda kv: len(kv[1]), reverse=True):
        floors = {e.get("floor") for e in dev_events if e.get("floor")}
        floor_str = f" ({', '.join(sorted(floors))})" if floors else ""
        cats = Counter(e["category"] for e in dev_events)
        cat_str = ", ".join(f"{CATEGORY_LABELS.get(c, c)} ({n}x)" for c, n in cats.most_common())
        lines.append(f"  - {device}{floor_str}: {len(dev_events)} Ereignis(se) - {cat_str}")

    lines += ["", "Ereignisse im Detail (neueste zuerst, max. 30):"]
    for e in sorted(events, key=lambda x: x["ts"], reverse=True)[:30]:
        sev = SEVERITY_LABELS.get(e["severity"], "Warnung")
        lines.append(f"  {_fmt(e['ts'])} [{sev}] {e['message']}")

    if len(events) > 30:
        lines.append(f"  ... und {len(events) - 30} weitere (siehe Dashboard).")

    lines += ["", "Diese Nachricht wurde automatisch von der NightmareCatcher erstellt."]

    subject = f"NightmareCatcher: {len(events)} Auffälligkeit(en), davon {critical} kritisch"
    return subject, "\n".join(lines)


def send_mail(s: dict, subject: str, body: str) -> None:
    """Versendet die Mail synchron über SMTP (vom Aufrufer in einen Thread auszulagern)."""
    msg = EmailMessage()
    msg["From"] = s["mail_from"] or s["mail_username"]
    msg["To"] = s["mail_to"]
    msg["Subject"] = subject
    msg.set_content(body)

    host = s["mail_smtp_host"]
    port = int(s["mail_smtp_port"])

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            if s["mail_username"]:
                smtp.login(s["mail_username"], s["mail_password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if s["mail_use_tls"]:
                smtp.starttls()
            if s["mail_username"]:
                smtp.login(s["mail_username"], s["mail_password"])
            smtp.send_message(msg)
