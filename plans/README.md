# aqla Agent — Analyse und Bauplan

Stand: 03.10.2026. Der ursprüngliche Analyseauftrag wurde anschließend zur vollständigen Implementierung und unabhängigen VPS-Vorbereitung erweitert.

**Aktueller Auftrag:** Eigene Hermes-Installation und eigenes HERMES_HOME auf dem VPS; keine Anbindung an die lokale Installation. Der ursprüngliche Analysebericht bleibt Referenz, die aktuelle Implementierung und ihre Prüfgrenzen stehen in [IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md).

**Produkt:** Telegram-Nachrichten → Hermes versteht Signal oder Update im Kontext → Python prüft feste Nutzerregeln → eigener Hyperliquid-MCP führt aus → Hermes berichtet im Telegram-Bot.

| Dokument | Zweck | Status |
|---|---|---|
| [Analyse](000-analyse-tsx.md) | Belegte Unterschiede zwischen TSX und dem gewünschten neuen Produkt | Fertig |
| [Bauplan](001-aqla-agent.md) | Ursprünglicher Implementierungsauftrag; spätere Korrekturen und aktueller Stand stehen am Anfang | Implementiert, unabhängiges VPS-Paket |

Der Bauplan verwendet nach Nutzerkorrektur die bereits vom Nutzer eingerichtete native Hermes-Telegram-Anbindung. Eigener Bottransport und AIAgent-Einbettung entfallen. Reihenfolge: native MCP-/Skill-/Ereignisintegration prüfen → Dateizustand und Regeln → Hyperliquid-Kern → Quellen und Kontext → automatische Ausführung/Updates → übrige API-Abdeckung → Abnahme. Der vollständige MCP ist Bestandteil des Ziels; der Handelspfad wird zuerst fertiggestellt und geprüft.

Keine TSX-Fortsetzung, kein Fork, keine Migration, keine Übernahme alter Trades oder Daten. TSX wurde ausschließlich als Referenz gelesen. Der Arbeitsordner hatte bei der Analyse weder Quellcode noch ein Git-Repository.

Geprüfte Referenzstände:

- TSX: `e056b723c13c6aafd9baca22a691f6cdaa70adc2`, Paketversion 0.9.6.
- Hermes: `1a4508e2aff2db5f50409893a2115be777bd5643`.
- Hyperliquid Python SDK: `2fdb18f9517675ea03695a0962bd19eece9c83f0`.

Referenzen werden bei der Implementierung erneut auf Kompatibilität geprüft und reproduzierbar gepinnt. Die Analyse hat keine Abhängigkeiten installiert, keine Tests ausgeführt, keine Server gestartet und keine Börsenaktionen vorgenommen.

## Bewusst nicht als Altprojekt-Reparaturen geplant

- Veraltete README-Zahlen korrigieren: für das neue Produkt ohne Nutzen.
- SurrealDB, MiniApp oder Forschung refaktorieren: diese Funktionen entfallen vollständig.
- Die Update-Bindung im alten TSX reparieren: als Regressionstest für aqla aufgenommen; Altcode bleibt unverändert.
- Normale Anweisungen in TSX-Skills als Angriff melden: sie sind erwartete Anwendungsdaten und wurden nicht als Arbeitsanweisungen übernommen.

## Prüfgrenzen

Die ursprüngliche Analyse war keine vollständige Prüfung aller Altprojektdateien. In aqla sind Dateizustand, Regeln, Ausführung, API-Verträge, native Hermes-Anbindung und Datenbankverzicht inzwischen separat geprüft. Reale Telegram-Zustellung, Live-Exchange-Ausführung und Erkennungsqualität des gewählten Modells sind keine behaupteten Testergebnisse.
