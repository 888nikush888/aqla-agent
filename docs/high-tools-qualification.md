# Qualitätsfreigabe für `high` mit Tools

Der native Testlauf nutzt ausschließlich OpenCode Go, `high` und die produktiven MCP-Lesewerkzeuge. Jeder Fall erhält einen eigenen Hermes-Prozess und isolierten Zustand. Fake-Börsentransporte lehnen jede Verbindung ab; schreibende Tools sind nicht verfügbar. Ein gültiger Entwurf benötigt weiterhin den getrennten Vergleich mit der eingefrorenen Referenz. Ein fehlgeschlagener Gesamtlauf liefert einen Fehler-Exitcode.

Private historische Quellen, Referenzen, Master, Modellzugang und Ergebnisse gehören außerhalb des öffentlichen Repositories. Der Datensatz enthält `cases` und `protocol.dataset_sha256` beziehungsweise `manifest.dataset_sha256`. Der Runner prüft den Hash, bevor er startet. Erwartungswerte gelangen weder in den Modellkontext noch in das MCP-Fixture. Ergebnisse enthalten Quellcode-, Master- und Datensatz-Hashes; geänderter Code verlangt ein neues Ergebnisverzeichnis.

Beispiel mit separat installiertem, gepinntem Hermes-Python und Backend-Python:

```powershell
$env:HERMES_MODEL_API_KEY = '<nur im privaten Prozess setzen>'
.cache/hermes314-venv/Scripts/python.exe scripts/evaluate_high_tools.py `
  --hermes-source .cache/hermes-pinned `
  --mcp-python .venv/Scripts/python.exe `
  --dataset .cache/private/development.json `
  --master .cache/private/master.txt `
  --output .cache/private/development-results --runs 3
```

Für die getrennten 30 Blindfälle denselben Befehl mit deren versiegeltem Datensatz und eigenem Ergebnisverzeichnis wiederholen. Keine Zugangsdaten als Kommandozeilenargumente, in Logs oder in Git speichern. Drei Entwürfe pro Fall und drei Transportversuche mit 15/30 Sekunden Wartezeit sind fest begrenzt. Alle tatsächlich gemessenen Modellantworten und Anfrageversuche zählen; unbekannter Verbrauch fehlgeschlagener Anfragen bleibt ausdrücklich unbekannt. Tokenbasierte Kosten sind Schätzungen, keine ausgelesenen Abonnementabbuchungen.

Eine Freigabe verlangt bei identischem Code und Master genau 76 × 3 und 30 × 3 verschiedene Fall/Wiederholungs-Paare, alle korrekt, keine verbotenen Tools, keine falsche Handelsfreigabe, keine verlorene Verwaltungsanweisung und keine offenen Prüfungsfehler. Erstversuchsquote, Korrekturaufrufe, gesamte Laufzeit und Verbrauch bleiben getrennt vom Endergebnis. Ein teilweise ausgeführter Test oder `all_passed` einer Fallauswahl genügt nicht. Wird ein gescheiterter Blindfall zur Verbesserung verwendet, sind anschließend 30 neue Blindfälle nötig.

## Offener Referenzwiderspruch vom 17. Oktober 2026

Ein eingefrorener Entwicklungsfall fordert `unrelated` für einen Kommentar, der Tokenkonzentration als Grund für mangelnde Analysierbarkeit und spekulatives Handelsrisiko nennt. Der bereits vor dieser Änderung bestehende Klassenvertrag definiert `unrelated` als Begrüßung, Werbung, Einladung oder **nichtfinanziellen** allgemeinen Kommentar; Finanzanalyse gehört zu `report`. Der aktuelle Modelllauf liefert mit entsprechendem Originalbeleg `report`.

Diese Diskrepanz betrifft die Referenz beziehungsweise Klassengrenze, nicht einen fehlenden Zahlenbeleg oder eine Handelsfreigabe. Die Referenz wird weder umgeschrieben noch als gleichwertig gewertet. Ein zusätzliches, nur zur passenden Referenz erfundenes Sonderkriterium wäre kein Qualitätsnachweis. Bis der Widerspruch fachlich geklärt und ein neuer, ausdrücklich genehmigter Nachweis definiert ist, bleibt die Freigabe für `high` gesperrt. Die reservierten Blindfälle bleiben unbenutzt; Installer und VPS bleiben auf der bisherigen Einstellung `ultra` (API-Parameter `max`).

Nach bestandener Freigabe müssen GitHub-CI, Installer-Vorgabe, tatsächlicher API-Parameter `high` und mindestens zwei vollständige Sync-Zyklen auf dem VPS geprüft werden. Rückfall betrifft Software und Modellkonfiguration; eine Finanzdatenbank wird niemals auf einen alten Stand zurückgesetzt.
