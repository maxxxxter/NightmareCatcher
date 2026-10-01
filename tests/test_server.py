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


# --- v1.3.0: Geräte-Wächter, Roaming, Heatmap -------------------------------

def _load_main(fresh, monkeypatch):
    """main.py frisch laden (nutzt die Test-DB aus dem fresh-Fixture)."""
    monkeypatch.setenv("NETDIAG_CONFIG", "nicht_vorhanden.yaml")
    import main
    importlib.reload(main)
    return main


def test_watch_targets_built_from_settings(fresh, monkeypatch):
    db, settings = fresh
    main = _load_main(fresh, monkeypatch)
    s = dict(settings.get_all(mask_secrets=False))
    s["watch_targets"] = [
        {"name": "IPTV-Box", "host": "192.168.1.60", "port": None},
        {"name": "NAS", "host": "192.168.1.10", "port": 8080},
        {"name": "", "host": "", "port": None},          # leer -> ignoriert
        {"name": "Kaputt", "host": "1.2.3.4", "port": "abc"},  # Port ungültig -> Ping
    ]
    targets = main._check_targets(s)
    labels = [t["label"] for t in targets]
    assert labels[:2] == ["Gateway", "Internet"]
    assert "IPTV-Box" in labels and "NAS" in labels
    assert len(targets) == 5  # leeres Ziel wurde übersprungen
    tcp_target = next(t for t in targets if t["label"] == "NAS")
    assert tcp_target["kind"] == "tcp" and tcp_target["port"] == 8080
    kaputt = next(t for t in targets if t["label"] == "Kaputt")
    assert kaputt["kind"] == "ping" and kaputt["port"] is None


def test_tcp_check_reachable_and_unreachable():
    import socket
    import threading
    from ping_utils import tcp_check

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()
    try:
        rtt = tcp_check("127.0.0.1", port)
        assert rtt is not None and rtt >= 0
    finally:
        srv.close()
    assert tcp_check("127.0.0.1", 1) is None  # geschlossener Port


def test_roaming_pingpong_detection(fresh, monkeypatch):
    db, settings = fresh
    main = _load_main(fresh, monkeypatch)
    s = settings.get_all(mask_secrets=False)
    names = {"ap-a": "AP EG", "ap-b": "AP 1.OG"}

    def client_on(ap):
        return [{"mac": "cc:dd", "ap_mac": ap, "is_wired": False, "hostname": "Notebook"}]

    now = 1000.0
    main._check_roaming(client_on("ap-a"), names, s, now)          # erster Kontakt
    for i in range(1, 5):                                          # 4 Wechsel in 10 min
        ap = "ap-b" if i % 2 else "ap-a"
        main._check_roaming(client_on(ap), names, s, now + i * 60)

    events = db.events_since(0)
    roams = [e for e in events if e["category"] == "wifi_roaming"]
    assert roams, "keine Roaming-Ereignisse erzeugt"
    assert any(e["severity"] == "warning" and "Ping-Pong" in e["message"] for e in roams)
    assert any("AP EG" in e["message"] for e in roams)


def test_heatmap_aggregation(fresh, monkeypatch):
    import asyncio
    import time as time_mod
    db, settings = fresh
    main = _load_main(fresh, monkeypatch)
    now = time_mod.time()
    db.insert_event(now - 60, "server", "warning", "ping_latency", "X", None, "w1", None)
    db.insert_event(now - 120, "server", "critical", "ping_timeout", "X", None, "c1", None)
    db.insert_event(now - 180, "user", "info", "user_marker", "X", None, "marker", None)  # zählt nicht

    result = asyncio.run(main.heatmap(days=1))
    assert result["total"] == 2
    assert result["max"] >= 1
    assert sum(v for row in result["cells"] for v in row) == 2


def test_version_compare(fresh, monkeypatch):
    main = _load_main(fresh, monkeypatch)
    assert main._version_tuple("v1.3.0") == (1, 3, 0)
    assert main._version_tuple("v1.10.0") > main._version_tuple("v1.9.9")
    assert main._version_tuple("unsinn") == (0,)


# --- Zugangsschutz -----------------------------------------------------------

def _clients(main):
    from fastapi.testclient import TestClient
    remote = TestClient(main.app, client=("192.168.1.50", 50000))
    local = TestClient(main.app, client=("127.0.0.1", 50000))
    return remote, local


def test_settings_locked_remotely_without_password(fresh, monkeypatch):
    main = _load_main(fresh, monkeypatch)
    remote, local = _clients(main)
    assert remote.get("/api/settings").status_code == 403
    assert remote.get("/api/settings/export").status_code == 403
    assert local.get("/api/settings").status_code == 200
    # Dashboard-Daten und Agent-Meldungen bleiben offen
    assert remote.get("/api/ping").status_code == 200
    assert remote.post("/api/report", json={"device_name": "PC", "floor": "EG", "ts": 1.0}).status_code == 200


