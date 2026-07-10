"""Windows-Autostart über den Registry-Run-Key des aktuellen Nutzers.

Trägt das laufende Programm (die EXE bzw. den Python-Aufruf) unter
HKCU\\...\\Run ein, sodass es beim Anmelden automatisch startet. Auf
Nicht-Windows-Systemen sind alle Funktionen wirkungslos.
"""
from __future__ import annotations

import sys

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _command() -> str:
    """Startbefehl des aktuellen Programms (mit Anführungszeichen für Pfade)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # Aus dem Quellcode gestartet: python + Skriptpfad
    script = sys.argv[0]
    return f'"{sys.executable}" "{script}"'


def is_supported() -> bool:
    return sys.platform == "win32"


def is_enabled(name: str) -> bool:
    if not is_supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, name)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_enabled(name: str, enabled: bool) -> bool:
    """Aktiviert/deaktiviert den Autostart. Gibt den neuen Zustand zurück."""
    if not is_supported():
        return False
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, _command())
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass
    return enabled
