from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

log = logging.getLogger("netdiag.unifi")


class UnifiLoginError(Exception):
    """Login abgelehnt oder pausiert - mit verständlicher Ursache."""


class UnifiClient:
    """Kleiner Client für die lokale UniFi-Network-API.

    Unterstützt sowohl UniFi-OS-Konsolen (UDM/UDM-Pro/UDR/Cloud Gateway,
    API unter /proxy/network/...) als auch den klassischen, selbst gehosteten
    Controller (API unter :8443/api/...). Welche Variante zutrifft, wird beim
    ersten Login automatisch erkannt.
    """

    def __init__(self, controller_url: str, username: str, password: str,
                 site: str = "default", verify_ssl: bool = False):
        self.base = controller_url.rstrip("/")
        self.username = username
        self.password = password
        self.site = site
        self._client = httpx.AsyncClient(verify=verify_ssl, timeout=10.0)
        self._is_unifi_os: Optional[bool] = None
        self._csrf_token: Optional[str] = None
        # Backoff nach fehlgeschlagenem Login: verhindert einen Login-Sturm,
        # der sonst das Rate-Limit des Controllers auslöst (HTTP 429) und
        # den Zugang dauerhaft blockiert.
        self._login_blocked_until: float = 0.0
        self._last_login_error: str = ""

    def _block_login(self, reason: str, seconds: float) -> None:
        self._login_blocked_until = time.time() + seconds
        self._last_login_error = reason
        log.warning("UniFi-Login: %s - nächster Versuch in %d s", reason, int(seconds))

    async def _login(self) -> None:
        now = time.time()
        if now < self._login_blocked_until:
            raise UnifiLoginError(
                f"{self._last_login_error} (nächster Login-Versuch in "
                f"{int(self._login_blocked_until - now)} s)"
            )

        credentials = {"username": self.username, "password": self.password}
        try:
            resp = await self._client.post(f"{self.base}/api/auth/login", json=credentials)
        except httpx.HTTPError as e:
            self._block_login(f"Controller nicht erreichbar: {e}", 30)
            raise UnifiLoginError(self._last_login_error)

        if resp.status_code == 200:
            self._is_unifi_os = True
            self._csrf_token = resp.headers.get("x-csrf-token") or resp.headers.get("x-updated-csrf-token")
            self._login_blocked_until = 0.0
            log.info("UniFi-Login erfolgreich (UniFi OS)")
            return

        if resp.status_code == 404:
            # Endpunkt existiert nicht -> klassischer, selbst gehosteter Controller
            try:
                resp2 = await self._client.post(f"{self.base}/api/login", json=credentials)
            except httpx.HTTPError as e:
                self._block_login(f"Controller nicht erreichbar: {e}", 30)
                raise UnifiLoginError(self._last_login_error)
            if resp2.status_code == 200:
                self._is_unifi_os = False
                self._login_blocked_until = 0.0
                log.info("UniFi-Login erfolgreich (klassischer Controller)")
                return
            self._block_login(
                f"Zugangsdaten vom klassischen Controller abgelehnt (HTTP {resp2.status_code})", 900)
            raise UnifiLoginError(self._last_login_error)

        if resp.status_code == 429:
            # Rate-Limit: JETZT NICHT weiter versuchen, sonst hebt es sich nie auf.
            self._block_login(
                "Rate-Limit des Controllers erreicht (HTTP 429, zu viele Login-Versuche). "
                "Warte einige Minuten, das Limit hebt sich von selbst auf", 300)
            raise UnifiLoginError(self._last_login_error)

        # 401/403: Zugangsdaten falsch, Konto gesperrt oder Cloud-Konto ohne lokales
        # Passwort. Langer Backoff (15 min): häufigeres Wiederholen falscher
        # Zugangsdaten ist zwecklos und hält sonst das Rate-Limit des Controllers
        # dauerhaft am Leben. Nach Korrektur der Zugangsdaten in den Einstellungen
        # wird sofort ein frischer Login versucht (neuer Client, kein Backoff).
        self._block_login(
            f"Zugangsdaten abgelehnt (HTTP {resp.status_code}). Nutzername/Passwort auf der "
            f"Einstellungen-Seite prüfen; der Nutzer muss ein lokaler Controller-Nutzer sein", 900)
        raise UnifiLoginError(self._last_login_error)

    def _api_base(self) -> str:
        return f"{self.base}/proxy/network/api" if self._is_unifi_os else f"{self.base}/api"

    async def _get(self, path: str) -> dict:
        if self._is_unifi_os is None:
            await self._login()

        headers = {"x-csrf-token": self._csrf_token} if self._csrf_token else {}
        url = f"{self._api_base()}{path}"
        resp = await self._client.get(url, headers=headers)

        if resp.status_code in (401, 403):
            await self._login()
            headers = {"x-csrf-token": self._csrf_token} if self._csrf_token else {}
            resp = await self._client.get(url, headers=headers)

        resp.raise_for_status()
        return resp.json()

    async def get_devices(self) -> list[dict]:
        """Liefert Switches/APs/Gateway inkl. port_table, Fehlerzählern, Status."""
        data = await self._get(f"/s/{self.site}/stat/device")
        return data.get("data", [])

    async def get_clients(self) -> list[dict]:
        """Liefert verbundene Clients inkl. WLAN-Signalqualität."""
        data = await self._get(f"/s/{self.site}/stat/sta")
        return data.get("data", [])

    async def get_health(self) -> list[dict]:
        """Liefert den Gesundheitsstatus der Subsysteme (www/wan/lan/wlan),
        u.a. mit der vom Gateway gemessenen Internet-Latenz."""
        data = await self._get(f"/s/{self.site}/stat/health")
        return data.get("data", [])

    async def get_events(self, limit: int = 100) -> list[dict]:
        """Liefert die letzten Controller-Ereignisse (Log-Einträge)."""
        data = await self._get(f"/s/{self.site}/stat/event?_limit={limit}")
        return data.get("data", [])

    async def get_alarms(self) -> list[dict]:
        """Liefert die aktiven Alarme des Controllers."""
        data = await self._get(f"/s/{self.site}/list/alarm")
        return data.get("data", [])

    async def close(self) -> None:
        await self._client.aclose()
