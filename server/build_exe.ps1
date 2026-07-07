# Baut die Server-EXE (dist\NightmareCatcher-Server.exe).
# Voraussetzung: einmalig `python -m venv .venv` und `pip install -r requirements.txt`.
# --windowed: kein Konsolenfenster - der Server lebt im Tray-Symbol,
# Logausgaben landen in nightmarecatcher-server.log neben der EXE.
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name NightmareCatcher-Server `
    --add-data "static;static" `
    --collect-submodules uvicorn `
    --collect-submodules pystray `
    run.py
Write-Host "Fertig: dist\NightmareCatcher-Server.exe"
