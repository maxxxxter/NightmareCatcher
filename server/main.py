from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import autostart
import db
import mailer
import settings
from config import Config, load_config
from ping_utils import ping_once, tcp_check
from unifi_client import UnifiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("netdiag")

CONFIG_PATH = os.environ.get("NETDIAG_CONFIG", "config.yaml")
cfg: Config = load_config(CONFIG_PATH)

APP_VERSION = "v1.3.0"
GITHUB_REPO = "maxxxxter/NightmareCatcher"

app = FastAPI(title="NightmareCatcher", version=APP_VERSION)

_latest_release: dict = {}

FLOOR_LABELS = ["Keller", "EG", "1.OG", "2.OG"]

_last_port_stats: dict[tuple, dict] = {}
_last_event_fired: dict[tuple, float] = {}
_throughput_history: deque = deque(maxlen=200)
_gateway_health: dict = {}
_ping_samples: dict[str, deque] = {}
_seen_log_ids: set[str] = set()
_last_device_names: dict[str, str] = {}
_device_names_loaded = False
_client_ap: dict[str, str] = {}
_client_roams: dict[str, deque] = {}

GATEWAY_TYPES = ("ugw", "udm", "uxg")

# Schlüsselwörter, nach denen die UniFi-Controller-Logs permanent durchsucht
# werden - alles, was Lags/Abbrüche erklären kann: Verbindungsverluste,
# Neustarts, WAN-Wechsel, DFS-Radar (erzwungener WLAN-Kanalwechsel), STP/Loops.
LOG_KEYWORDS = (
    "disconnect", "lost contact", "restart", "reboot", "radar", "dfs",
    "channel", "wan", "offline", "upgrade", "stp", "loop", "blocked",
    "poweroff", "isolated",
)

_unifi_client: Optional[UnifiClient] = None
_unifi_client_sig: Optional[tuple] = None
_unifi_client_lock = asyncio.Lock()


class MeasurementIn(BaseModel):
    target_label: str
    target_ip: str
    sent: int
    received: int
    avg_latency_ms: Optional[float] = None
    jitter_ms: Optional[float] = None


class EventIn(BaseModel):
    severity: str
    category: str
    message: str
    detail: Optional[dict] = None


class ReportIn(BaseModel):
    device_name: str
    floor: str
    ts: float
    connection_type: Optional[str] = None
    wlan_signal_percent: Optional[int] = None
    wlan_channel: Optional[int] = None
    measurements: list[MeasurementIn] = []
    events: list[EventIn] = []


import re as _re

_FLOOR_PATTERNS = [
    # Reihenfolge wichtig: spezifische Muster zuerst ("2.OG" enthält auch "OG").
    ("2.OG", _re.compile(r"\b2\.?\s*og\b|\bdg\b|büro|buero", _re.IGNORECASE)),
    ("1.OG", _re.compile(r"\b1\.?\s*og\b|\bog\b", _re.IGNORECASE)),
    ("EG", _re.compile(r"\beg\b", _re.IGNORECASE)),
    ("Keller", _re.compile(r"keller|\bug\b", _re.IGNORECASE)),
]


def guess_floor(name: Optional[str]) -> Optional[str]:
    """Errät das Stockwerk aus dem Gerätenamen (z.B. 'Switch 1.OG' -> 1.OG,
    'AP Büro' -> 2.OG). Wird nur genutzt, solange keine manuelle Zuordnung existiert."""
    if not name:
        return None
    for floor, pattern in _FLOOR_PATTERNS:
        if pattern.search(name):
            return floor
    return None


def _should_fire(key: tuple, cooldown: float) -> bool:
    """Verhindert, dass ein andauernder Zustand (z.B. Gerät offline) bei jedem
    Poll-Intervall ein neues Ereignis erzeugt."""
    now = time.time()
    last = _last_event_fired.get(key)
    if last is not None and now - last < cooldown:
        return False
    _last_event_fired[key] = now
    return True


async def _ensure_unifi_client(s: dict) -> Optional[UnifiClient]:
    """Legt bei Bedarf einen neuen UniFi-Client an - z.B. wenn sich die
    Zugangsdaten über die Einstellungen-Seite geändert haben - und gibt None
    zurück, wenn die Integration deaktiviert oder unvollständig konfiguriert ist."""
    global _unifi_client, _unifi_client_sig
    async with _unifi_client_lock:
        if not s["unifi_enabled"] or not s["unifi_controller_url"] or not s["unifi_username"]:
            if _unifi_client:
                await _unifi_client.close()
                _unifi_client = None
                _unifi_client_sig = None
            return None

        sig = (s["unifi_controller_url"], s["unifi_username"], s["unifi_password"],
               s["unifi_site"], s["unifi_verify_ssl"])
        if _unifi_client is None or sig != _unifi_client_sig:
            if _unifi_client:
                await _unifi_client.close()
            _unifi_client = UnifiClient(sig[0], sig[1], sig[2], site=sig[3], verify_ssl=sig[4])
            _unifi_client_sig = sig
        return _unifi_client


def _grade(value, warn, crit, higher_is_bad: bool = True) -> str:
    if value is None:
        return "unknown"
    if higher_is_bad:
        if value >= crit:
            return "critical"
        if value >= warn:
            return "warning"
    else:
        if value <= crit:
            return "critical"
        if value <= warn:
            return "warning"
    return "ok"


