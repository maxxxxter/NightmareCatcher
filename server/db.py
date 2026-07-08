from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Optional

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    device_name TEXT NOT NULL,
    floor TEXT NOT NULL,
    target_label TEXT NOT NULL,
    target_ip TEXT NOT NULL,
    sent_count INTEGER NOT NULL,
    received_count INTEGER NOT NULL,
    loss_percent REAL NOT NULL,
    avg_latency_ms REAL,
    jitter_ms REAL,
    connection_type TEXT,
    wlan_signal_percent INTEGER,
    wlan_channel INTEGER
);
CREATE INDEX IF NOT EXISTS idx_measurements_ts ON measurements(ts);
CREATE INDEX IF NOT EXISTS idx_measurements_device ON measurements(device_name, target_label);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    device_name TEXT,
    floor TEXT,
    message TEXT NOT NULL,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS unifi_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    device_mac TEXT NOT NULL,
    device_name TEXT,
    device_type TEXT,
    state INTEGER,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_unifi_ts ON unifi_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_unifi_mac ON unifi_snapshots(device_mac);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_floor_map (
    device_mac TEXT PRIMARY KEY,
    floor TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);
"""

_MISSING = object()


def init(db_path: str) -> None:
    global _conn
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(SCHEMA)
        try:
            _conn.execute("ALTER TABLE device_floor_map ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        except sqlite3.OperationalError:
            pass  # Spalte existiert bereits (Datenbank auf neuem Stand)
        _conn.commit()


def insert_measurement(ts, device_name, floor, target_label, target_ip, sent, received, loss_percent,
                        avg_latency_ms, jitter_ms, connection_type, wlan_signal_percent, wlan_channel):
    with _lock:
        _conn.execute(
            """INSERT INTO measurements
               (ts, device_name, floor, target_label, target_ip, sent_count, received_count, loss_percent,
                avg_latency_ms, jitter_ms, connection_type, wlan_signal_percent, wlan_channel)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, device_name, floor, target_label, target_ip, sent, received, loss_percent,
             avg_latency_ms, jitter_ms, connection_type, wlan_signal_percent, wlan_channel),
        )
        _conn.commit()


def insert_event(ts, source, severity, category, device_name, floor, message, detail: Optional[dict]):
    with _lock:
        _conn.execute(
            """INSERT INTO events (ts, source, severity, category, device_name, floor, message, detail_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, source, severity, category, device_name, floor, message,
             json.dumps(detail, ensure_ascii=False) if detail is not None else None),
        )
        _conn.commit()


def insert_unifi_snapshot(ts, mac, name, dtype, state, raw: dict):
    with _lock:
        _conn.execute(
            """INSERT INTO unifi_snapshots (ts, device_mac, device_name, device_type, state, raw_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ts, mac, name, dtype, state, json.dumps(raw, ensure_ascii=False)),
        )
        _conn.commit()


def device_status(stale_after_seconds: int, loss_warn: float, loss_crit: float,
                   jitter_warn: float, jitter_crit: float) -> list[dict]:
    now = time.time()
    with _lock:
        rows = _conn.execute(
            """SELECT m.* FROM measurements m
               JOIN (
                   SELECT device_name, target_label, MAX(ts) AS max_ts
                   FROM measurements GROUP BY device_name, target_label
               ) latest
               ON m.device_name = latest.device_name AND m.target_label = latest.target_label
                  AND m.ts = latest.max_ts
               ORDER BY m.device_name, m.target_label"""
        ).fetchall()

    result = []
    for r in rows:
        age = now - r["ts"]
        if age > stale_after_seconds:
            level = "unknown"
        elif r["loss_percent"] >= loss_crit or (r["jitter_ms"] or 0) >= jitter_crit:
            level = "critical"
        elif r["loss_percent"] >= loss_warn or (r["jitter_ms"] or 0) >= jitter_warn:
            level = "warning"
        else:
            level = "ok"
        result.append({
            "device_name": r["device_name"], "floor": r["floor"], "target_label": r["target_label"],
            "target_ip": r["target_ip"], "loss_percent": round(r["loss_percent"], 2),
            "avg_latency_ms": r["avg_latency_ms"], "jitter_ms": r["jitter_ms"],
            "connection_type": r["connection_type"], "wlan_signal_percent": r["wlan_signal_percent"],
            "wlan_channel": r["wlan_channel"], "age_seconds": round(age, 1), "level": level,
        })
    return result