def test_admin_password_unlocks_remote_access(fresh, monkeypatch):
    main = _load_main(fresh, monkeypatch)
    remote, local = _clients(main)
    assert local.put("/api/admin/password", json={"password": "kurz"}).json()["success"] is False
    assert local.put("/api/admin/password", json={"password": "geheim-ü1"}).json()["success"] is True
    assert remote.get("/api/settings").status_code == 401
    assert remote.get("/api/settings", headers={"X-Admin-Password": "falsch"}).status_code == 401
    from urllib.parse import quote
    ok = remote.get("/api/settings", headers={"X-Admin-Password": quote("geheim-ü1")})
    assert ok.status_code == 200
    db, _ = fresh
    assert "geheim" not in str(db.get_setting("admin_password_hash"))  # nur Hash gespeichert


def test_unifi_test_never_sends_saved_password_to_other_url(fresh, monkeypatch):
    db, settings = fresh
    main = _load_main(fresh, monkeypatch)
    settings.update({"unifi_controller_url": "https://192.168.1.1", "unifi_username": "ro",
                     "unifi_password": "topsecret"})
    used = []

    class FakeClient:
        def __init__(self, url, user, password, **kw):
            used.append((url, password))

        async def get_devices(self):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(main, "UnifiClient", FakeClient)
    _, local = _clients(main)
    r = local.post("/api/settings/unifi/test", json={"unifi_controller_url": "https://evil.example"}).json()
    assert r["success"] is False and used == []

    main._last_unifi_test_ts = 0
    r = local.post("/api/settings/unifi/test", json={"unifi_controller_url": "https://192.168.1.1/"}).json()
    assert r["success"] is True and used == [("https://192.168.1.1/", "topsecret")]


def test_mail_test_never_sends_saved_password_to_other_host(fresh, monkeypatch):
    db, settings = fresh
    main = _load_main(fresh, monkeypatch)
    settings.update({"mail_smtp_host": "smtp.example.org", "mail_username": "me",
                     "mail_password": "mailsecret", "mail_to": "me@example.org"})
    seen = []
    monkeypatch.setattr(main.mailer, "send_mail", lambda s, subject, body: seen.append(s["mail_password"]))
    _, local = _clients(main)
    local.post("/api/settings/mail/test", json={"mail_smtp_host": "smtp.evil.example"})
    local.post("/api/settings/mail/test", json={})
    assert seen == ["", "mailsecret"]


# --- Datenbank / Router-Last -------------------------------------------------

def test_old_snapshot_table_is_migrated(tmp_path):
    import json
    import sqlite3
    import db
    path = tmp_path / "alt.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unifi_snapshots (id INTEGER PRIMARY KEY, ts REAL, device_mac TEXT, "
                 "device_name TEXT, device_type TEXT, state INTEGER, raw_json TEXT)")
    for ts, name in ((1.0, "Switch alt"), (2.0, "Switch 1.OG")):
        conn.execute("INSERT INTO unifi_snapshots (ts, device_mac, device_name, device_type, state, raw_json) "
                     "VALUES (?, 'aa', ?, 'usw', 1, ?)", (ts, name, json.dumps({"model": "US8", "ip": "10.0.0.2"})))
    conn.commit()
    conn.close()

    importlib.reload(db)
    db.init(str(path))
    devices = db.latest_unifi_devices()
    assert len(devices) == 1
    assert devices[0]["device_name"] == "Switch 1.OG" and devices[0]["model"] == "US8"
    assert devices[0]["ip"] == "10.0.0.2"
    assert db.latest_device_names() == {"aa": "Switch 1.OG"}
    tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "unifi_snapshots" not in tables


def test_unifi_device_upsert_keeps_one_row(fresh):
    db, _ = fresh
    for ts in (1.0, 2.0, 3.0):
        db.upsert_unifi_device(ts, "aa", "AP EG", "uap", 1, {"num_sta": int(ts)})
    devices = db.latest_unifi_devices()
    assert len(devices) == 1 and devices[0]["ts"] == 3.0 and devices[0]["num_sta"] == 3


def test_wan_rates_from_health(fresh, monkeypatch):
    main = _load_main(fresh, monkeypatch)
    health = [{"subsystem": "lan"}, {"subsystem": "wan", "rx_bytes-r": 1_000_000, "tx_bytes-r": 250_000}]
    assert main._wan_rates_from_health(health) == (1_000_000.0, 250_000.0)
    assert main._wan_rates_from_health([{"subsystem": "wan"}]) is None
    devices = [{"type": "usw"}, {"type": "udm", "wan1": {"rx_bytes-r": 10, "tx_bytes-r": 5}}]
    assert main._wan_rates_from_devices(devices) == (10.0, 5.0)
