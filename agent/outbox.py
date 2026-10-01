"""Zwischenspeicher für Messungen, die der Server gerade nicht annimmt.

Gerade bei Verbindungsabbrüchen - also genau den Momenten, die NightmareCatcher
aufdecken soll - erreicht der Agent den Server nicht. Statt die Messwerte zu
verwerfen, landen sie hier (SQLite-Datei neben dem Programm) und werden
nachgeliefert, sobald der Server wieder erreichbar ist. Der Server übernimmt
den ursprünglichen Messzeitpunkt, die Werte erscheinen also an der richtigen
Stelle in Verlauf und Zeitleiste.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

MAX_ROWS = 50_000  # bei 10-s-Takt knapp 6 Tage; älteste Einträge fallen zuerst weg


class Outbox:
    def __init__(self, path: Path | str, max_rows: int = MAX_ROWS):
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
        )
        self._conn.commit()
        # Zeitpunkt der ersten fehlgeschlagenen Übertragung des aktuellen
        # Ausfalls (None = Server erreichbar).
        self.offline_since: Optional[float] = None

    def put(self, payload: dict) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO outbox (payload) VALUES (?)", (json.dumps(payload),))
            self._conn.execute(
                "DELETE FROM outbox WHERE id <= (SELECT MAX(id) FROM outbox) - ?", (self.max_rows,)
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def flush(self, send: Callable[[dict], None], limit: int = 300) -> int:
        """Liefert bis zu `limit` gespeicherte Meldungen (älteste zuerst) über
        `send` aus. Bricht beim ersten Fehler ab; der Rest bleibt gespeichert.
        Gibt die Anzahl der erfolgreich übertragenen Meldungen zurück."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, payload FROM outbox ORDER BY id ASC LIMIT ?", (limit,)
            ).fetchall()
        sent = 0
        for row_id, raw in rows:
            send(json.loads(raw))  # Fehler -> Abbruch, Eintrag bleibt erhalten
            with self._lock:
                self._conn.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
                self._conn.commit()
            sent += 1
        return sent

    def close(self) -> None:
        with self._lock:
            self._conn.close()
