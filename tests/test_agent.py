"""Tests für die Agent-Logik (Ping-Parsing, Einstellungs-Store)."""
import sys
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "agent"
sys.path.insert(0, str(AGENT))


def test_ping_parsing_german_output(monkeypatch):
    import ping_utils

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
