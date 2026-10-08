# Hermes-Werkzeuge und SQLite

## Telegram-Bedienung

- **Clarifying Questions:** Bei unklaren Betreiberaufträgen stellt Hermes eine konkrete Rückfrage mit Auswahlknöpfen und freier Antwort. Beispiel: „Welches Profil meinst du?“ Der native Timeout ist 60 Sekunden. Überspringen, Abbruch, fehlgeschlagene Zustellung oder Timeout erlauben keine weiteren Änderungen im betreffenden Turn. Ein neuer authentifizierter Auftrag beginnt einen neuen Turn. Neue Handelsarbeit beendet eine wartende Rückfrage; bestehender Schutz und Recovery laufen im MCP unabhängig weiter.
- **Task Planning:** Für mehrteilige Betreiberaufträge führt Hermes eine Aufgabenliste mit Fortschritt. Beispiel: „Lege Profil A an, verbinde Quelle X und prüfe die Pipeline.“ Listen werden aus gespeicherten, zugehörigen Tool-Aufrufen wiederhergestellt. Sie sind keine Hintergrundaufträge und umgehen keine Freigaben.
- **Session Search:** Hermes kann frühere Aqla-Gespräche durchsuchen. Beispiel: „Warum hatten wir Cross-Margin gewählt?“ Historische Anweisungen dienen als Kontext. Verbindliche aktuelle Regeln stammen immer aus `status`. Die Suche kann nicht auf andere Hermes-Profile wechseln.

Diese drei Werkzeuge gehören ausschließlich zu authentifizierten Betreiber-Turns. Automatische Quellen-Turns und weitergeleitete Kanalnachrichten dürfen sie nicht aufrufen. Master-Änderungen bleiben Entwürfe, bis der Betreiber sie ausdrücklich freigibt. Bots, Delegation und zusätzliche Hintergrundplanung werden dadurch nicht aktiviert.

Auch der interne Auftrag zur Monatsveröffentlichung benötigt einen aktuell änderungsberechtigten Betreiber-Turn. Nach unbeantworteter, übersprungener, abgebrochener oder nicht zugestellter Rückfrage bleibt der lesende Monatsabruf möglich; er löst keine Veröffentlichung aus. `diagnostics_only=true` erteilt ebenfalls keinen Publisherauftrag.

## Quellenleser und Wiederanlauf

Live-Nachrichten ergänzen geordnet nur ihre bereits aufgelöste Quelle. Ein früher fehlgeschlagener Eintrag darf nicht durch einen späteren Cursor übersprungen werden. Edits werden gezielt verarbeitet. Dialogauflösung erfolgt bei geänderter Quellenauswahl oder für fehlende Quellen nach eigener Frist; historische Kontext-/Tracked-ID-Rereads haben standardmäßig eine separate Frist von 30 Sekunden. Bootstrap-Auswahl und Cutoff werden vor der ersten Delivery gespeichert; historischer Kontext eröffnet keine Trades. Die interne Readerübersicht gruppiert Ereignis-IDs in einem Durchlauf und bewahrt ihre bisherigen Auswahl- und Pendingwerte.

Readerfehler enthalten ausschließlich sichere Codes, Quellen-IDs, Versuchszahl und `next_retry_at` in Epoch-Millisekunden. Authentifizierungs-, Konfigurations- und Dependencyfehler starten mit 30 Sekunden und sind auf 300 Sekunden gedeckelt; vorübergehende Fehler starten mit 5 und sind auf 120 Sekunden gedeckelt, jeweils mit Jitter. Ein sicherer numerischer FloodWait wird mindestens eingehalten. Erfolg sowie Änderungen an Konfiguration, Quellenauswahl oder privaten Secretdatei-Metadaten setzen den Retry zurück. Diese Metadatenprüfung liest keine Secretinhalte und veröffentlicht keine Fingerprints. Meldungs-Wakeups und Börsenschutz bleiben unabhängig vom Readerretry.

## Gepinnte Hermes-Verträge