def _check_gateway(dev: dict, s: dict, now: float, floor: Optional[str], alerts: bool = True) -> None:
    """Prüft die UDM/das Gateway auf Zustände, die Latenzen erklären können:
    CPU-/Speicher-Überlastung, Übertemperatur, kürzlicher Neustart.
    Bei alerts=False werden die Werte fürs Dashboard weiter erfasst,
    aber keine Ereignisse erzeugt (Überwachung deaktiviert)."""
    name = dev.get("name") or dev.get("model") or dev.get("mac", "Gateway")
    mac = dev.get("mac", "gateway")
    cooldown = s["event_cooldown_seconds"]
    stats = dev.get("system-stats") or {}

    try:
        cpu = float(stats.get("cpu")) if stats.get("cpu") is not None else None
    except (TypeError, ValueError):
        cpu = None
    try:
        mem = float(stats.get("mem")) if stats.get("mem") is not None else None
    except (TypeError, ValueError):
        mem = None

    temp = dev.get("general_temperature")
    if temp is None:
        temps = [t.get("value") for t in (dev.get("temperatures") or []) if isinstance(t.get("value"), (int, float))]
        temp = round(max(temps), 1) if temps else None

    uptime = dev.get("uptime")

    checks = [
        ("cpu", cpu, s["gw_cpu_warn"], s["gw_cpu_crit"], "CPU-Auslastung", "%"),
        ("mem", mem, s["gw_mem_warn"], s["gw_mem_crit"], "Speicher-Auslastung", "%"),
        ("temp", temp, s["gw_temp_warn"], s["gw_temp_crit"], "Temperatur", "°C"),
    ]
    levels = {}
    for key, value, warn, crit, label, unit in checks:
        level = _grade(value, warn, crit)
        levels[key] = level
        if alerts and level in ("warning", "critical") and _should_fire((mac, f"gw_{key}"), cooldown):
            db.insert_event(
                now, "unifi", level, "gateway_load", name, floor,
                f"Router '{name}': {label} bei {value}{unit} - kann Latenzen/Ruckler verursachen",
                {key: value},
            )

    if alerts and uptime is not None and uptime < s["unifi_poll_interval_seconds"] * 3:
        if _should_fire((mac, "gw_reboot"), cooldown):
            db.insert_event(
                now, "unifi", "critical", "gateway_reboot", name, floor,
                f"Router '{name}' wurde vor kurzem neu gestartet (Uptime {uptime} s) - "
                f"erklärt Verbindungsabbrüche zu diesem Zeitpunkt",
                {"uptime": uptime},
            )

    _gateway_health.update({
        "ts": now, "name": name, "cpu": cpu, "mem": mem, "temp": temp, "uptime": uptime,
        "levels": {**_gateway_health.get("levels", {}), **levels},
    })


def _check_health(health: list[dict], s: dict, now: float, alerts: bool = True) -> None:
    """Wertet den Controller-Gesundheitsstatus aus: vom Gateway gemessene
    Internet-Latenz und Status der WAN-/Internet-Subsysteme.
    Bei alerts=False nur Anzeige-Aktualisierung, keine Ereignisse."""
    cooldown = s["event_cooldown_seconds"]
    www = next((h for h in health if h.get("subsystem") == "www"), None)
    wan = next((h for h in health if h.get("subsystem") == "wan"), None)

    latency = www.get("latency") if www else None
    wan_status = wan.get("status") if wan else None
    www_status = www.get("status") if www else None

    latency_level = _grade(latency, s["wan_latency_ms_warn"], s["wan_latency_ms_crit"])
    if alerts and latency_level in ("warning", "critical") and _should_fire(("health", "wan_latency"), cooldown):
        db.insert_event(
            now, "unifi", latency_level, "wan_latency", "Gateway", None,
            f"Internet-Latenz am Gateway erhöht: {latency} ms "
            f"(Warnung ab {s['wan_latency_ms_warn']} ms)",
            {"latency_ms": latency},
        )

    for label, status_value in (("WAN", wan_status), ("Internet", www_status)):
        if alerts and status_value and status_value not in ("ok", "unknown") and \
                _should_fire(("health", f"status_{label}"), cooldown):
            db.insert_event(
                now, "unifi", "critical", "wan_status", "Gateway", None,
                f"{label}-Subsystem meldet Status '{status_value}'",
                {"status": status_value},
            )

    _gateway_health.update({
        "ts": now, "wan_latency_ms": latency, "wan_status": wan_status, "www_status": www_status,
        "levels": {**_gateway_health.get("levels", {}), "wan_latency": latency_level},
    })


def _handle_rename(mac: str, old_name: str, new_name: str,
                   floor_entries: dict, now: float) -> None:
    """Übernimmt eine im UniFi-Controller erfolgte Umbenennung in die App:
    protokolliert die Änderung und aktualisiert eine automatisch erratene
    Stockwerk-Zuordnung anhand des neuen Namens. Manuelle Zuordnungen bleiben
    grundsätzlich unangetastet."""
    entry = floor_entries.get(mac)
    floor_note = ""
    if entry and entry.get("source") == "auto":
        guessed = guess_floor(new_name)
        if guessed and guessed != entry.get("floor"):
            db.set_device_floor(mac, guessed, source="auto")
            entry["floor"] = guessed
            floor_note = f" - Stockwerk-Zuordnung automatisch auf {guessed} aktualisiert"

    db.insert_event(
        now, "unifi", "info", "device_renamed", new_name,
        (entry or {}).get("floor") or None,
        f"Gerät umbenannt: '{old_name}' → '{new_name}'{floor_note}",
        {"mac": mac, "old_name": old_name},
    )
    log.info("UniFi-Gerät umbenannt: '%s' -> '%s'%s", old_name, new_name, floor_note)


WIFI_LOG_HINTS = ("radar", "dfs", "channel", "wireless", "wlan", "roam")


ROAM_PINGPONG_COUNT = 4          # ab so vielen AP-Wechseln ...
ROAM_PINGPONG_WINDOW = 600       # ... innerhalb dieses Fensters (s) wird gewarnt


def _check_roaming(clients: list[dict], device_names: dict[str, str],
                   s: dict, now: float) -> None:
    """Erkennt AP-Wechsel der WLAN-Clients. Einzelne Wechsel sind normal und
    werden nur protokolliert (Hinweis); häufige Wechsel in kurzer Zeit
    ('Ping-Pong-Roaming') sind eine klassische Ursache für sporadische
    Verbindungsabbrüche und erzeugen eine Warnung."""
    cooldown = s["event_cooldown_seconds"]
    for c in clients:
        if c.get("is_wired"):
            continue
        mac = c.get("mac")
        ap_mac = c.get("ap_mac")
        if not mac or not ap_mac:
            continue
        prev_ap = _client_ap.get(mac)
        _client_ap[mac] = ap_mac
        if prev_ap is None or prev_ap == ap_mac:
            continue

        client_name = c.get("name") or c.get("hostname") or mac
        old_ap = device_names.get(prev_ap, prev_ap)
        new_ap = device_names.get(ap_mac, ap_mac)
        roams = _client_roams.setdefault(mac, deque(maxlen=20))
        roams.append(now)
        recent = [t for t in roams if now - t <= ROAM_PINGPONG_WINDOW]

        if len(recent) >= ROAM_PINGPONG_COUNT:
            if _should_fire((mac, "roam_pingpong"), cooldown):
                db.insert_event(
                    now, "unifi", "warning", "wifi_roaming", client_name, None,
                    f"Ping-Pong-Roaming: '{client_name}' wechselte {len(recent)}x in "
                    f"{ROAM_PINGPONG_WINDOW // 60} min den Access Point (zuletzt "
                    f"'{old_ap}' → '{new_ap}') - typische Ursache für Verbindungsabbrüche",
                    {"mac": mac, "roams": len(recent)},
                )
        else:
            db.insert_event(
                now, "unifi", "info", "wifi_roaming", client_name, None,
                f"'{client_name}' wechselte den Access Point: '{old_ap}' → '{new_ap}'",
                {"mac": mac},
            )


