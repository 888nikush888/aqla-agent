# Qualitätsfreigabe für `high` mit Tools

## Vollständiger Nachweis vom 18. Oktober 2026

Die unveränderten 76 Originalfälle und die 30 unabhängigen Blindfälle sind in jeweils drei nativen Durchläufen korrekt: **318/318**. Alle Ergebnisse wurden erneut gegen die versiegelten Referenzen bewertet, mit identischem Code und Master, ausschließlich `high` über OpenCode Go und ohne echte Testorders. Synthetische native Regressionen prüfen zusätzlich Verbindungsabbrüche, Rate-Limits und das Ende nach drei Transportversuchen.

| Datensatz | Beobachtungen | Endergebnis korrekt | Erster Entwurf korrekt | Mittel inkl. Prozess-/MCP-Start | Modellanfragen | Transportwiederholungen | Geschätzter Verbrauch |
|---|---:|---:|---:|---:|---:|---:|---:|
| Original | 228 | 228 | 194 | 43.21 s | 723 | 0 | $1.0479 |
| Blind | 90 | 90 | 85 | 31.72 s | 277 | 0 | $0.3932 |

Die längsten vollständigen Fallzeiten betrugen: Original 257.22 s; Blind 86.72 s.

Über alle erfassten nativen Entwicklungs- und Qualitätsläufe einschließlich verworfener Läufe: **18535 bekannte Modellanfragen**, **$24.2084 geschätzter bekannter Kontingentverbrauch**. Produktionsanfragen sind darin nicht enthalten. Nicht vollständig erfasste fehlgeschlagene oder beim Abbruch laufende Anfragen können zusätzlich verbraucht haben; dieser unbekannte Anteil wird nicht als null gerechnet.

Erstversuch und Endergebnis sind getrennt: Die Tool-Schleife darf höchstens drei Interpretationsentwürfe verwenden. Alle gemessenen API-Aufrufe einschließlich Korrekturen und Transportversuchen sind enthalten. Die Schätzung verwendet die veröffentlichten tokenabhängigen [OpenCode-Go-Kontingentpreise](https://opencode.ai/v2/docs/console/go/#usage-limits), keine ausgelesene Abonnementabbuchung; Reasoning-Tokens sind bereits Teil der Ausgabe. Unbekannter Verbrauch abgebrochener früherer Entwicklungsanfragen bleibt unbekannt und ist keine Null. Frühere verworfene Entwicklungs- und Blindläufe zählen nicht zum bestandenen Nachweis, ihr bekannter Verbrauch wird separat erfasst.

Die frühere Messung ohne native Tools ist kein unmittelbar vergleichbarer Laufzeitmaßstab. Dieser Nachweis gilt für die geprüften Fälle. Neue unklare Quellen bleiben gesperrt; Risikogrenzen, Eigentumsnachweis und erneute Prüfung vor jeder schreibenden Wirkung gelten unverändert. Die Installer-Vorgabe darf mit diesem Nachweis auf `high` wechseln; eine laufende Instanz gilt erst nach eigener Ausroll- und Betriebsprüfung als umgestellt.

## Wiederholbarkeit

Der native Runner `scripts/evaluate_high_tools.py` prüft Datensatz-Hashes, begrenzte Korrekturen, unabhängige Durchläufe und getrennten Verbrauch. Private Originalfälle, Blindsätze und Zugangsdaten bleiben außerhalb des Repositories. Verworfene Entwicklungsstände sind ausschließlich in der Git-Historie dokumentiert. Die Referenzen werden nicht an Modellantworten angepasst.