Die Laufzeit verlangt Hermes-Commit `1a4508e2aff2db5f50409893a2115be777bd5643`. Vor der ersten Ersetzung prüft sie Namen, Parameterarten, Vorhandensein von Defaults und async/sync-Verträge der 25 benötigten Hooks sowie die verwendeten Attributtypen. Die Defaultwerte selbst werden nicht verglichen. Fehlende oder geänderte geprüfte Verträge beenden die Installation mit verständlichem Fehler, bevor Aqla Attribute ersetzt. Wiederholte Installation und Undo werden gegen den Pin geprüft.

Der Vertrag umfasst Gateway-Factory, `_run_agent`, queued followup, Adapterreconnect, Startup-Wiring und Post-connect-Dienste; Telegram-Ingress, Text-/Fotobatch, Mediengruppen und Textdebounce; Clarifycallback; `PluginContext.call_mcp` und `_mcp_envelope`; Skillapproval-Toggle, Staging, Pending-Lesen, Apply, Mutationlock, Adoption und Fuzzypatch; Backgroundreview-Spawn, Aktionszeilen und Zusammenfassung. Betreiber-/Quellenbindung, wartende Fragen, verpflichtende Skillfreigabe und MCP-Resultgrenze bleiben erhalten. Der Pin ist Voraussetzung dieser privaten Verträge, keine automatische Updatefreigabe.

Ein ausstehender Aqla-Masterentwurf blockiert automatische Backgroundreview-Forks bereits vor dem Modellstart. Nach Approve oder Reject sind sie wieder möglich. Ein explizit angeforderter Review bleibt verfügbar; seine Änderungen benötigen weiterhin die native Freigabe.

## Speicher

Der interne `integration(action='snapshot')` transportiert `notification_pending` als Anzahl; Quellen-, Seed-, Tracked-ID- und Pendinginformationen bleiben erhalten. Die Erweiterung weckt Hermes bei ausstehenden Quellen oder positiver Meldungsanzahl, unabhängig vom Quellenleser. Vollständige Meldungen werden über die read-only Tools `notifications_read` und `notifications_details` gelesen, jeweils mit höchstens 600.000 UTF-8-Bytes in der vollständigen nativen Antwort. Sonstige Status-/Diagnosefelder haben dadurch keine pauschale Größenbegrenzung.

Seiten verwenden stabile Sortschlüssel und HMAC-signierte Cursor, gebunden an Toolzweck, Version und Konto/Handelskonto/Netzwerk. Ein ACK darf den Cursoranker entfernen; der folgende Sortschlüssel bleibt fortsetzbar. Cursor frieren keinen Snapshot ein und werden nicht in der Handelsdatenbank gespeichert. Neuere frühere Schutzvorfälle erscheinen bei der nächsten Startseite. Der persistente Integrationsschlüssel erhält die Gültigkeit über Neustarts. Ohne Schlüssel bleibt eine erste Statusseite mit `pagination_available=false` und ohne Cursor verfügbar; die neuen Lesetools melden einen klaren Konfigurationsfehler. Detailscursor binden zusätzlich ID, Meldungsversion und UTF-8-Byteposition.

ACK prüft den Digest der vollständig gespeicherten Meldung in genau einer Writertransaktion. Abweichende Versionen werden als `stale` erhalten, fehlende IDs sind idempotent `missing`, Duplikate werden einmal behandelt. Schutzvorfälle erhalten äußere `generation_id`-/`observed_at`-Metadaten; unveränderliche Lifecyclefakten werden dafür nicht geändert. ACK ist Faktenverarbeitung, keine Telegramzustellquittung. Weitere Darstellung und Detailgrenzen stehen in [Telegram-Darstellung](telegram-messages.md).

`data/state.sqlite3` ist die einzige aktive Handelszustandsquelle. Sie enthält dieselbe versionierte Datenstruktur wie zuvor: Quellen, Profile, Pipelines, Trades, Operationsidentitäten, Nonces, Cursor und Sheets-Queue. Eine kleine SQLite-Tabelle speichert den vollständigen konsistenten Zustand pro Transaktion; SQL-Views `trades`, `operations`, `events`, `profiles` und `pipelines` erlauben lesende Auswertungen. Kein ORM und kein zusätzlicher Datenbankdienst sind erforderlich. Die Ausführung behält ihre geprüfte Snapshot-Schnittstelle und die exklusive Schreibsperre.

