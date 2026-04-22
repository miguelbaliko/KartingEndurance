# APX GP War Room — Anleitung für Ueli

## Was das ist
Ein Python-Programm für euren APX GP Kommandostand.

Es zeigt:
- Position
- Fahrer
- Stintzeit
- Pace AVG3 / AVG5 / AVG10
- Trend
- Kart-Rating
- Boxfenster
- Undercut-Hinweis
- klare Empfehlung: HOLD / PREPARE / BOX / ATTACK
- Kamera-Feeds

## So startest du es

### Windows
Doppelklick:

```text
run_windows.bat
```

Oder Konsole:

```bash
python app.py
```

Dann öffnen:

```text
http://127.0.0.1:8080/
```

Für den Strategen:

```text
http://127.0.0.1:8080/strategist
```

## Auf 2 PCs
Laptop 1 läuft als Server.
Laptop 2 öffnet im Browser:

```text
http://IP-DES-SERVER-LAPTOPS:8080/
```

## Wichtig
Der Live-Scraper für Alcanede muss vom Programmierer auf die echte Apex-Timing-Seite angepasst werden. Bis dahin läuft das System im Manual-Modus mit `data/manual_input.csv`.

## APX Logo
Das Paket enthält ein professionelles APX-GP-SVG als Platzhalter.
Wenn du das echte Logo hast: Datei ersetzen unter:

```text
static/apx_logo.svg
```

oder der Entwickler passt den HTML-Link auf `apx_logo.png` an.
