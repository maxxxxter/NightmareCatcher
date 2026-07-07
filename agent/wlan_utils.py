from __future__ import annotations

import platform
import re
import subprocess
from typing import Optional, TypedDict


class WlanInfo(TypedDict):
    connection_type: str
    ssid: Optional[str]
    wlan_signal_percent: Optional[int]
    wlan_channel: Optional[int]


def get_wlan_info() -> Optional[WlanInfo]:
    """Liest den WLAN-Status über `netsh` aus (nur Windows).

    Berücksichtigt sowohl englische als auch deutsche Windows-Sprachausgabe
    von `netsh wlan show interfaces`.
    Gibt None zurück, wenn kein aktives WLAN besteht (z.B. bei LAN-Verbindung).
    """
    if platform.system().lower() != "windows":
        return None

    try:
        proc = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, errors="replace", timeout=5,
        )
        text = proc.stdout
    except Exception:
        return None

    state_match = re.search(r"^\s*(?:State|Status)\s*:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    if not state_match:
        return None
    state_value = state_match.group(1).strip().lower()
    if "connected" not in state_value and "verbunden" not in state_value:
        return None

    ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", text, re.MULTILINE)
    signal = re.search(r"^\s*Signal\s*:\s*(\d+)%", text, re.MULTILINE)
    channel = re.search(r"^\s*(?:Channel|Kanal)\s*:\s*(\d+)", text, re.MULTILINE)

    return {
        "connection_type": "WLAN",
        "ssid": ssid.group(1).strip() if ssid else None,
        "wlan_signal_percent": int(signal.group(1)) if signal else None,
        "wlan_channel": int(channel.group(1)) if channel else None,
    }
