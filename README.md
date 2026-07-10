# NightmareCatcher

Ein Werkzeug, um gelegentlichen Lags/Rucklern auf den Grund zu gehen. Läuft verteilt: ein
leichter **Agent** auf jedem PC/Notebook misst laufend Ping/Jitter/Paketverlust
zu Router, Etagen-Switch und Internet und meldet das an einen zentralen
**Server**. Der Server fragt zusätzlich den lokalen **UniFi-Controller** ab
(Switches, Router **und WLAN-Access-Points**) und zeigt alles in einem
Dashboard an — inklusive Speedmeter für die aktuelle Internetauslastung und
einfacher Ereignis-Korrelation ("IPTV-Ruckler zeitgleich mit Fehlern am
1.OG-Switch").

Server und Agent werden komplett über eine **grafische Oberfläche im Browser
bzw. als Desktop-Fenster** konfiguriert — keine manuelle Bearbeitung von
YAML-Dateien im laufenden Betrieb mehr nötig.

## Architektur

```
PC/Notebook (Agent, GUI)  ──┐
PC/Notebook (Agent, GUI)  ──┼──►  Server (FastAPI + SQLite)  ──►  Dashboard + Einstellungen (Browser)
PC/Notebook (Agent, GUI)  ──┘              │
                                            └──►  UniFi-Controller: Switches, Router, WLAN-APs (lokale API)
```

- **Agent** (`agent/`): Python, keine Admin-Rechte nötig. Zwei Startarten:
  - `agent_gui.py` — Desktop-Fenster (Tkinter) zur dauerhaften Konfiguration
    (Gerätename, Stockwerk, Server-URL, Ziel-IPs, Schwellwerte) inkl.
    Speichern-Button, Verbindungstest und Start/Stopp der Messung.
  - `agent.py config.yaml` — reiner Kommandozeilen-Betrieb ohne GUI, z.B. für
    einen Dienst/Task ohne Desktop-Session.
- **Server** (`server/`): nimmt Meldungen aller Agenten entgegen, pollt
  parallel den UniFi-Controller (Switches, Router, APs), speichert alles in
  SQLite und stellt zwei Seiten bereit: `/` (Dashboard) und `/settings`
  (Einstellungen).

## Schnellstart mit den EXE-Dateien (empfohlen)

Fertig gebaute Programme, kein Python nötig:

- **Server:** `server\dist\NightmareCatcher-Server.exe` — doppelklicken.
  Startet den Server (Port 8000), öffnet automatisch das Dashboard im Browser
  und legt ein **Tray-Symbol** in der Taskleiste ab (lila Kreis mit N). Kein
  Konsolenfenster; beenden per Rechtsklick auf das Tray-Symbol → "Beenden",
  Doppelklick öffnet das Dashboard. Logausgaben landen in
  `nightmarecatcher-server.log` neben der EXE.
  (Start ohne Browserfenster: `--no-browser`, ohne Tray: `--no-tray`)
- **Agent:** `agent\dist\NightmareCatcher-Agent.exe` — doppelklicken. Alle
  Einstellungen (Gerätename, Stockwerk, Server-URL, Messziele, Schwellwerte)
  werden über das Menü **"Einstellungen"** gepflegt — mit "Empfohlen:"-Hinweis
  unter jedem Feld, wie beim Server. Gespeichert wird dauerhaft in `agent.db`
  neben der EXE; eine vorhandene `config.yaml` wird beim ersten Start
  automatisch importiert und als `.imported.bak` gesichert. Ändert man
  Einstellungen bei laufender Messung, startet der Agent automatisch mit den
  neuen Werten neu. Das Schließen des Fensters (X) minimiert nur in das
  **Tray-Symbol** — der Agent misst weiter. Beenden per Rechtsklick auf das
  Symbol → "Beenden" (oder Datei → Beenden im Fenster).

Beide EXEs legen ihre Daten (`netdiag.db` bzw. `config.yaml`) im Ordner ab,
aus dem sie gestartet werden. Die Datenbank bereinigt sich automatisch
(UniFi-Rohdaten nach 24 h, Messwerte nach 14 Tagen, Ereignisse nach 90 Tagen).
Neu bauen nach Codeänderungen: `server\build_exe.ps1` bzw. `agent\build_exe.ps1`.

## Einrichtung aus dem Quellcode (Alternative zu den EXEs)

### 1. Server (auf einem dauerhaft laufenden PC)

```powershell
cd server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.yaml config.yaml
```

`config.yaml` dient nur noch als **einmalige Startbefüllung** beim allerersten
Serverstart (Server-Host/Port, Datenbankpfad, Erstbelegung der Einstellungen).
Danach werden alle Werte — UniFi-Zugangsdaten, Schwellwerte, WAN-Bandbreite —
ausschließlich über die Einstellungen-Seite im Browser geändert und landen
direkt in der SQLite-Datenbank (`netdiag.db`), nicht mehr in der YAML-Datei.

Server starten:

```powershell
python run.py
```

Dashboard: `http://<server-ip>:8000` &nbsp;·&nbsp; Einstellungen: `http://<server-ip>:8000/settings`

**Empfehlung:** UniFi-Zugangsdaten direkt auf der Einstellungen-Seite eingeben
statt in `config.yaml`, damit sie nur in der (bereits per `.gitignore`
ausgeschlossenen) Datenbank liegen und nicht zusätzlich als Klartext in einer
Konfigurationsdatei.

**UniFi-Nutzer anlegen:** Im UniFi-Network-Controller unter
*Einstellungen → Admins* einen zusätzlichen **lokalen** Administrator mit
eingeschränkter Rolle (Nur-Lese reicht) anlegen. Der Nutzer braucht ein
lokales Passwort (kein reiner Cloud-SSO-Account), sonst schlägt der API-Login
fehl. Bei selbstsigniertem Zertifikat (Standard bei UniFi) auf der
Einstellungen-Seite "Zertifikat streng prüfen" deaktiviert lassen.

### 2. Agent (auf jedem PC/Notebook, das mitmessen soll)

```powershell
cd agent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python agent_gui.py
```

Alle Einstellungen (Gerätename, Stockwerk Keller/EG/1.OG/2.OG, Server-URL,
Messziele, Schwellwerte) über das Menü **"Einstellungen"** pflegen — unter
jedem Feld steht der empfohlene Wert. Gespeichert wird dauerhaft in
`agent.db`; eine alte `config.yaml` wird beim ersten Start automatisch
importiert. Mit **Verbindung testen** prüfen, dann **Agent starten**.

Für Dauerbetrieb ohne offenes Fenster: `agent_gui.py` per Windows-
Aufgabenplanung beim Anmelden starten — die gespeicherten Einstellungen
werden automatisch geladen. Für vollautomatischen Start ohne GUI:
`python agent.py` (nutzt agent.db) oder `python agent.py config.yaml`
(explizite Konfigurationsdatei).

### 3. Geräte den Stockwerken zuordnen

Auf der Einstellungen-Seite ("Geräte & Stockwerke") erscheinen alle vom
UniFi-Controller automatisch erkannten Geräte — Switches, Router **und
WLAN-Access-Points**. Über ein Dropdown pro Gerät die Etage zuweisen; der
Gerätename im UniFi-Controller selbst muss dafür nicht geändert werden. Die
Zuordnung wird sofort gespeichert und danach im Dashboard sowie in der
Ereignis-Korrelation verwendet.

## Neu in v1.3.0

- **Geräte-Wächter**: eigene Überwachungsziele (Einstellungen → "Geräte-Wächter"),
  z.B. die IPTV-Box per Ping oder ein Server-Dienst per TCP-Port-Check —
  erscheinen im Dauerping-Panel und im Latenz-Verlauf.
- **WLAN-Roaming-Erkennung**: AP-Wechsel aller WLAN-Clients werden protokolliert;
  häufige Wechsel in kurzer Zeit (Ping-Pong-Roaming) erzeugen eine Warnung.
- **Störungs-Heatmap**: Häufigkeit aller Warnungen nach Wochentag × Uhrzeit —
  macht Muster bei sporadischen Fehlern sichtbar.
- **Speedtest-Automatik**: löst im eingestellten Intervall den Speedtest des
  UniFi-Gateways aus und warnt, wenn der Durchsatz unter den Schwellwert fällt.
- **Update-Hinweis** in der Navigation, wenn auf GitHub eine neuere Version liegt.
- **Einstellungen exportieren/importieren** (Einstellungen → "Sicherung").

## Dashboard lesen

- **Internetauslastung**: zwei Speedmeter (Download/Upload) mit dem aktuellen
  Durchsatz am UniFi-Gateway, alle paar Sekunden aktualisiert. Der Skalen-
  Referenzwert (z.B. deine gebuchte Bandbreite) lässt sich auf der
  Einstellungen-Seite anpassen.
- **Dauerping** (direkt unter dem Speedmeter): Der Server pingt permanent
  (Standard alle 2 s) das Gateway (192.168.1.1) und das Internet (1.1.1.1) an
  und zeigt die letzten ~5 Minuten als Verlaufskurve — Latenzspitzen und
  Ausfälle (rote Punkte) sind sofort sichtbar und erzeugen automatisch ein
  Ereignis in der Zeitleiste. Ziele, Intervall und Schwellwerte sind in den
  Einstellungen änderbar.
- **Router (UDM)**: Gesundheitswerte des Gateways, die Latenzen erklären
  können — CPU- und Speicher-Auslastung, Temperatur, vom Gateway gemessene
  Internet-Latenz, WAN-Status und Laufzeit (erkennt auch unbemerkte
  Neustarts). Überschreitungen erzeugen Ereignisse in der Zeitleiste; die
  Schwellwerte sind auf der Einstellungen-Seite anpassbar.
- **Geräte-Status**: pro PC/Notebook der aktuelle Zustand je Messziel (Gateway,
  Etagen-Switch, Internet). Grün = stabil, Gelb = auffällig, Rot = kritisch,
  Grau = keine aktuellen Daten.
- **UniFi-Infrastruktur**: Live-Status aller UniFi-Geräte (Switch/AP/Router)
  inkl. zugeordneter Etage, Zufriedenheitswert und Client-Anzahl.
- **Ereignis-Zeitleiste**: alle Auffälligkeiten chronologisch, inkl. WLAN-
  spezifischer Ereignisse (Funk-Zufriedenheit, Kanalauslastung) und Funden aus
  den **UniFi-Controller-Logs**, die der Server permanent nach möglichen
  Fehlerquellen durchsucht (Verbindungsabbrüche, Neustarts, WAN-Wechsel,
  DFS-Radar/Kanalwechsel, STP/Loops). Ereignisse, die zeitlich zusammenfallen
  (Standard ±30 s), werden als "Zeitgleich" verknüpft angezeigt.

## E-Mail-Berichte

Auf der Einstellungen-Seite ("E-Mail-Berichte") SMTP-Zugangsdaten und
Empfängeradresse hinterlegen, Intervall in Stunden wählen (z.B. 24 für einen
Tagesbericht) und mit "Test-Mail senden" prüfen. Der Bericht fasst alle
Auffälligkeiten des Zeitraums zusammen und listet auf, **welche Geräte an den
Lags beteiligt waren** (sortiert nach Häufigkeit, mit Etage und Störungsart).
Bei Anbietern wie Gmail/GMX/Web.de ist meist ein App-Passwort nötig.

## Vorgehen bei der Fehlersuche

1. Agenten mindestens 1–2 Tage durchlaufen lassen, bis genug Ereignisse
   gesammelt sind (die Störungen treten laut Beschreibung nur gelegentlich auf).
2. Bei einer bemerkten IPTV-Störung die Uhrzeit notieren und im Dashboard in
   der Zeitleiste nachschauen, was zeitgleich auf welcher Etage/welchem
   Switch oder Access Point passiert ist.
3. Wiederholen sich Port-Fehler immer am selben Switch-Port, deutet das auf
   Kabel/Hardware hin; treten WLAN-Qualitätsereignisse auf, eher auf
   Funkinterferenz/Kanalwahl/Überlastung; treten Verluste überall gleichzeitig
   auf, eher auf den Internet-Uplink oder das Gateway (dazu passend: Blick auf
   das Speedmeter, ob zeitgleich die Auslastung hoch war).

## Grenzen der aktuellen Version

- Kein gezieltes Multicast-/IPTV-Stream-Monitoring (misst allgemeine
  Netzwerkqualität, nicht den TV-Stream selbst).
- Kein gezieltes Monitoring einzelner Anwendungsprozesse — Verbindungs-
  abbrüche werden indirekt über die allgemeinen Netzwerkmetriken des
  jeweiligen PCs bzw. über den Geräte-Wächter (TCP-Dienst-Check) sichtbar.
- Korrelation ist zeitbasiert und einfach gehalten, keine automatische
  Ursachendiagnose.
- Server-Host/Port bleiben bewusst nur in `config.yaml` (Änderung erfordert
  Neustart des Serverprozesses).
- Für Langzeitbetrieb empfiehlt sich der Server auf einem Gerät, das
  durchgehend läuft (z.B. NAS oder Mini-PC statt Notebook im Standby).
