"""Laufzeit-Einstellungen, persistent in der SQLite-Datenbank gespeichert.

config.yaml dient nur noch als einmalige Startbefüllung (Bootstrap) beim
allerersten Start. Danach werden alle Werte über die Einstellungen-Seite im
Dashboard geändert und landen direkt in der Datenbank - kein Bearbeiten von
YAML-Dateien mehr nötig, keine Zugangsdaten im Klartext in Konfigurationsdateien.
"""
from __future__ import annotations

from typing import Optional

import db
from config import Config

DEFAULTS: dict = {
    "unifi_enabled": True,
    "unifi_controller_url": "",
    "unifi_username": "",
    "unifi_password": "",
    "unifi_site": "default",
    "unifi_verify_ssl": False,
    "unifi_poll_interval_seconds": 30,

    "loss_percent_warn": 1.0,
    "loss_percent_crit": 8.0,
    "jitter_ms_warn": 10.0,
    "jitter_ms_crit": 40.0,
    "unifi_port_error_delta_warn": 5,
    "correlation_window_seconds": 30,
    "stale_after_seconds": 60,
    "event_cooldown_seconds": 300,

    "wifi_satisfaction_warn": 70,
    "wifi_satisfaction_crit": 50,
    "wifi_cu_warn": 60,
    "wifi_cu_crit": 85,

    "wan_max_mbps": 500,

    "gw_cpu_warn": 70,
    "gw_cpu_crit": 90,
    "gw_mem_warn": 85,
    "gw_mem_crit": 95,
    "gw_temp_warn": 75,
    "gw_temp_crit": 90,
    "wan_latency_ms_warn": 25,
    "wan_latency_ms_crit": 80,

    "server_ping_interval_seconds": 2,
    "ping_target_local": "192.168.1.1",
    "ping_target_internet": "1.1.1.1",
    "ping_local_warn_ms": 10,
    "ping_local_crit_ms": 50,
    "ping_internet_warn_ms": 50,
    "ping_internet_crit_ms": 150,

    "mail_enabled": False,
    "mail_smtp_host": "",
    "mail_smtp_port": 587,
    "mail_use_tls": True,
    "mail_username": "",
    "mail_password": "",
    "mail_from": "",
    "mail_to": "marc@radler.org",
    "mail_report_interval_hours": 24,
    "mail_instant_alerts": False,
    "mail_instant_cooldown_minutes": 15,

    # Geräte-Wächter: frei konfigurierbare Überwachungsziele, z.B.
    # [{"name": "IPTV-Box", "host": "192.168.1.60", "port": null}]
    # port gesetzt -> TCP-Verbindungstest (Dienst), sonst Ping.
    "watch_targets": [],
    "watch_warn_ms": 100,
    "watch_crit_ms": 500,

    "speedtest_interval_hours": 6,
    "speedtest_warn_percent": 50,

    "monitor_watch": True,
    "monitor_ping": True,
    "monitor_agents": True,
    "monitor_ports": True,
    "monitor_wifi": True,
    "monitor_gateway": True,
    "monitor_wan": True,
    "monitor_controller_logs": True,
}

SECRET_KEYS = {"unifi_password", "mail_password"}

# Schlüssel, deren Wert bei einem Update auf die neue Empfehlung aktualisiert
# werden darf, wenn er nie manuell geändert wurde. Verbindungs- und
# Identitätsdaten (UniFi, Mail) stehen bewusst NICHT hier - sie stammen vom
# Nutzer und dürfen nie automatisch überschrieben werden.
THRESHOLD_KEYS = {
    "loss_percent_warn", "loss_percent_crit", "jitter_ms_warn", "jitter_ms_crit",
    "unifi_port_error_delta_warn", "correlation_window_seconds", "stale_after_seconds",
    "event_cooldown_seconds",
    "wifi_satisfaction_warn", "wifi_satisfaction_crit", "wifi_cu_warn", "wifi_cu_crit",
    "gw_cpu_warn", "gw_cpu_crit", "gw_mem_warn", "gw_mem_crit",
    "gw_temp_warn", "gw_temp_crit", "wan_latency_ms_warn", "wan_latency_ms_crit",
    "ping_local_warn_ms", "ping_local_crit_ms", "ping_internet_warn_ms", "ping_internet_crit_ms",
    "server_ping_interval_seconds", "unifi_poll_interval_seconds",
    "watch_warn_ms", "watch_crit_ms", "speedtest_interval_hours", "speedtest_warn_percent",
}

# Merkliste der manuell (über die GUI) geänderten Schlüssel. Nur Werte, die
# hier NICHT drinstehen, folgen bei App-Updates automatisch der jeweils
# aktuellen Empfehlung.
_MODIFIED_KEY = "_user_modified_keys"


