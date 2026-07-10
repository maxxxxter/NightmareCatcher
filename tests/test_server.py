"""Automatische Tests für die kritische Server-Logik.

Ausführen (im Projektverzeichnis, mit aktivierter server/.venv):
    python -m pytest tests -q
"""
import importlib
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """Frische DB + neu geladene Module je Test (globaler DB-Zustand)."""
    import db
    import settings as settings_mod
    importlib.reload(db)
    importlib.reload(settings_mod)
    db.init(str(tmp_path / "test.db"))
    return db, settings_mod


# --- Einstellungs-Schutz ---------------------------------------------------

def test_unmodified_values_follow_recommendation(fresh):
    db, settings = fresh
    from config import Config
    db.set_setting("jitter_ms_warn", 15.0)  # alter Wert, nie manuell geändert
    settings.seed_from_bootstrap(Config())
    assert settings.get_all(mask_secrets=False)["jitter_ms_warn"] == settings.DEFAULTS["jitter_ms_warn"]


def test_manual_change_survives_update(fresh):
    db, settings = fresh
    from config import Config
    settings.seed_from_bootstrap(Config())
    settings.update({"jitter_ms_warn": 25.0})
    settings.seed_from_bootstrap(Config())  # simuliertes App-Update
    assert settings.get_all(mask_secrets=False)["jitter_ms_warn"] == 25.0


def test_empty_input_does_not_wipe_value(fresh):
    db, settings = fresh
    from config import Config
    settings.seed_from_bootstrap(Config())
    settings.update({"ping_target_local": "192.168.0.1"})
    settings.update({"ping_target_local": ""})  # leeres Feld
    assert settings.get_all(mask_secrets=False)["ping_target_local"] == "192.168.0.1"


def test_empty_password_keeps_existing(fresh):
    db, settings = fresh
    from config import Config
    settings.seed_from_bootstrap(Config())
    settings.update({"unifi_password": "geheim"})
    settings.update({"unifi_password": ""})
    assert settings.get_all(mask_secrets=False)["unifi_password"] == "geheim"
    assert settings.get_all(mask_secrets=True)["unifi_password"] is True


def test_cache_invalidated_on_update(fresh):
    db, settings = fresh
    from config import Config
    settings.seed_from_bootstrap(Config())
    _ = settings.get_all()  # füllt Cache
    settings.update({"wan_max_mbps": 999})
    assert settings.get_all(mask_secrets=False)["wan_max_mbps"] == 999


# --- Ping-Historie / Downsampling -----------------------------------------

def test_ping_history_buckets_and_loss(fresh):
    db, _ = fresh
    base = 1_000_000.0
    for i in range(20):
        rtt = None if i % 5 == 0 else float(i)  # jeder 5. ist Timeout
        db.insert_ping_history(base + i, {"Gateway": rtt})
    points = db.ping_history("Gateway", base - 1, max_points=4)
    assert 1 <= len(points) <= 4
    assert any(p["loss_percent"] > 0 for p in points)
    assert all(p["avg_ms"] is None or p["avg_ms"] >= 0 for p in points)


def test_ping_history_empty(fresh):
    db, _ = fresh
    assert db.ping_history("Gateway", 0) == []


# --- Grade-Logik -----------------------------------------------------------

def test_grade_thresholds(fresh):
    import main
    assert main._grade(5, 10, 50) == "ok"
    assert main._grade(20, 10, 50) == "warning"
    assert main._grade(60, 10, 50) == "critical"
    assert main._grade(None, 10, 50) == "unknown"


def test_guess_floor(fresh):
    import main
    assert main.guess_floor("Switch EG") == "EG"
    assert main.guess_floor("AP 1.OG") == "1.OG"
    assert main.guess_floor("Switch Büro") == "2.OG"
    assert main.guess_floor("Keller-Switch") == "Keller"
    assert main.guess_floor("namenloses Gerät") is None


# --- Mail-Bericht ----------------------------------------------------------

def test_mail_report_lists_devices(fresh):
    import mailer
    events = [
        {"ts": 1000, "severity": "critical", "category": "packet_loss",
         "device_name": "PC-Büro", "floor": "1.OG", "message": "Verlust 30%"},
        {"ts": 1001, "severity": "warning", "category": "jitter",
         "device_name": "PC-Büro", "floor": "1.OG", "message": "Jitter hoch"},
    ]
    subject, body = mailer.build_report(events, 900, 1100)
    assert "PC-Büro" in body
    assert "2" in subject
    assert "Paketverlust" in body


def test_mail_report_empty(fresh):
    import mailer
    subject, body = mailer.build_report([], 0, 100)
    assert "keine" in subject.lower()
    assert "NightmareCatcher" in subject