def events_with_correlation(limit: int, window_seconds: int) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        all_recent = _conn.execute(
            "SELECT id, ts, source, floor, message FROM events ORDER BY ts DESC LIMIT ?", (limit * 4,)
        ).fetchall()

    events = []
    for r in rows:
        related = [
            {"id": o["id"], "source": o["source"], "message": o["message"]}
            for o in all_recent
            if o["id"] != r["id"]
            and abs(o["ts"] - r["ts"]) <= window_seconds
            and o["source"] != r["source"]
            and (o["floor"] == r["floor"] or o["floor"] is None or r["floor"] is None)
        ]
        events.append({
            "id": r["id"], "ts": r["ts"], "source": r["source"], "severity": r["severity"],
            "category": r["category"], "device_name": r["device_name"], "floor": r["floor"],
            "message": r["message"],
            "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
            "related": related,
        })
    return events


def latest_unifi_snapshots() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            """SELECT s.* FROM unifi_snapshots s
               JOIN (SELECT device_mac, MAX(ts) AS max_ts FROM unifi_snapshots GROUP BY device_mac) latest
               ON s.device_mac = latest.device_mac AND s.ts = latest.max_ts"""
        ).fetchall()
        floor_rows = _conn.execute("SELECT device_mac, floor, source FROM device_floor_map").fetchall()
    floor_map = {r["device_mac"]: (r["floor"], r["source"]) for r in floor_rows}
    result = []
    for r in rows:
        raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
        floor, floor_source = floor_map.get(r["device_mac"], (None, None))
        result.append({
            "device_mac": r["device_mac"], "device_name": r["device_name"], "device_type": r["device_type"],
            "state": r["state"], "ts": r["ts"],
            "model": raw.get("model"), "uptime": raw.get("uptime"), "satisfaction": raw.get("satisfaction"),
            "num_sta": raw.get("num_sta"), "floor": floor or None, "floor_source": floor_source,
            "ip": raw.get("ip") or (raw.get("connect_request_ip")),
        })
    return result


def latest_device_names() -> dict[str, str]:
    """Zuletzt bekannter Name je UniFi-Gerät (MAC) - für die Erkennung von
    Umbenennungen auch über Server-Neustarts hinweg."""
    with _lock:
        rows = _conn.execute(
            """SELECT s.device_mac, s.device_name FROM unifi_snapshots s
               JOIN (SELECT device_mac, MAX(ts) AS max_ts FROM unifi_snapshots GROUP BY device_mac) latest
               ON s.device_mac = latest.device_mac AND s.ts = latest.max_ts"""
        ).fetchall()
    return {r["device_mac"]: r["device_name"] for r in rows if r["device_name"]}


def prune(now: float) -> None:
    """Alte Daten entfernen, damit die Datenbank nicht unbegrenzt wächst.
    UniFi-Snapshots (große Roh-JSONs, nur der jeweils letzte wird angezeigt): 24 h.
    Messwerte der Agenten: 14 Tage. Ereignisse: 90 Tage."""
    with _lock:
        _conn.execute("DELETE FROM unifi_snapshots WHERE ts < ?", (now - 24 * 3600,))
        _conn.execute("DELETE FROM measurements WHERE ts < ?", (now - 14 * 86400,))
        _conn.execute("DELETE FROM events WHERE ts < ?", (now - 90 * 86400,))
        _conn.commit()


def events_since(ts: float) -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM events WHERE ts >= ? ORDER BY ts ASC", (ts,)
        ).fetchall()
    return [
        {
            "id": r["id"], "ts": r["ts"], "source": r["source"], "severity": r["severity"],
            "category": r["category"], "device_name": r["device_name"], "floor": r["floor"],
            "message": r["message"],
        }
        for r in rows
    ]


# --- Einstellungen (persistent, per GUI editierbar) ---------------------

def get_setting(key: str, default=None):
    with _lock:
        row = _conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def has_setting(key: str) -> bool:
    with _lock:
        row = _conn.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone()
    return row is not None


def set_setting(key: str, value) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        _conn.commit()


# --- Stockwerk-Zuordnung für UniFi-Geräte --------------------------------
# floor = '' bedeutet: bewusst nicht zugeordnet (blockiert die Automatik).
# source = 'auto' | 'manual' - manuelle Zuordnungen werden nie überschrieben.

def set_device_floor(mac: str, floor: Optional[str], source: str = "manual") -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO device_floor_map (device_mac, floor, source) VALUES (?, ?, ?) "
            "ON CONFLICT(device_mac) DO UPDATE SET floor = excluded.floor, source = excluded.source",
            (mac, floor or "", source),
        )
        _conn.commit()


def get_floor_map() -> dict[str, str]:
    with _lock:
        rows = _conn.execute("SELECT device_mac, floor FROM device_floor_map").fetchall()
    return {r["device_mac"]: r["floor"] for r in rows if r["floor"]}


def get_floor_entries() -> dict[str, dict]:
    """Alle Zuordnungs-Einträge inkl. Quelle (auch bewusst leere)."""
    with _lock:
        rows = _conn.execute("SELECT device_mac, floor, source FROM device_floor_map").fetchall()
    return {r["device_mac"]: {"floor": r["floor"], "source": r["source"]} for r in rows}
