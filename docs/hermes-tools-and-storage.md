# Hermes-Werkzeuge und SQLite

## Telegram-Bedienung

- **Clarifying Questions:** Bei unklaren Betreiberaufträgen stellt Hermes eine konkrete Rückfrage mit Auswahlknöpfen und freier Antwort. Beispiel: „Welches Profil meinst du?“ Der native Timeout ist 60 Sekunden. Überspringen, Abbruch, fehlgeschlagene Zustellung oder Timeout erlauben keine weiteren Änderungen im betreffenden Turn. Ein neuer authentifizierter Auftrag beginnt einen neuen Turn. Neue Handelsarbeit beendet eine wartende Rückfrage; bestehender Schutz und Recovery laufen im MCP unabhängig weiter.
- **Task Planning:** Für mehrteilige Betreiberaufträge führt Hermes eine Aufgabenliste mit Fortschritt. Beispiel: „Lege Profil A an, verbinde Quelle X und prüfe die Pipeline.“ Listen werden aus gespeicherten, zugehörigen Tool-Aufrufen wiederhergestellt. Sie sind keine Hintergrundaufträge und umgehen keine Freigaben.
- **Session Search:** Hermes kann frühere Aqla-Gespräche durchsuchen. Beispiel: „Warum hatten wir Cross-Margin gewählt?“ Historische Anweisungen dienen als Kontext. Verbindliche aktuelle Regeln stammen immer aus `status`. Die Suche kann nicht auf andere Hermes-Profile wechseln.

Diese drei Werkzeuge gehören ausschließlich zu authentifizierten Betreiber-Turns. Automatische Quellen-Turns und weitergeleitete Kanalnachrichten dürfen sie nicht aufrufen. Master-Änderungen bleiben Entwürfe, bis der Betreiber sie ausdrücklich freigibt. Bots, Delegation und zusätzliche Hintergrundplanung werden dadurch nicht aktiviert.

## Speicher

`data/state.sqlite3` ist die einzige aktive Handelszustandsquelle. Sie enthält dieselbe versionierte Datenstruktur wie zuvor: Quellen, Profile, Pipelines, Trades, Operationsidentitäten, Nonces, Cursor und Sheets-Queue. Eine kleine SQLite-Tabelle speichert den vollständigen konsistenten Zustand pro Transaktion; SQL-Views `trades`, `operations`, `events`, `profiles` und `pipelines` erlauben lesende Auswertungen. Kein ORM und kein zusätzlicher Datenbankdienst sind erforderlich. Die Ausführung behält ihre geprüfte Snapshot-Schnittstelle und die exklusive Schreibsperre.

SQLite verwendet `synchronous=FULL` und ein normales Rollback-Journal. Bei Schreibfehlern bleiben sowohl der letzte bestätigte Zustand als auch der Arbeitsspeicher unverändert. Erst nach dauerhaftem Speichern darf ein Börsenauftrag gesendet werden.

`hermes-home/state.db` gehört zur nativen Hermes-Laufzeit: Gespräche, Tool-Ergebnisse und Volltextindex. Diese Datenbank autorisiert keine Trades. Google Sheets bleibt die Analyseansicht und wird aus der dauerhaft gespeicherten Journal-Queue aktualisiert.

## Migration und Sicherung

Beim ersten Start importiert Aqla eine vorhandene `state.json` im gleichen Ordner. Die Kontobindung und gespeicherte Ausführungsidentitäten werden vor dem Import geprüft. Alte `state_path: .../state.json`-Konfigurationen werden weiterhin erkannt, verwenden aber die gleichnamige `.sqlite3`-Datei. Sobald sie existiert, wird die alte JSON-Datei nie erneut als aktive Quelle geladen.

Die Datei `state.sqlite3.initialized` verhindert einen unbeabsichtigten Neustart mit leerem Zustand, wenn die Datenbank später fehlt. Sie gehört zusammen mit der Datenbank in Sicherungen. Eine beschädigte, leere, fremd gebundene oder fehlende initialisierte Datenbank erfordert eine ausdrückliche Wiederherstellung; keine automatische Rückkehr zu einem veralteten JSON-Stand.

Hermes importiert alte `sessions/transcripts/*.jsonl` pro Gespräch in einer Transaktion einschließlich Importbeleg. Originaldateien bleiben unverändert. Ein nach dem Import gelöschtes Gespräch wird beim nächsten Start nicht wieder eingefügt. Nachrichten- und Session-IDs sowie Tool-Verknüpfungen bleiben erhalten.

Vor einer Sicherung den Aqla-Dienst geordnet stoppen oder die SQLite-Backup-API verwenden. Bei laufenden Datenbanken nicht nur die Hauptdatei kopieren: Hermes kann zusätzlich WAL-Dateien verwenden. Sichere beide Datenbanken, den Initialisierungsbeleg, Konfiguration und geschützte Zugangsdaten. Dateien gehören dem Dienstnutzer mit Modus `600`, Datenverzeichnisse mit `700`. Sicherungen enthalten vertrauliche Gesprächs- und Handelsdaten und gehören nicht ins Repository. Alte JSON-/JSONL-Dateien sind nur historische Sicherungen, kein Ersatz für den aktuellen SQLite-Zustand.