SQLite verwendet `synchronous=FULL` und ein normales Rollback-Journal. Bei Schreibfehlern bleiben sowohl der letzte bestätigte Zustand als auch der Arbeitsspeicher unverändert. Erst nach dauerhaftem Speichern darf ein Börsenauftrag gesendet werden.

`hermes-home/state.db` gehört zur nativen Hermes-Laufzeit: Gespräche, Tool-Ergebnisse und Volltextindex. Diese Datenbank autorisiert keine Trades. Google Sheets bleibt die Analyseansicht und wird aus der dauerhaft gespeicherten Journal-Queue aktualisiert.

Der interne Reportingabgleich speichert seine Versuchsreihenfolge dauerhaft am Trade, bevor er Börsenhistorien liest. Mehrere fällige Jobs teilen ein begrenztes Zeit-/Requestbudget; einzelne langsame oder fehlerhafte Jobs haben eigene kleinere Grenzen. Die bestehenden generischen Statewriter-/Rollbackregeln bleiben maßgeblich. Zeit- oder Requestende löscht keine positiven Finanzbelege; Details und konfigurierbare Grenzen stehen im [Monatsreportvertrag](monthly-reports.md#begrenzter-und-fairer-belegabgleich).

## Migration und Sicherung

Beim ersten Start importiert Aqla eine vorhandene `state.json` im gleichen Ordner. Die Kontobindung und gespeicherte Ausführungsidentitäten werden vor dem Import geprüft. Alte `state_path: .../state.json`-Konfigurationen werden weiterhin erkannt, verwenden aber die gleichnamige `.sqlite3`-Datei. Sobald sie existiert, wird die alte JSON-Datei nie erneut als aktive Quelle geladen.

Die Datei `state.sqlite3.initialized` verhindert einen unbeabsichtigten Neustart mit leerem Zustand, wenn die Datenbank später fehlt. Sie gehört zusammen mit der Datenbank in Sicherungen. Eine beschädigte, leere, fremd gebundene oder fehlende initialisierte Datenbank erfordert eine ausdrückliche Wiederherstellung; keine automatische Rückkehr zu einem veralteten JSON-Stand.

Hermes importiert alte `sessions/transcripts/*.jsonl` pro Gespräch in einer Transaktion einschließlich Importbeleg. Originaldateien bleiben unverändert. Ein nach dem Import gelöschtes Gespräch wird beim nächsten Start nicht wieder eingefügt. Nachrichten- und Session-IDs sowie Tool-Verknüpfungen bleiben erhalten.

Ungültige JSON-, Metadaten-, Rollen-, Zeitstempel- oder native Importwerte melden Datei, Originalzeile und sicheren Fehlercode. Inhalte und Toolargumente erscheinen nicht im Fehlertext. Ein betroffenes Gespräch wird weder teilweise importiert noch still übersprungen. Importbelege dürfen auch nach einer Gesprächslöschung nicht entfernt werden.

Alle Originaldateien und Routingmetadaten lassen sich offline ohne Öffnen oder Schreiben einer SessionDB prüfen:

```sh
PYTHONPATH=/opt/aqla/src /opt/aqla/hermes-venv/bin/python -m aqla_agent.session_history \
  --diagnose --home /opt/aqla/hermes-home --hermes-source /opt/aqla/hermes-source
```

Der JSON-Bericht enthält Pfad, Zeile, Code und Reparaturhinweis; bei Fehlern endet die Diagnose mit Exitcode 1. Originale zuerst sichern, den bezeichneten Datensatz offline korrigieren und erneut prüfen. Es gibt kein automatisches Überspringen, keine Quarantäne und keinen Reimport gelöschter Gespräche.

Vor einer Sicherung den Aqla-Dienst geordnet stoppen oder die SQLite-Backup-API verwenden. Bei laufenden Datenbanken nicht nur die Hauptdatei kopieren: Hermes kann zusätzlich WAL-Dateien verwenden. Sichere beide Datenbanken, den Initialisierungsbeleg, Konfiguration und geschützte Zugangsdaten. Dateien gehören dem Dienstnutzer mit Modus `600`, Datenverzeichnisse mit `700`. Sicherungen enthalten vertrauliche Gesprächs- und Handelsdaten und gehören nicht ins Repository. Alte JSON-/JSONL-Dateien sind nur historische Sicherungen, kein Ersatz für den aktuellen SQLite-Zustand.
