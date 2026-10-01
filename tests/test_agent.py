"""Tests für die Agent-Logik (Ping-Parsing, Einstellungs-Store)."""
import sys
from pathlib import Path


AGENT = Path(__file__).resolve().parent.parent / "agent"
sys.path.insert(0, str(AGENT))


def _load_agent_ping_utils():
    """Agent-Modul explizit per Dateipfad laden - server/ und agent/ haben
    beide ein ping_utils.py, der Name im Modul-Cache wäre sonst mehrdeutig."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_ping_utils", AGENT / "ping_utils.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ping_parsing_german_output(monkeypatch):
    ping_utils = _load_agent_ping_utils()

    class FakeProc:
        returncode = 0
        stdout = ("Ping wird ausgeführt für 1.1.1.1 mit 32 Bytes Daten:\n"
                  "Antwort von 1.1.1.1: Bytes=32 Zeit=9ms TTL=59\n"
                  "Antwort von 1.1.1.1: Bytes=32 Zeit=11ms TTL=59\n"
                  "Ping-Statistik für 1.1.1.1:\n"
                  "    Pakete: Gesendet = 2, Empfangen = 2, Verloren = 0 (0% Verlust),\n")

    monkeypatch.setattr(ping_utils.subprocess, "run", lambda *a, **k: FakeProc())
    monkeypatch.setattr(ping_utils.platform, "system", lambda: "Windows")
    r = ping_utils.ping_host("1.1.1.1", count=2)
    assert r["sent"] == 2 and r["received"] == 2
    assert r["avg_latency_ms"] == 10.0
    assert r["jitter_ms"] is not None


def test_agent_settings_yaml_import(tmp_path, monkeypatch):
    import agent_settings
    monkeypatch.setattr(agent_settings, "BASE_DIR", tmp_path)
    monkeypatch.setattr(agent_settings, "DB_PATH", tmp_path / "agent.db")
    monkeypatch.setattr(agent_settings, "YAML_PATH", tmp_path / "config.yaml")
    agent_settings._conn = None

    (tmp_path / "config.yaml").write_text(
        "agent:\n  device_name: PC-Test\n  floor: 1.OG\n"
        "targets:\n  gateway: 10.0.0.1\n  internet: [1.1.1.1]\n",
        encoding="utf-8",
    )
    agent_settings.init()
    s = agent_settings.load()
    assert s["device_name"] == "PC-Test"
    assert s["floor"] == "1.OG"
    assert s["gateway"] == "10.0.0.1"
    assert not (tmp_path / "config.yaml").exists()


def test_agent_settings_empty_input_protected(tmp_path, monkeypatch):
    import agent_settings
    monkeypatch.setattr(agent_settings, "BASE_DIR", tmp_path)
    monkeypatch.setattr(agent_settings, "DB_PATH", tmp_path / "agent2.db")
    monkeypatch.setattr(agent_settings, "YAML_PATH", tmp_path / "none.yaml")
    agent_settings._conn = None

    agent_settings.init()
    agent_settings.save({"gateway": "10.1.1.1"})
    agent_settings.save({"gateway": "", "report_interval_seconds": None})
    s = agent_settings.load()
    assert s["gateway"] == "10.1.1.1"
    assert s["report_interval_seconds"] == agent_settings.DEFAULTS["report_interval_seconds"]


def _load_agent_module():
    """agent.py importiert 'ping_utils' - beim gemeinsamen Testlauf liegt unter
    diesem Namen evtl. schon das Server-Modul im Cache. Für den Import daher
    kurzzeitig das Agent-Modul einsetzen."""
    import importlib
    previous = sys.modules.get("ping_utils")
    sys.modules["ping_utils"] = _load_agent_ping_utils()
    try:
        import agent
        importlib.reload(agent)
    finally:
        if previous is not None:
            sys.modules["ping_utils"] = previous
        else:
            del sys.modules["ping_utils"]
    return agent


def test_outbox_flush_stops_on_error(tmp_path):
    from outbox import Outbox
    box = Outbox(tmp_path / "o.db", max_rows=3)
    for i in range(5):
        box.put({"n": i})
    assert box.count() == 3  # älteste fallen weg

    got = []

    def flaky(p):
        if p["n"] == 3:
            raise ConnectionError("weg")
        got.append(p["n"])

    try:
        box.flush(flaky)
    except ConnectionError:
        pass
    assert got == [2] and box.count() == 2
    assert box.flush(lambda p: got.append(p["n"])) == 2
    assert got == [2, 3, 4] and box.count() == 0


def test_agent_buffers_while_offline_and_reports_disconnect(tmp_path):
    agent = _load_agent_module()
    from outbox import Outbox
    import httpx

    received = []
    online = {"up": False}

    class FakeClient:
        def post(self, url, json):
            if not online["up"]:
                raise httpx.ConnectError("offline")
            received.append(json)
            return httpx.Response(200, request=httpx.Request("POST", url))

    cfg = {"agent": {"device_name": "PC", "floor": "EG", "server_url": "http://srv"}}
    box = Outbox(tmp_path / "o.db")
    logs = []

    def payload(ts):
        return {"device_name": "PC", "floor": "EG", "ts": ts, "connection_type": "LAN",
                "measurements": [{"target_label": "Gateway"}], "events": []}

    agent._deliver(FakeClient(), cfg, payload(100.0), box, logs.append)
    agent._deliver(FakeClient(), cfg, payload(110.0), box, logs.append)
    assert received == [] and box.count() == 2 and box.offline_since == 100.0

    online["up"] = True
    agent._deliver(FakeClient(), cfg, payload(190.0), box, logs.append)
    assert box.count() == 0 and box.offline_since is None
    assert [p["ts"] for p in received] == [190.0, 100.0, 110.0, 100.0]
    disconnect = received[-1]["events"][0]
    assert disconnect["category"] == "disconnect" and disconnect["severity"] == "critical"
    assert disconnect["detail"]["duration_s"] == 90.0


def test_agent_drops_payload_rejected_by_server(tmp_path):
    agent = _load_agent_module()
    from outbox import Outbox
    import httpx

    class RejectingClient:
        def post(self, url, json):
            return httpx.Response(422, request=httpx.Request("POST", url))

    box = Outbox(tmp_path / "o.db")
    cfg = {"agent": {"device_name": "PC", "floor": "EG", "server_url": "http://srv"}}
    agent._deliver(RejectingClient(), cfg, {"ts": 1.0, "events": []}, box, lambda m: None)
    assert box.count() == 0 and box.offline_since is None


def test_run_cycle_measures_targets_in_parallel(monkeypatch):
    import threading
    import time as time_mod
    agent = _load_agent_module()
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fake_ping(ip, count=8):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time_mod.sleep(0.2)
        with lock:
            active["now"] -= 1
        return {"sent": 4, "received": 4, "avg_latency_ms": 5.0, "jitter_ms": 1.0, "rtts": []}

    monkeypatch.setattr(agent, "ping_host", fake_ping)
    monkeypatch.setattr(agent, "get_wlan_info", lambda: None)
    sent = []
    monkeypatch.setattr(agent, "_deliver", lambda c, cfg, p, o, log: sent.append(p))
    cfg = {"agent": {"device_name": "PC", "floor": "EG", "server_url": "x", "ping_count": 4},
           "anomaly": {"loss_percent_trigger": 1.0, "jitter_ms_trigger": 10.0}}
    targets = [("Gateway", "a"), ("Internet-1", "b"), ("Internet-2", "c")]
    agent.run_cycle(None, cfg, targets)
    assert active["max"] == 3
    assert [m["target_label"] for m in sent[0]["measurements"]] == ["Gateway", "Internet-1", "Internet-2"]
