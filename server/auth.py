"""Zugangsschutz für Einstellungen und Zugangsdaten.

Dashboard und Agent-Meldungen bleiben offen. Alles, was Einstellungen liest
oder ändert, Zugangsdaten exportiert oder Verbindungstests mit gespeicherten
Passwörtern auslöst, ist geschützt:

- Vom Server-PC selbst (127.0.0.1/::1) ist der Zugriff immer erlaubt - dort
  öffnet das Tray-Symbol das Dashboard.
- Von anderen Geräten nur mit dem Admin-Passwort (Header X-Admin-Password, URL-kodiert).
  Ist noch keines vergeben, sind die Einstellungen nur am Server-PC erreichbar.

Das Passwort wird nur als gesalzener PBKDF2-Hash in der Datenbank abgelegt und
ist bewusst kein Teil der normalen Einstellungen (kein Export/Import).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional
from urllib.parse import unquote

from fastapi import HTTPException, Request

import db

SETTING_KEY = "admin_password_hash"
HEADER = "x-admin-password"
_ITERATIONS = 200_000
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _hash(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def set_password(password: Optional[str]) -> None:
    """Setzt das Admin-Passwort; leer/None entfernt es wieder."""
    if not password:
        db.set_setting(SETTING_KEY, None)
        return
    db.set_setting(SETTING_KEY, _hash(password, secrets.token_bytes(16)))


def has_password() -> bool:
    return bool(db.get_setting(SETTING_KEY))


def check_password(password: Optional[str]) -> bool:
    stored = db.get_setting(SETTING_KEY)
    if not stored or not password:
        return False
    try:
        _algo, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def is_local(request: Request) -> bool:
    return bool(request.client) and request.client.host in LOCAL_HOSTS


def require_admin(request: Request) -> None:
    """FastAPI-Abhängigkeit für alle geschützten Endpunkte."""
    if is_local(request):
        return
    if not has_password():
        raise HTTPException(
            status_code=403,
            detail="Einstellungen sind von anderen Geräten aus gesperrt, solange kein "
                   "Admin-Passwort vergeben ist. Bitte am Server-PC unter Einstellungen → "
                   "Zugangsschutz ein Passwort festlegen.",
        )
    # Der Browser schickt das Passwort URL-kodiert (Header erlauben nur ASCII).
    if not check_password(unquote(request.headers.get(HEADER) or "")):
        raise HTTPException(status_code=401, detail="Admin-Passwort erforderlich oder falsch.")
