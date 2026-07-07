"""Laufzeit-Einstellungen, persistent in der SQLite-Datenbank gespeichert.

config.yaml dient nur noch als einmalige Startbefüllung (Bootstrap) beim
allerersten Start. Danach werden alle Werte über die Einstellungen-Seite im
Dashboard geändert und landen direkt in der Datenbank - kein Bearbeiten von
YAML-Dateien mehr nötig, keine Zugangsdaten im Klartext in Konfigurationsdateien.
"""
from __future__ import annotations

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
}

SECRET_KEYS = {"unifi_password", "mail_password"}


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


def get_all(mask_secrets: bool = True) -> dict:
    result = {key: db.get_setting(key, default) for key, default in DEFAULTS.items()}
    if mask_secrets:
        for key in SECRET_KEYS:
            result[key] = bool(result[key])
    return result


def update(partial: dict) -> None:
    for key, value in partial.items():
        if key not in DEFAULTS:
            continue
        if key in SECRET_KEYS and (value is None or value == ""):
            continue  # leeres Passwort-Feld überschreibt das gespeicherte Passwort nicht
        db.set_setting(key, value)
