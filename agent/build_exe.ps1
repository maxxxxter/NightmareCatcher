# Baut die Agent-EXE (dist\NightmareCatcher-Agent.exe).
# Voraussetzung: einmalig `python -m venv .venv` und `pip install -r requirements.txt`.
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name NightmareCatcher-Agent `
    --collect-submodules pystray `
    agent_gui.py
Write-Host "Fertig: dist\NightmareCatcher-Agent.exe"
