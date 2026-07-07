from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ServerCfg:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class UnifiCfg:
    enabled: bool = True
    controller_url: str = ""
    username: str = ""
    password: str = ""
    site: str = "default"
    verify_ssl: bool = False
    poll_interval_seconds: int = 30


@dataclass
class ThresholdsCfg:
    loss_percent_warn: float = 1.0
    loss_percent_crit: float = 8.0
    jitter_ms_warn: float = 10.0
    jitter_ms_crit: float = 40.0
    unifi_port_error_delta_warn: int = 5
    correlation_window_seconds: int = 30
    stale_after_seconds: int = 60


@dataclass
class DatabaseCfg:
    path: str = "netdiag.db"


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    unifi: UnifiCfg = field(default_factory=UnifiCfg)
    thresholds: ThresholdsCfg = field(default_factory=ThresholdsCfg)
    database: DatabaseCfg = field(default_factory=DatabaseCfg)


def load_config(path: str = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        # Kein Fehler: ohne config.yaml starten wir mit Standardwerten
        # (Port 8000, SQLite im Arbeitsverzeichnis). Alle weiteren Einstellungen
        # werden ohnehin über die Einstellungen-Seite gepflegt.
        return Config()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Config(
        server=ServerCfg(**raw.get("server", {})),
        unifi=UnifiCfg(**raw.get("unifi", {})),
        thresholds=ThresholdsCfg(**raw.get("thresholds", {})),
        database=DatabaseCfg(**raw.get("database", {})),
    )
