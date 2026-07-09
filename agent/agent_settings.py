"""Einstellungs-Speicher des Agenten - im Stil des Servers.

Alle Einstellungen liegen in einer SQLite-Datenbank (agent.db) neben dem
Programm, nicht mehr in einer config.yaml. Eine vorhandene config.yaml wird
beim ersten Start einmalig importiert und danach als .imported.bak gesichert.

Wie beim Server gilt: Werte, die nie manuell geändert wurden, folgen bei
App-Updates automatisch der aktuellen Empfehlung; manuell geänderte Werte
bleiben dauerhaft erhalten. Leere Eingaben können sinnvolle Voreinstellungen
nicht überschreiben.
"""
from __future__ import annotations

import json
import socket
import sqlite3
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent  # neben der EXE
else:
    BASE_DIR = Path(__file__).parent

DB_PATH = BASE_DIR / "agent.db"
YAML_PATH = BASE_DIR / "config.yaml"

DEFAULTS: dict = {
    "device_name": socket.gethostname(),
    "floor": "EG",
    "server_url": "http://192.168.1.78:8000",
    "report_interval_seconds": 10,
    "ping_count": 8,
    "gateway": "192.168.1.1",
    "floor_switch": "",
    "internet": "1.1.1.1, 8.8.8.8",
    "loss_percent_trigger": 1.0,
    "jitter_ms_trigger": 10.0,
    "traceroute_on_anomaly": True,
}

_MODIFIED_KEY = "_user_modified_keys"
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        _conn.commit()
    return _conn


def _get(key: str, default=None):
    row = _connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _set(key: str, value) -> None:
    conn = _connect()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()


def _has(key: str) -> bool:
    return _connect().execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone() is not None


def _import_yaml_once() -> bool:
    """Importiert eine vorhandene config.yaml einmalig in die Datenbank."""
    if not YAML_PATH.exists():
        return False
    try:
        import yaml
        raw = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return False

    agent = raw.get("agent", {})
    targets = raw.get("targets", {})
    anomaly = raw.get("anomaly", {})
    internet = targets.get("internet")
    imported = {
        "device_name": agent.get("device_name"),
        "floor": agent.get("floor"),
        "server_url": agent.get("server_url"),
        "report_interval_seconds": agent.get("report_interval_seconds"),
        "ping_count": agent.get("ping_count"),
        "gateway": targets.get("gateway"),
        "floor_switch": targets.get("floor_switch"),
        "internet": ", ".join(internet) if isinstance(internet, list) else internet,
        "loss_percent_trigger": anomaly.get("loss_percent_trigger"),
        "jitter_ms_trigger": anomaly.get("jitter_ms_trigger"),
        "traceroute_on_anomaly": anomaly.get("traceroute_on_anomaly"),
    }
    for key, value in imported.items():
        if value is not None:
            _set(key, value)
    try:
        YAML_PATH.rename(YAML_PATH.with_suffix(".yaml.imported.bak"))
    except OSError:
        pass
    return True


def init() -> None:
    """Beim Programmstart aufrufen: legt fehlende Werte an, importiert eine
    alte config.yaml und gleicht nicht manuell geänderte Werte mit der
    aktuellen Empfehlung ab."""
    first_run = not _has(_MODIFIED_KEY)

    if first_run:
        _import_yaml_once()

    for key, default in DEFAULTS.items():
        if not _has(key):
            _set(key, default)

    if first_run:
        # Alles, was jetzt (z.B. durch den YAML-Import) von der Empfehlung
        # abweicht, gilt als bewusst gesetzt und wird geschützt.
        modified = {key for key, default in DEFAULTS.items() if _get(key, default) != default}
        _set(_MODIFIED_KEY, sorted(modified))

    modified = set(_get(_MODIFIED_KEY, []))
    for key, default in DEFAULTS.items():
        if key not in modified and _get(key, default) != default:
            _set(key, default)


def load() -> dict:
    return {key: _get(key, default) for key, default in DEFAULTS.items()}


def save(partial: dict) -> None:
    modified = set(_get(_MODIFIED_KEY, []))
    for key, value in partial.items():
        if key not in DEFAULTS:
            continue
        default = DEFAULTS[key]
        # Leere/ungültige Eingaben überschreiben keine sinnvolle Voreinstellung
        if value is None and not isinstance(default, str):
            continue
        if value == "" and default != "":
            continue
        _set(key, value)
        if value == default:
            modified.discard(key)
        else:
            modified.add(key)
    _set(_MODIFIED_KEY, sorted(modified))


def to_agent_cfg(s: dict) -> dict:
    """Baut die vom Mess-Loop (agent.run_cycle) erwartete Struktur."""
    internet = [ip.strip() for ip in str(s["internet"]).split(",") if ip.strip()]
    return {
        "agent": {
            "device_name": s["device_name"],
            "floor": s["floor"],
            "server_url": str(s["server_url"]).rstrip("/"),
            "report_interval_seconds": int(s["report_interval_seconds"]),
            "ping_count": int(s["ping_count"]),
        },
        "targets": {
            "gateway": s["gateway"],
            "floor_switch": s["floor_switch"],
            "internet": internet,
        },
        "anomaly": {
            "loss_percent_trigger": float(s["loss_percent_trigger"]),
            "jitter_ms_trigger": float(s["jitter_ms_trigger"]),
            "traceroute_on_anomaly": bool(s["traceroute_on_anomaly"]),
        },
    }