def _scan_controller_logs(ctrl_events: list[dict], alarms: list[dict], now: float,
                          include_wifi: bool = True) -> None:
    """Durchsucht Controller-Logs und Alarme nach möglichen Fehlerquellen und
    übernimmt Treffer in die Ereignis-Zeitleiste (dedupliziert per Log-ID).
    Bei include_wifi=False werden WLAN-bezogene Einträge (Radar/DFS,
    Kanalwechsel, AP-Meldungen) übersprungen - so ist der WLAN-Schalter
    in den Einstellungen lückenlos."""
    global _seen_log_ids
    if len(_seen_log_ids) > 10000:
        _seen_log_ids = set()

    for entry, severity, category in (
        [(e, "warning", "controller_log") for e in ctrl_events]
        + [(a, "critical", "controller_alarm") for a in alarms]
    ):
        eid = entry.get("_id")
        if not eid or eid in _seen_log_ids:
            continue
        text = f"{entry.get('key', '')} {entry.get('msg', '')}".lower()
        if category == "controller_log" and not any(kw in text for kw in LOG_KEYWORDS):
            _seen_log_ids.add(eid)
            continue
        if not include_wifi and (entry.get("ap_name") or entry.get("ap")
                                 or any(h in text for h in WIFI_LOG_HINTS)):
            _seen_log_ids.add(eid)
            continue
        _seen_log_ids.add(eid)

        device = (entry.get("sw_name") or entry.get("ap_name") or entry.get("gw_name")
                  or entry.get("hostname") or "UniFi")
        entry_ts = entry.get("time")
        ts = entry_ts / 1000 if isinstance(entry_ts, (int, float)) and entry_ts > 1e12 else now
        msg = entry.get("msg") or entry.get("key") or "Unbekannter Log-Eintrag"
        db.insert_event(
            ts, "unifi", severity, category, device, None,
            f"UniFi-Log: {msg}",
            {"key": entry.get("key"), "id": eid},
        )


async def unifi_poll_loop() -> None:
    """Vollständiger Scan: Switch-Port-Fehler, Geräte-Status, WLAN-Funk-Qualität."""
    while True:
        s = settings.get_all(mask_secrets=False)
        client = await _ensure_unifi_client(s)
        if client is None:
            await asyncio.sleep(5)
            continue

        try:
            devices = await client.get_devices()
            now = time.time()
            floor_entries = db.get_floor_entries()
            cooldown = s["event_cooldown_seconds"]

            # Zuletzt bekannte Namen einmalig aus der Datenbank laden, damit
            # Umbenennungen auch nach einem Server-Neustart erkannt werden.
            global _device_names_loaded
            if not _device_names_loaded:
                _last_device_names.update(db.latest_device_names())
                _device_names_loaded = True

            for dev in devices:
                mac = dev.get("mac", "unknown")
                name = dev.get("name") or dev.get("model") or mac
                dtype = dev.get("type")
                state = dev.get("state")

                # Umbenennung im UniFi-Controller erkennen und übernehmen
                old_name = _last_device_names.get(mac)
                if old_name is not None and old_name != name:
                    _handle_rename(mac, old_name, name, floor_entries, now)
                _last_device_names[mac] = name

                # Automatische Stockwerk-Zuordnung aus dem Gerätenamen -
                # nur solange keine (auch bewusst leere) manuelle Zuordnung existiert.
                entry = floor_entries.get(mac)
                if entry is None:
                    guessed = guess_floor(name)
                    if guessed:
                        db.set_device_floor(mac, guessed, source="auto")
                        entry = {"floor": guessed, "source": "auto"}
                floor = (entry or {}).get("floor") or None

                db.insert_unifi_snapshot(now, mac, name, dtype, state, dev)

                if state is not None and state != 1 and s["monitor_ports"]:
                    if _should_fire((mac, "device_offline"), cooldown):
                        db.insert_event(
                            now, "unifi", "critical", "device_offline", name, floor,
                            f"UniFi-Gerät '{name}' ist nicht im Status 'online' (state={state})",
                            {"state": state},
                        )

                # Switch-/Kupfer-Ports: Fehler- und Drop-Zähler als Delta seit letztem Poll
                for port in (dev.get("port_table") or []):
                    if not port.get("up"):
                        continue
                    key = (mac, port.get("port_idx"))
                    rx_err = port.get("rx_errors", 0) or 0
                    tx_err = port.get("tx_errors", 0) or 0
                    rx_drop = port.get("rx_dropped", 0) or 0
                    tx_drop = port.get("tx_dropped", 0) or 0
                    prev = _last_port_stats.get(key)
                    _last_port_stats[key] = dict(
                        rx_errors=rx_err, tx_errors=tx_err, rx_dropped=rx_drop, tx_dropped=tx_drop,
                    )
                    if prev is None:
                        continue
                    d_err = (rx_err - prev["rx_errors"]) + (tx_err - prev["tx_errors"])
                    d_drop = (rx_drop - prev["rx_dropped"]) + (tx_drop - prev["tx_dropped"])
                    if (d_err >= s["unifi_port_error_delta_warn"] or d_drop >= s["unifi_port_error_delta_warn"]) \
                            and s["monitor_ports"]:
                        db.insert_event(
                            now, "unifi", "warning", "port_errors", name, floor,
                            f"Port {port.get('port_idx')} an '{name}': +{d_err} Fehler, +{d_drop} Drops "
                            f"seit letzter Prüfung",
                            {"port_idx": port.get("port_idx"), "d_err": d_err, "d_drop": d_drop},
                        )

                # Router/UDM: Systemlast, Temperatur, Neustarts
                if dtype in GATEWAY_TYPES:
                    _check_gateway(dev, s, now, floor, alerts=s["monitor_gateway"])

                # WLAN-Access-Points: Funk-Qualität und Kanalauslastung je Band
                if dtype == "uap" and s["monitor_wifi"]:
                    for radio in (dev.get("radio_table_stats") or []):
                        radio_name = radio.get("name") or radio.get("radio") or "?"
                        satisfaction = radio.get("satisfaction")
                        cu_total = radio.get("cu_total")

                        # -1 bedeutet bei UniFi "keine Daten/keine Clients", nicht "schlecht"
                        if satisfaction is not None and satisfaction >= 0:
                            if satisfaction <= s["wifi_satisfaction_crit"] and \
                                    _should_fire((mac, radio_name, "wifi_quality"), cooldown):
                                db.insert_event(
                                    now, "unifi", "critical", "wifi_quality", name, floor,
                                    f"WLAN-Funk {radio_name} an '{name}': Zufriedenheit nur {satisfaction}%",
                                    {"radio": radio_name, "satisfaction": satisfaction},
                                )
                            elif satisfaction <= s["wifi_satisfaction_warn"] and \
                                    _should_fire((mac, radio_name, "wifi_quality"), cooldown):
                                db.insert_event(
                                    now, "unifi", "warning", "wifi_quality", name, floor,
                                    f"WLAN-Funk {radio_name} an '{name}': Zufriedenheit {satisfaction}%",
                                    {"radio": radio_name, "satisfaction": satisfaction},
                                )

                        if cu_total is not None:
                            if cu_total >= s["wifi_cu_crit"] and \
                                    _should_fire((mac, radio_name, "wifi_congestion"), cooldown):
                                db.insert_event(
                                    now, "unifi", "critical", "wifi_congestion", name, floor,
                                    f"WLAN-Funk {radio_name} an '{name}': Kanalauslastung {cu_total}%",
                                    {"radio": radio_name, "cu_total": cu_total},
                                )
                            elif cu_total >= s["wifi_cu_warn"] and \
                                    _should_fire((mac, radio_name, "wifi_congestion"), cooldown):
                                db.insert_event(
                                    now, "unifi", "warning", "wifi_congestion", name, floor,
                                    f"WLAN-Funk {radio_name} an '{name}': Kanalauslastung {cu_total}%",
                                    {"radio": radio_name, "cu_total": cu_total},
                                )
            try:
                health = await client.get_health()
                _check_health(health, s, now, alerts=s["monitor_wan"])
            except Exception as e:
                log.debug("Health-Abfrage fehlgeschlagen: %s", e)

            if s["monitor_controller_logs"]:
                try:
                    ctrl_events = await client.get_events(limit=100)
                    alarms = await client.get_alarms()
                    _scan_controller_logs(ctrl_events, alarms, now, include_wifi=s["monitor_wifi"])
                except Exception as e:
                    log.debug("Log-/Alarm-Abfrage fehlgeschlagen: %s", e)

            if s["monitor_wifi"]:
                try:
                    clients = await client.get_clients()
                    _check_roaming(clients, _last_device_names, s, now)
                except Exception as e:
                    log.debug("Client-Abfrage fehlgeschlagen: %s", e)
        except Exception as e:
            log.warning("UniFi-Abfrage fehlgeschlagen: %s", e)

        await asyncio.sleep(s["unifi_poll_interval_seconds"])


