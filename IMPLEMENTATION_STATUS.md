# aqla Agent — Auslieferungsstand

Stand: 03.10.2026. Neues eigenständiges Python-Projekt und unabhängiges VPS-Quellpaket. Die bestehende Hermes-Installation des Nutzers wurde weder verändert noch angebunden. Kein TSX-Code oder alter Handelszustand wurde übernommen.

## Inhalt

- Native Hermes-Telegram-Kommunikation; eigene schmale Erweiterung für Quellenaufnahme, Wakeups, Herkunft und Skill-Neuladen. Kein eigener Bottransport oder Agentenhost.
- Read-only Telethon-Kanalleser mit StringSession, Kontext, Replies, Edits, periodischem Backfill und serieller Aufnahme vor Cursorfortschritt. Native Owner-Weiterleitungen werden ebenfalls gespeichert.
- Kurzer durch Hermes bearbeitbarer Master; Python prüft Nutzerregeln, Zahlen, Mengen, Marktpräzision, Budgets und Operationsidentitäten.
- Automatischer Perpetual-Signalhandel mit Entry/SL, anteiligen Limit-TPs, Updates, Einstand, Reduktion, Schließen und Wiederanlauf. Spot liegt im vollständigen MCP; die automatische Signalstrecke lehnt Spot vor einer Order ab.
- Eigener vollständiger HyperCore-MCP: 59 Info-Verträge, 40 dokumentierte Exchange-Aktionstypen, 52 öffentliche SDK-Operationen einschließlich der gesonderten Approval-/Multisig-Pfade, 24 WebSocket-Abonnements, HIP-3/HIP-4 und Deployeraktionen. Siehe docs/api-coverage.md. API-Snapshot: 02.10.2026; keine Behauptung unbekannter zukünftiger API-Erweiterungen.
- Atomarer JSON-Handelszustand und JSONL-Gesprächsverlauf. Keine Datenbank. SQLite-Zugriffe und ungenutzte Hermes-Hintergrundprodukte sind im separaten VPS-Profil deaktiviert. Native Hermes-Telegram-Befehle sind verfügbar.
- Installer mit eigenem Hermes-Checkout/Home und systemd-Unit; Aqla Python 3.12, Hermes Python 3.14, beide im Zielordner verwaltet. Gepinnter Hermes-Commit: 1a4508e2aff2db5f50409893a2115be777bd5643.
- Source-ZIP mit SHA-256, Wheel und Source-Distribution; keine lokalen Secrets, Daten oder QA-Umgebungen im Archiv. Eigenständiges privates GitHub-Repository `888nikush888/aqla-agent` mit Qualitätspipeline.

## Geprüft

Ruff; eingefrorene Installation; Paketbuild; separate Wheel-Installation und Offline-Installer; regel- und zustandsbezogene Tests einschließlich Crash vor TP-Versand, verlorenen Antworten, echten Teilfills, beendeter eigener Position mit fremdem Wiedereröffnen, frischer Equity bei Recovery und verlorener niedriger Telegram-ID.

Mit dem tatsächlichen gepinnten Hermes: Gesprächswiederherstellung ohne DB, Gateway-Konstruktion ohne Session-DB, native AIAgent-Hooks und stdio-MCP. Ein lokales geskriptetes Modell prüfte Owner-Regeländerung, Master-Patch, Größenbegrenzung und die Abwehr von Kontrollaufrufen aus einer echten nativen Weiterleitung. Native Boot-Anbindung startet ohne neue Owner-Nachricht; geänderte Skills leeren den nativen Agentencache vor dem nächsten Turn.

Ein echter lokaler WebSocket-Server prüfte Reconnect, Unsubscribe und einen angenommenen Auftrag mit verlorener Antwort: kein erneuter Versand.

## Prüfgrenzen und Einrichtung

Keine echten Telegram-Nachrichten, keine Live-Exchange-Mutationen und keine Anmeldung am Konto des Nutzers. Der Linux-Dienst wurde hier nicht auf einem entfernten VPS gestartet. Zugangsdaten, Kontoadresse, Modell, Quellen und Handelsregeln werden auf dem VPS gesetzt; README.md enthält die Schritte.

Geskriptete Modellprüfungen beweisen Integration und Befugnisse, keine allgemeine sprachliche Erkennungsqualität. Für das gewünschte echte Modell liegen 30 Kontextfälle und ein ausdrücklich auszuführender Eval-Runner ohne Handels-/Telegram-Werkzeuge bei. Text und Captions werden verarbeitet; reine Bildsignale nicht. Native Stop-Ausführung ist kein garantierter maximaler tatsächlicher Verlust.
