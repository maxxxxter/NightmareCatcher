from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("netdiag.unifi")


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

    async def _login(self) -> None:
        try:
            resp = await self._client.post(
                f"{self.base}/api/auth/login",
                json={"username": self.username, "password": self.password},
            )
            if resp.status_code == 200:
                self._is_unifi_os = True
                self._csrf_token = resp.headers.get("x-csrf-token") or resp.headers.get("x-updated-csrf-token")
                log.info("UniFi-Login erfolgreich (UniFi OS)")
                return
        except httpx.HTTPError as e:
            log.debug("UniFi-OS-Login nicht möglich, versuche klassischen Controller: %s", e)

        resp = await self._client.post(
            f"{self.base}/api/login",
            json={"username": self.username, "password": self.password},
        )
        resp.raise_for_status()
        self._is_unifi_os = False
        log.info("UniFi-Login erfolgreich (klassischer Controller)")

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