async def throughput_poll_loop() -> None:
    """Separater, schnellerer Takt nur für die Internet-Auslastung (Speedmeter)."""
    while True:
        s = settings.get_all(mask_secrets=False)
        client = await _ensure_unifi_client(s)
        if client is not None:
            try:
                devices = await client.get_devices()
                for dev in devices:
                    wan = dev.get("wan1") or dev.get("wan2")
                    if not wan:
                        continue
                    rx = wan.get("rx_bytes-r") or 0
                    tx = wan.get("tx_bytes-r") or 0
                    _throughput_history.append({
                        "ts": time.time(),
                        "down_mbps": round(rx * 8 / 1_000_000, 2),
                        "up_mbps": round(tx * 8 / 1_000_000, 2),
                    })
                    break
            except Exception as e:
                log.debug("Durchsatz-Abfrage fehlgeschlagen: %s", e)
        await asyncio.sleep(3)


def _check_targets(s: dict) -> list[dict]:
    """Alle Dauer-Überwachungsziele: die zwei festen Ping-Ziele plus die frei
    konfigurierten Geräte-Wächter (Ping oder TCP-Dienst-Check)."""
    targets = [
        {"label": "Gateway", "host": s["ping_target_local"], "port": None,
         "warn": s["ping_local_warn_ms"], "crit": s["ping_local_crit_ms"],
         "monitor_key": "monitor_ping", "kind": "ping"},
        {"label": "Internet", "host": s["ping_target_internet"], "port": None,
         "warn": s["ping_internet_warn_ms"], "crit": s["ping_internet_crit_ms"],
         "monitor_key": "monitor_ping", "kind": "ping"},
    ]
    for t in (s.get("watch_targets") or []):
        host = str(t.get("host") or "").strip()
        if not host:
            continue
        port = t.get("port") or None
        try:
            port = int(port) if port else None
        except (TypeError, ValueError):
            port = None
        targets.append({
            "label": str(t.get("name") or host).strip()[:40],
            "host": host, "port": port,
            "warn": s["watch_warn_ms"], "crit": s["watch_crit_ms"],
            "monitor_key": "monitor_watch",
            "kind": "tcp" if port else "ping",
        })
    return targets


def _run_check(t: dict) -> Optional[float]:
    if t["kind"] == "tcp":
        return tcp_check(t["host"], t["port"])
    return ping_once(t["host"])


