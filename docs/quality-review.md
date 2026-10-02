# Prüfung vor der GitHub-Erstveröffentlichung

Stand: 03.10.2026. Geprüft wurden Ausführung und Wiederanlauf, Quellenaufnahme und Herkunft, native Hermes-Anbindung, Deployment, Abhängigkeiten sowie Git-/Artefakt-Hygiene. Die Reviews waren lesend und wurden durch gezielte lokale Regressionstests ergänzt. Keine echten Exchange-, Telegram- oder Modellaktionen.

## Korrigierte Befunde

| Befund | Auswirkung | Korrektur und Nachweis |
|---|---|---|
| Positionsabfrage konnte einen später sichtbar werdenden eigenen TP-Fill als zusätzliche externe Reduktion verbuchen | Falscher Abschluss und Entfernung des Stops bei noch offener Position | Keine abgeleiteten Reduktionsbuchungen; widersprüchliche Abfragen behalten Schutz bis zum Fill-Abgleich. `test_fill_between_history_and_position_reads_keeps_live_protection` |
| Terminalstatus wurde vor dem Aufräumen eigener Exit-Orders gespeichert | Nach einem Absturz konnten alte Orders eine spätere Position beeinflussen | Dauerhafter Cleanup-Zustand hält Marktbesitz; exakte Orders werden vor Freigabe als beendet bestätigt. Wiederanlauf umfasst auch alte Terminaldatensätze. `test_crash_before_exit_cleanup_keeps_market_reserved_until_restart` |
| Historischer Cutoff fehlte bei Live-Edits und beim erneuten Abruf bekannter Nachrichten | Eine historische Kontextrevision konnte als neuer Entry erscheinen | Einheitliche Cutoff-Klassifikation für Backfill, Rereads und Live-Ereignisse. `test_historical_edits_stay_context_in_reread_and_live_paths` |
| Native Prüfkommandos vermischten Quell- und Deploymentverzeichnisse | Die dokumentierten Befehle waren nach Installation nicht ausführbar | README nutzt explizite `AQLA_SOURCE`- und `AQLA_DEPLOY`-Pfade |
| Source-Distribution konnte trotz ignoriertem QA-Verzeichnis eine dortige `.env.example` aufnehmen | Lokale Referenz-/Testdateien gelangten unbeabsichtigt ins Quellpaket | Explizite sdist-Dateiliste und Prüfung jedes Archivmitglieds über `scripts/verify_artifacts.py` |

## Dauerhafte CI-Prüfungen

- Frozen-Installation, Lint, Tests und Branch-Coverage auf Python 3.12, 3.13 und 3.14. Die lokale Ausgangsmessung liegt bei 75 Prozent; die Pipeline verlangt mindestens 70 Prozent.
- API-Abdeckung und lokaler WebSocket-Test für Reconnect, Unsubscribe und verlorene Antworten.
- Tatsächlicher gepinnter Hermes-Checkout: Dateiverlauf ohne DB, native Befehle, Quellenautorität, stdio-MCP, Nutzerregeln und Masterbearbeitung. Modellantworten sind dabei lokal geskriptet.
- Checksum-gepinnter Gitleaks-Scanner über die Git-Historie, Verbot lokaler Secret-/Statepfade und pip-audit über gehashte, eingefrorene Runtime-/Telegram-Abhängigkeiten. GitHub-Actions sind auf Commit-SHAs gepinnt; Dependabot prüft deren Updates monatlich.
- Paketbuild, Archivinhalt, SHA-256, getrennte Wheel-Installation und Offline-Deploymentvorbereitung. CI-Berichte und geprüfte Pakete liegen als GitHub-Artefakte vor.

## Grenzen

Die Prüfung ist kein formaler Beweis aller Zustandsübergänge, kein vollständiges Audit der Hermes-Abhängigkeiten und kein Live-Trading-Test. Ein sauberer Advisory-Scan bedeutet, dass zum Scanzeitpunkt keine bekannten Advisories für die geprüften Versionen gemeldet wurden. Sprachliche Signalerkennung muss mit dem tatsächlich gewählten Modell über den beiliegenden Eval-Runner geprüft werden. VPS-Dienststart und echte Telegram-Zustellung sind getrennte Betriebsprüfungen.