def _get_modified() -> set:
    return set(db.get_setting(_MODIFIED_KEY, []))


def _save_modified(modified: set) -> None:
    db.set_setting(_MODIFIED_KEY, sorted(modified))


def seed_from_bootstrap(cfg: Config) -> None:
    """Befüllt beim ersten Start die DB aus config.yaml. Läuft danach nie wieder."""
    seed = {
        "unifi_enabled": cfg.unifi.enabled,
        "unifi_controller_url": cfg.unifi.controller_url,
        "unifi_username": cfg.unifi.username,
        "unifi_password": cfg.unifi.password,
        "unifi_site": cfg.unifi.site,
        "unifi_verify_ssl": cfg.unifi.verify_ssl,
        "unifi_poll_interval_seconds": cfg.unifi.poll_interval_seconds,
        "loss_percent_warn": cfg.thresholds.loss_percent_warn,
        "loss_percent_crit": cfg.thresholds.loss_percent_crit,
        "jitter_ms_warn": cfg.thresholds.jitter_ms_warn,
        "jitter_ms_crit": cfg.thresholds.jitter_ms_crit,
        "unifi_port_error_delta_warn": cfg.thresholds.unifi_port_error_delta_warn,
        "correlation_window_seconds": cfg.thresholds.correlation_window_seconds,
        "stale_after_seconds": cfg.thresholds.stale_after_seconds,
    }
    for key, default in DEFAULTS.items():
        if not db.has_setting(key):
            db.set_setting(key, seed.get(key, default))

    # Einmalige Migration für Bestands-Datenbanken: alles außer Schwellwerten,
    # das von der Empfehlung abweicht, gilt als bewusst gesetzt (z.B. per
    # config.yaml eingespielte Zugangsdaten) und wird geschützt.
    if not db.has_setting(_MODIFIED_KEY):
        modified = set()
        for key, default in DEFAULTS.items():
            if key in THRESHOLD_KEYS:
                continue
            if db.get_setting(key, default) != default:
                modified.add(key)
        _save_modified(modified)

    # Nicht manuell geänderte Werte folgen automatisch der aktuellen Empfehlung -
    # so bleiben z.B. die Ping-Ziele und Schwellwerte nach Updates auf dem
    # empfohlenen Stand, solange der Nutzer sie nie selbst angefasst hat.
    modified = _get_modified()
    for key, default in DEFAULTS.items():
        if key in modified:
            continue
        if db.get_setting(key, default) != default:
            db.set_setting(key, default)
    _invalidate_cache()


# In-Memory-Cache der kompletten Einstellungen. get_all() wird sehr häufig
# aufgerufen (u.a. im 2-Sekunden-Ping-Loop) - ohne Cache wären das je Aufruf
# ~55 einzelne SQLite-Abfragen. Der Cache wird bei jeder Änderung geleert.
_cache: Optional[dict] = None


def _invalidate_cache() -> None:
    global _cache
    _cache = None


def get_all(mask_secrets: bool = True) -> dict:
    global _cache
    if _cache is None:
        _cache = {key: db.get_setting(key, default) for key, default in DEFAULTS.items()}
    result = dict(_cache)
    if mask_secrets:
        for key in SECRET_KEYS:
            result[key] = bool(result[key])
    return result


def get_recommended() -> dict:
    """Empfohlene Werte für die Anzeige unter den Eingabefeldern (ohne Secrets)."""
    return {key: value for key, value in DEFAULTS.items() if key not in SECRET_KEYS}


def update(partial: dict) -> None:
    modified = _get_modified()
    for key, value in partial.items():
        if key not in DEFAULTS:
            continue
        default = DEFAULTS[key]
        if key in SECRET_KEYS and (value is None or value == ""):
            continue  # leeres Passwort-Feld überschreibt das gespeicherte Passwort nicht
        # Leere Eingaben überschreiben nie eine sinnvolle Voreinstellung: ein
        # leer gelassenes Zahlenfeld (None) oder ein geleertes Textfeld, für das
        # eine Empfehlung existiert, wird ignoriert. So können z.B. die
        # Ping-Ziele nicht versehentlich durch Speichern einer halb geladenen
        # Seite gelöscht werden.
        if value is None and not isinstance(default, str):
            continue
        if value == "" and default != "":
            continue
        db.set_setting(key, value)
        # Wert entspricht wieder der Empfehlung -> folgt künftigen Updates;
        # abweichender Wert -> dauerhaft geschützt.
        if value == DEFAULTS[key]:
            modified.discard(key)
        else:
            modified.add(key)
    _save_modified(modified)
    _invalidate_cache()