async def server_ping_loop() -> None:
    """Dauerping vom Server aus: kurzer Takt (Standard 2 s), damit auch kurze
    Lags sichtbar werden. Prüft neben Gateway/Internet auch die frei
    konfigurierten Geräte-Wächter (z.B. IPTV-Box per Ping, Butler21-Server per
    TCP-Dienst-Check). Timeouts und Latenzspitzen erzeugen sofort ein Ereignis."""
    while True:
        s = settings.get_all(mask_secrets=False)
        targets = _check_targets(s)
        results = await asyncio.gather(*[asyncio.to_thread(_run_check, t) for t in targets])
        now = time.time()
        cooldown = s["event_cooldown_seconds"]
        history_samples: dict[str, Optional[float]] = {}
        active_labels = {t["label"] for t in targets}
        for stale in set(_ping_samples) - active_labels:
            del _ping_samples[stale]  # entfernte Wächter-Ziele nicht weiter anzeigen

        for t, rtt in zip(targets, results):
            label, host, port = t["label"], t["host"], t["port"]
            target_desc = f"{host}:{port}" if port else host
            check_desc = "Dienst-Check" if t["kind"] == "tcp" else "Dauerping"
            dq = _ping_samples.setdefault(label, deque(maxlen=150))
            dq.append({"ts": now, "rtt": rtt})
            history_samples[label] = rtt
            if not s[t["monitor_key"]]:
                continue  # Messung/Anzeige läuft weiter, nur keine Ereignisse
            is_watch = t["monitor_key"] == "monitor_watch"
            if rtt is None:
                if _should_fire(("srvping", label, "timeout"), cooldown):
                    db.insert_event(
                        now, "server", "critical",
                        "watch_timeout" if is_watch else "ping_timeout",
                        label if is_watch else "Diagnose-Server", None,
                        f"{check_desc} zu {label} ({target_desc}): keine Antwort", None,
                    )
            else:
                level = _grade(rtt, t["warn"], t["crit"])
                if level in ("warning", "critical") and _should_fire(("srvping", label, "latency"), cooldown):
                    db.insert_event(
                        now, "server", level,
                        "watch_latency" if is_watch else "ping_latency",
                        label if is_watch else "Diagnose-Server", None,
                        f"{check_desc} zu {label} ({target_desc}): Latenzspitze {rtt:.0f} ms "
                        f"(Warnung ab {t['warn']} ms)",
                        {"rtt_ms": rtt},
                    )
        try:
            db.insert_ping_history(now, history_samples)
        except Exception as e:
            log.debug("Ping-Historie speichern fehlgeschlagen: %s", e)
        await asyncio.sleep(max(1, s["server_ping_interval_seconds"]))


async def mail_report_loop() -> None:
    """Versendet im eingestellten Intervall einen Bericht über alle
    Auffälligkeiten des Zeitraums an die hinterlegte Mailadresse."""
    while True:
        await asyncio.sleep(60)
        s = settings.get_all(mask_secrets=False)
        if not s["mail_enabled"] or not s["mail_to"] or not s["mail_smtp_host"]:
            continue
        interval = max(1, float(s["mail_report_interval_hours"])) * 3600
        last = db.get_setting("mail_last_report_ts", 0) or 0
        now = time.time()
        if now - last < interval:
            continue
        since = last if last else now - interval
        events = db.events_since(since)
        subject, body = mailer.build_report(events, since_ts=since, now_ts=now)
        try:
            await asyncio.to_thread(mailer.send_mail, s, subject, body)
            db.set_setting("mail_last_report_ts", now)
            log.info("Mail-Bericht an %s versendet (%d Ereignisse)", s["mail_to"], len(events))
        except Exception as e:
            log.warning("Mail-Versand fehlgeschlagen: %s", e)


async def instant_alert_loop() -> None:
    """Schickt bei neuen kritischen Ereignissen sofort eine Mail (zusätzlich zum
    Intervall-Bericht), mit Cooldown gegen Mail-Fluten. Abschaltbar."""
    last_seen = time.time()
    while True:
        await asyncio.sleep(20)
        s = settings.get_all(mask_secrets=False)
        now = time.time()
        if not (s["mail_instant_alerts"] and s["mail_enabled"] and s["mail_smtp_host"] and s["mail_to"]):
            last_seen = now
            continue
        cooldown = max(1, s["mail_instant_cooldown_minutes"]) * 60
        last_sent = db.get_setting("mail_last_instant_ts", 0) or 0
        new_criticals = [
            e for e in db.events_since(last_seen)
            if e["severity"] == "critical" and e["category"] != "user_marker"
        ]
        last_seen = now
        if not new_criticals or now - last_sent < cooldown:
            continue
        lines = [f"{time.strftime('%H:%M:%S', time.localtime(e['ts']))}  {e['message']}"
                 for e in new_criticals[-15:]]
        subject = f"NightmareCatcher: {len(new_criticals)} kritische(s) Ereignis(se)"
        body = ("Sofort-Alarm - kritische Ereignisse im Heimnetz:\n\n"
                + "\n".join(lines)
                + "\n\nWeitere Details im Dashboard.")
        try:
            await asyncio.to_thread(mailer.send_mail, s, subject, body)
            db.set_setting("mail_last_instant_ts", now)
            log.info("Sofort-Alarm an %s versendet (%d kritische Ereignisse)", s["mail_to"], len(new_criticals))
        except Exception as e:
            log.warning("Sofort-Alarm fehlgeschlagen: %s", e)


async def speedtest_loop() -> None:
    """Löst im eingestellten Intervall den Speedtest des UniFi-Gateways aus und
    speichert die Ergebnisse - deckt schleichende Provider-Probleme auf.
    Intervall 0 = deaktiviert."""
    while True:
        await asyncio.sleep(120)
        s = settings.get_all(mask_secrets=False)
        interval_h = s["speedtest_interval_hours"] or 0
        if interval_h <= 0:
            continue
        now = time.time()
        if now - db.last_speedtest_ts() < interval_h * 3600:
            continue
        client = await _ensure_unifi_client(s)
        if client is None:
            continue
        try:
            await client.start_speedtest()
            log.info("Speedtest am Gateway angestoßen")
            await asyncio.sleep(90)  # Gateway braucht ~30-60 s für den Test

            devices = await client.get_devices()
            for dev in devices:
                if dev.get("type") not in GATEWAY_TYPES:
                    continue
                st = dev.get("speedtest-status") or {}
                rundate = st.get("rundate")
                down = st.get("xput_download")
                up = st.get("xput_upload")
                latency = st.get("latency")
                if not rundate or down is None:
                    log.info("Speedtest-Ergebnis noch nicht verfügbar")
                    break
                if rundate <= db.last_speedtest_ts():
                    break  # kein neues Ergebnis (alter Lauf)
                db.insert_speedtest(rundate, round(down, 1), round(up, 1) if up else None, latency)
                log.info("Speedtest: %.0f/%.0f Mbit/s, %s ms", down, up or 0, latency)

                warn_pct = s["speedtest_warn_percent"] or 0
                wan_max = s["wan_max_mbps"] or 0
                if warn_pct > 0 and wan_max > 0 and down < wan_max * warn_pct / 100:
                    if _should_fire(("speedtest", "slow"), s["event_cooldown_seconds"]):
                        db.insert_event(
                            time.time(), "unifi", "warning", "speedtest", "Gateway", None,
                            f"Speedtest: nur {down:.0f} Mbit/s Download - weniger als "
                            f"{warn_pct}% der Anschluss-Bandbreite ({wan_max} Mbit/s). "
                            f"Deutet auf ein Provider- oder Uplink-Problem hin",
                            {"down_mbps": down, "up_mbps": up, "latency_ms": latency},
                        )
                break
        except Exception as e:
            log.warning("Speedtest fehlgeschlagen: %s", e)


async def update_check_loop() -> None:
    """Prüft täglich auf GitHub, ob eine neuere Version veröffentlicht wurde.
    Rein informativ - das Dashboard zeigt dann einen dezenten Hinweis."""
    import httpx as _httpx
    while True:
        try:
            async with _httpx.AsyncClient(timeout=10.0) as hc:
                resp = await hc.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                    headers={"Accept": "application/vnd.github+json"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    _latest_release.update({
                        "tag": data.get("tag_name"),
                        "url": data.get("html_url"),
                        "checked": time.time(),
                    })
        except Exception as e:
            log.debug("Update-Check fehlgeschlagen: %s", e)
        await asyncio.sleep(24 * 3600)


async def prune_loop() -> None:
    """Stündliche Datenbank-Bereinigung, damit die Datei nicht unbegrenzt wächst."""
    while True:
        try:
            await asyncio.to_thread(db.prune, time.time())
        except Exception as e:
            log.warning("Datenbank-Bereinigung fehlgeschlagen: %s", e)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init(cfg.database.path)
    settings.seed_from_bootstrap(cfg)
    tasks = [
        asyncio.create_task(unifi_poll_loop()),
        asyncio.create_task(throughput_poll_loop()),
        asyncio.create_task(server_ping_loop()),
        asyncio.create_task(mail_report_loop()),
        asyncio.create_task(instant_alert_loop()),
        asyncio.create_task(speedtest_loop()),
        asyncio.create_task(update_check_loop()),
        asyncio.create_task(prune_loop()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        if _unifi_client:
            await _unifi_client.close()


app.router.lifespan_context = lifespan


@app.get("/api/ping")
async def ping() -> dict:
    return {"status": "ok", "ts": time.time()}


@app.post("/api/report")
async def report(payload: ReportIn) -> dict:
    for m in payload.measurements:
        loss = 0.0 if m.sent == 0 else (1 - m.received / m.sent) * 100
        db.insert_measurement(
            payload.ts, payload.device_name, payload.floor, m.target_label, m.target_ip,
            m.sent, m.received, loss, m.avg_latency_ms, m.jitter_ms,
            payload.connection_type, payload.wlan_signal_percent, payload.wlan_channel,
        )
    if settings.get_all(mask_secrets=False)["monitor_agents"]:
        for e in payload.events:
            db.insert_event(
                payload.ts, "agent", e.severity, e.category, payload.device_name, payload.floor,
                e.message, e.detail,
            )
    return {"status": "ok"}


@app.get("/api/status")
async def status() -> list[dict]:
    s = settings.get_all(mask_secrets=False)
    return db.device_status(
        stale_after_seconds=s["stale_after_seconds"],
        loss_warn=s["loss_percent_warn"], loss_crit=s["loss_percent_crit"],
        jitter_warn=s["jitter_ms_warn"], jitter_crit=s["jitter_ms_crit"],
    )


@app.get("/api/events")
async def events(limit: int = 100) -> list[dict]:
    s = settings.get_all(mask_secrets=False)
    return db.events_with_correlation(limit=limit, window_seconds=s["correlation_window_seconds"])


@app.get("/api/unifi/devices")
async def unifi_devices() -> list[dict]:
    return db.latest_unifi_snapshots()


@app.get("/api/unifi/throughput")
async def throughput() -> dict:
    s = settings.get_all(mask_secrets=False)
    if not _throughput_history:
        return {"down_mbps": None, "up_mbps": None, "ts": None, "wan_max_mbps": s["wan_max_mbps"]}
    latest = _throughput_history[-1]
    return {
        "down_mbps": latest["down_mbps"], "up_mbps": latest["up_mbps"], "ts": latest["ts"],
        "wan_max_mbps": s["wan_max_mbps"],
    }


@app.get("/api/gateway/health")
async def gateway_health() -> dict:
    if not _gateway_health:
        return {"available": False}
    return {"available": True, **_gateway_health}


# Welcher Überwachungs-Schalter für welche Ereignis-Kategorie zuständig ist -
# deaktivierte Bereiche fließen nicht in die Gesamtgesundheit ein.
CATEGORY_TOGGLE = {
    "packet_loss": "monitor_agents", "jitter": "monitor_agents", "disconnect": "monitor_agents",
    "port_errors": "monitor_ports", "device_offline": "monitor_ports",
    "wifi_quality": "monitor_wifi", "wifi_congestion": "monitor_wifi",
    "wifi_roaming": "monitor_wifi",
    "gateway_load": "monitor_gateway", "gateway_reboot": "monitor_gateway",
    "wan_latency": "monitor_wan", "wan_status": "monitor_wan",
    "ping_timeout": "monitor_ping", "ping_latency": "monitor_ping",
    "watch_timeout": "monitor_watch", "watch_latency": "monitor_watch",
    "controller_log": "monitor_controller_logs", "controller_alarm": "monitor_controller_logs",
}

_LEVEL_ORDER = {"ok": 0, "warning": 1, "critical": 2}


@app.get("/api/health-summary")
async def health_summary() -> dict:
    """Gesamtgesundheit des Netzwerks für die Ampel im Dashboard: schlechtester
    Zustand über alle aktuell überwachten Parameter (Live-Werte plus die
    Ereignisse der letzten 15 Minuten)."""
    s = settings.get_all(mask_secrets=False)
    now = time.time()
    worst = "ok"
    reasons: list[str] = []

    def bump(level: str, reason: str) -> None:
        nonlocal worst
        if level not in ("warning", "critical"):
            return
        reasons.append(reason)
        if _LEVEL_ORDER[level] > _LEVEL_ORDER[worst]:
            worst = level

    agents = db.device_status(
        stale_after_seconds=s["stale_after_seconds"],
        loss_warn=s["loss_percent_warn"], loss_crit=s["loss_percent_crit"],
        jitter_warn=s["jitter_ms_warn"], jitter_crit=s["jitter_ms_crit"],
    )
    if s["monitor_agents"]:
        for d in agents:
            if d["level"] in ("warning", "critical"):
                bump(d["level"], f"{d['device_name']}: {d['target_label']} auffällig")

    for t in _check_targets(s):
        if not s[t["monitor_key"]]:
            continue
        samples = _ping_samples.get(t["label"])
        if not samples:
            continue
        current = samples[-1]["rtt"]
        kind = "Dienst-Check" if t["kind"] == "tcp" else "Dauerping"
        if current is None:
            bump("critical", f"{kind} {t['label']}: keine Antwort")
        else:
            bump(_grade(current, t["warn"], t["crit"]), f"{kind} {t['label']}: {current:.0f} ms")

    levels = _gateway_health.get("levels", {})
    if s["monitor_gateway"]:
        for key, label in (("cpu", "Router-CPU"), ("mem", "Router-Speicher"), ("temp", "Router-Temperatur")):
            bump(levels.get(key, "ok") if levels.get(key) in _LEVEL_ORDER else "ok", f"{label} erhöht")
    if s["monitor_wan"]:
        if levels.get("wan_latency") in ("warning", "critical"):
            bump(levels["wan_latency"], "Internet-Latenz am Gateway erhöht")
        wan_status = _gateway_health.get("wan_status")
        if wan_status and wan_status not in ("ok", "unknown"):
            bump("critical", f"WAN-Status: {wan_status}")

    unifi_devices_list = db.latest_unifi_snapshots()
    if s["monitor_ports"]:
        for d in unifi_devices_list:
            if d["state"] is not None and d["state"] != 1 and now - d["ts"] < 300:
                bump("critical", f"UniFi-Gerät offline: {d['device_name']}")

    for e in db.events_since(now - 900):
        toggle = CATEGORY_TOGGLE.get(e["category"])
        if toggle and not s.get(toggle, True):
            continue
        if e["severity"] in ("warning", "critical"):
            bump(e["severity"], e["message"][:90])

    seen: set[str] = set()
    unique_reasons = [r for r in reasons if not (r in seen or seen.add(r))]

    has_data = bool(agents) or bool(_gateway_health) or bool(unifi_devices_list) \
        or any(_ping_samples.values())
    return {
        "level": worst if has_data else "unknown",
        "reasons": unique_reasons[:6],
        "ts": now,
    }


@app.get("/api/server-pings")
async def server_pings() -> list[dict]:
    s = settings.get_all(mask_secrets=False)
    result = []
    for t in _check_targets(s):
        label = t["label"]
        ip = f"{t['host']}:{t['port']}" if t["port"] else t["host"]
        warn, crit = t["warn"], t["crit"]
        samples = list(_ping_samples.get(label, []))
        rtts = [x["rtt"] for x in samples if x["rtt"] is not None]
        losses = sum(1 for x in samples if x["rtt"] is None)
        current = samples[-1]["rtt"] if samples else None
        level = "critical" if (samples and current is None) else _grade(current, warn, crit)
        result.append({
            "label": label, "ip": ip,
            "samples": samples,
            "current_ms": round(current, 1) if current is not None else None,
            "avg_ms": round(sum(rtts) / len(rtts), 1) if rtts else None,
            "max_ms": round(max(rtts), 1) if rtts else None,
            "loss_percent": round(losses / len(samples) * 100, 1) if samples else None,
            "level": level, "warn_ms": warn, "crit_ms": crit,
        })
    return result


@app.get("/api/history")
async def history(minutes: int = 1440) -> dict:
    """Ping-/Dienst-Verlauf (Standard: letzte 24 h) für die Diagramme im
    Dashboard - inklusive aller Geräte-Wächter-Ziele."""
    s = settings.get_all(mask_secrets=False)
    since = time.time() - minutes * 60
    return {
        "minutes": minutes,
        "targets": {
            t["label"]: db.ping_history(t["label"], since)
            for t in _check_targets(s)
        },
    }


@app.get("/api/speedtest")
async def speedtest_results(days: int = 7) -> dict:
    s = settings.get_all(mask_secrets=False)
    results = db.speedtest_history(time.time() - days * 86400)
    return {"results": results, "wan_max_mbps": s["wan_max_mbps"],
            "interval_hours": s["speedtest_interval_hours"]}


@app.get("/api/heatmap")
async def heatmap(days: int = 30) -> dict:
    """Störungs-Muster: Anzahl Warnungen/kritischer Ereignisse je Wochentag und
    Stunde (lokale Zeit). Macht sichtbar, ob sich Lags z.B. abends häufen."""
    since = time.time() - days * 86400
    cells = [[0] * 24 for _ in range(7)]  # [Wochentag Mo=0][Stunde]
    total = 0
    for e in db.events_since(since):
        if e["severity"] not in ("warning", "critical"):
            continue
        lt = time.localtime(e["ts"])
        cells[lt.tm_wday][lt.tm_hour] += 1
        total += 1
    return {"days": days, "cells": cells, "total": total,
            "max": max((v for row in cells for v in row), default=0)}


@app.post("/api/mark")
async def mark_moment(payload: dict = Body(default={})) -> dict:
    """Vom Nutzer im Dashboard gesetzter Marker ('Jetzt ruckelt es!'). Wird als
    Ereignis gespeichert, damit man später genau diesen Moment in der Zeitleiste
    findet - samt allem, was im selben Zeitfenster passiert ist."""
    note = str(payload.get("note", "")).strip()[:200]
    now = time.time()
    msg = "Manueller Marker gesetzt (Störung bemerkt)"
    if note:
        msg += f": {note}"
    db.insert_event(now, "user", "info", "user_marker", "Nutzer", None, msg, None)
    window = settings.get_all(mask_secrets=False)["correlation_window_seconds"]
    context = [
        e for e in db.events_since(now - window)
        if e["category"] != "user_marker"
    ]
    return {"status": "ok", "ts": now, "context_count": len(context)}


@app.post("/api/settings/mail/test")
async def test_mail(payload: dict = Body(default={})) -> dict:
    saved = settings.get_all(mask_secrets=False)
    s = dict(saved)
    for key, value in payload.items():
        if key in settings.DEFAULTS and value not in (None, ""):
            s[key] = value

    if not s["mail_smtp_host"] or not s["mail_to"]:
        return {"success": False, "message": "SMTP-Server und Empfängeradresse erforderlich."}

    subject = "NightmareCatcher: Test-Mail"
    body = ("Diese Test-Mail bestätigt, dass der Mail-Versand der NightmareCatcher "
            "korrekt eingerichtet ist.")
    try:
        await asyncio.to_thread(mailer.send_mail, s, subject, body)
        return {"success": True, "message": f"Test-Mail an {s['mail_to']} versendet."}
    except Exception as e:
        detail = str(e) or type(e).__name__
        return {"success": False, "message": f"Versand fehlgeschlagen: {detail}"}


@app.put("/api/unifi/floor-map")
async def update_floor_map(payload: dict = Body(...)) -> dict:
    # Änderungen über die GUI sind immer manuell und übersteuern die Automatik -
    # auch das bewusste Entfernen einer Zuordnung (leerer Wert) bleibt bestehen.
    for mac, floor in payload.items():
        db.set_device_floor(mac, floor or None, source="manual")
    return {"status": "ok"}


@app.get("/api/floors")
async def floors() -> list[str]:
    return FLOOR_LABELS


@app.get("/api/settings")
async def get_settings() -> dict:
    return settings.get_all(mask_secrets=True)


def _version_tuple(tag: str) -> tuple:
    try:
        return tuple(int(x) for x in tag.lstrip("v").split("."))
    except (ValueError, AttributeError):
        return (0,)


@app.get("/api/version")
async def version_info() -> dict:
    latest = _latest_release.get("tag")
    return {
        "current": APP_VERSION,
        "latest": latest,
        "update_available": bool(latest) and _version_tuple(latest) > _version_tuple(APP_VERSION),
        "release_url": _latest_release.get("url"),
    }


@app.get("/api/settings/export")
async def export_settings() -> dict:
    """Alle Einstellungen als Sicherung (inkl. Zugangsdaten im Klartext -
    Datei entsprechend sorgsam aufbewahren)."""
    return {
        "nightmarecatcher_settings": settings.get_all(mask_secrets=False),
        "floor_map": db.get_floor_entries(),
        "exported_at": time.time(),
        "app_version": APP_VERSION,
    }


@app.post("/api/settings/import")
async def import_settings(payload: dict = Body(...)) -> dict:
    data = payload.get("nightmarecatcher_settings")
    if not isinstance(data, dict):
        return {"success": False, "message": "Keine gültige NightmareCatcher-Sicherungsdatei."}
    settings.update(data)
    floor_map = payload.get("floor_map")
    if isinstance(floor_map, dict):
        for mac, entry in floor_map.items():
            if isinstance(entry, dict):
                db.set_device_floor(mac, entry.get("floor") or None,
                                    source=entry.get("source") or "manual")
    return {"success": True,
            "message": f"{len(data)} Einstellung(en) importiert. Seite neu laden, um sie zu sehen."}


@app.get("/api/settings/defaults")
async def get_settings_defaults() -> dict:
    return settings.get_recommended()


@app.get("/api/autostart")
async def get_autostart() -> dict:
    return {"supported": autostart.is_supported(),
            "enabled": autostart.is_enabled("NightmareCatcher-Server")}


@app.put("/api/autostart")
async def set_autostart(payload: dict = Body(...)) -> dict:
    enabled = autostart.set_enabled("NightmareCatcher-Server", bool(payload.get("enabled")))
    return {"supported": autostart.is_supported(), "enabled": enabled}


@app.put("/api/settings")
async def update_settings(payload: dict = Body(...)) -> dict:
    settings.update(payload)
    return settings.get_all(mask_secrets=True)


_last_unifi_test_ts: float = 0.0


@app.post("/api/settings/unifi/test")
async def test_unifi_connection(payload: dict = Body(default={})) -> dict:
    # Mindestabstand zwischen manuellen Tests: jeder Test ist ein echter Login-
    # Versuch am Controller - zu schnelles Wiederholen löst dessen Rate-Limit aus.
    global _last_unifi_test_ts
    now = time.time()
    if now - _last_unifi_test_ts < 30:
        return {"success": False,
                "message": f"Bitte {int(30 - (now - _last_unifi_test_ts))} s warten - zu häufige "
                           f"Login-Versuche lösen das Rate-Limit des Controllers aus."}
    _last_unifi_test_ts = now

    saved = settings.get_all(mask_secrets=False)
    controller_url = payload.get("unifi_controller_url") or saved["unifi_controller_url"]
    username = payload.get("unifi_username") or saved["unifi_username"]
    password = payload.get("unifi_password") or saved["unifi_password"]
    site = payload.get("unifi_site") or saved["unifi_site"]
    verify_ssl = payload.get("unifi_verify_ssl", saved["unifi_verify_ssl"])

    if not controller_url or not username or not password:
        return {"success": False, "message": "Controller-URL, Nutzername und Passwort erforderlich."}

    client = UnifiClient(controller_url, username, password, site=site, verify_ssl=verify_ssl)
    try:
        devices = await client.get_devices()
        return {"success": True, "message": f"Verbindung erfolgreich, {len(devices)} Gerät(e) gefunden."}
    except Exception as e:
        detail = str(e) or type(e).__name__
        return {"success": False, "message": f"Verbindung fehlgeschlagen: {detail}"}
    finally:
        await client.close()


# Bei PyInstaller-Onefile-Builds liegen die statischen Dateien im entpackten
# Temp-Verzeichnis (sys._MEIPASS), nicht neben der .py-Datei.
_base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
static_dir = _base / "static"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/settings")
async def settings_page() -> FileResponse:
    return FileResponse(static_dir / "settings.html")


app.mount("/static", StaticFiles(directory=static_dir), name="static")
