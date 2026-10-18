# Express für vollständige Einstiegsvorlagen

Express übergibt vollständig erkannte Textvorlagen ohne Modellaufruf an den bestehenden Python-Handelsablauf. Unbekannte Nachrichten bleiben unverändert bei Hermes mit `high`, Tools und Caveman. Sie werden durch Express nicht pauschal abgelehnt oder neu klassifiziert.

## Umfang und Sicherheit

Eine Vorlage enthält genau einen Markt, eine Richtung, eine ausdrückliche Markt-/Limitorder oder einen Entry-Bereich, einen Stop und nummerierte absolute Ziele. Vorhandene Hebelangaben bleiben erhalten; das Profil bestimmt weiterhin den erlaubten tatsächlichen Hebel. Ein Entry-Bereich verwendet die bestehende Profilwahl für eine einzelne Order. Die gesamte Nachricht muss zur Vorlage passen. Unbekannte Zeilen, Kommentare, Werbung und zusätzliche Anweisungen werden niemals abgeschnitten.

Replies, Edits, Weiterleitungen, Medien, historische Quellen, Prozentziele, mehrere Einstiege und mehrdeutige Zahlen bleiben zunächst außerhalb von Express. Vorhandene Zahlenkandidaten, Originalbelege und der gemeinsame Interpretationsprüfer werden wiederverwendet. Vorschau und Ausführung prüfen weiter Quellenrevision, Account, Risiko, Mindestgrößen, Signalalter, Börsenersatz und Schutz. Vor neuem Versand werden Vorlage, Belege und Express-Freigabe erneut kontrolliert. Ein Modell kann keine Express-Freigabe setzen.

Quelle und exklusive Übernahme werden atomar gespeichert. Der Backend-Worker startet unabhängig von einem laufenden KI-Gespräch; Hermes kann dieselbe Revision nicht gleichzeitig interpretieren oder handeln. Quellenänderungen widerrufen ungesendete Restarbeit. Bestehende Finanzoperationen und unbekannte Sendestände gehören ausschließlich ins vorhandene Recovery. Interpretations- und Finanzdatenformate bleiben erhalten.

## Nachweis vom 18. Oktober 2026

Die unveränderten 76 Originalfälle und 30 bisherigen Blindfälle bestehen die Express-Routingprüfung. Zusätzlich wurden vor Implementierung zehn vollständige historische Vorlagen und zwanzig Gegenbeispiele ohne Überschneidung früherer Originale/Reply-Familien versiegelt. Ergebnis: **136/136 korrekte Routingentscheidungen**, zwölf fachlich korrekte Übernahmen und 124 Übergaben an den normalen Pfad; keine falsche Express-Übernahme. Dies ist kein neuer vollständiger Modellnachweis für die 124 zurückgegebenen Nachrichten.

Alle zwölf übernommenen Vorlagen wurden separat mit gepinntem Hermes, echten MCP-Lesewerkzeugen, `high` und tatsächlich nativ geladenem Caveman dreifach geprüft: **36/36 korrekte Endergebnisse**, **20/36 bereits im ersten Entwurf**, 126 Modellanfragen, keine Transportwiederholung oder unbekannten Nutzungsdaten. Caveman steht vor dem unveränderten Master; ein zusätzlicher nativer Regressionstest kontrolliert den tatsächlichen Request. Echte Börsentransporte waren gesperrt.

Für neun Fälle wurden je drei gepaarte Handelsläufe mit identischen Fake-Börsenbedingungen ausgeführt. Drei Gold-/DEX-Fälle bleiben im Interpretationsnachweis und der bestehenden DEX-Testsuite; der einfache Fake-Vergleich umfasst sie nicht. Beide Wege nutzen denselben echten Python-Handelsablauf und dieselben unveränderten Test-Risikoregeln. Ein Limit-Fill wird sofort zum zulässigen Limitpreis simuliert. Positionen, Mengen, Stops und Ziele stimmen in **27/27 Paaren** überein.

| Vergleichbare 27 Beobachtungen | Express | High + Tools + Caveman |
|---|---:|---:|
| Durchschnitt | 0,457 s | 26,562 s |
| Median | 0,455 s | 23,377 s |
| P95 | 0,530 s | 47,270 s |
| Modellanfragen zur Interpretation | 0 | 96 |
| Ungecachte Eingabetokens | 0 | 574.039 |
| Gecachte Eingabetokens | 0 | 999.296 |
| Ausgabetokens inklusive Reasoning | 0 | 68.116 |
| Geschätzter Go-Kontingentwert | 0 | 0,129973 US-Dollar |

Die Laufzeit kombiniert native High-Lese-/Prüfschleife und aufgewärmten gemeinsamen Fake-Handelsablauf einschließlich Fill und Schutz. Prozessstart, echte Netzwerklatenz, tatsächliche Fill-Wartezeit und nachfolgende modellgesteuerte Handels-/Benachrichtigungsaufrufe fehlen. **Dies ist keine Live-Latenzgarantie oder Behauptung, das Gesamtsystem werde kostenfrei.** Spätere Telegram-Meldungen bleiben im bisherigen Zustellweg.

Der gesamte native Vergleich mit 36 Beobachtungen verbrauchte geschätzt 0,170882 US-Dollar Kontingentwert. Grundlage sind die am Prüftag bestätigten [OpenCode-Go-Tokenpreise](https://opencode.ai/v2/docs/console/go/#usage-limits), keine ausgelesene Abo-Abbuchung. Reasoning ist bereits Teil der Ausgabe. Ein erster isolierter Start mit unvollständigem Testpaket scheiterte vor jedem Modellaufruf und zählt nicht als bestandener Nachweis.

Synthetische Tests sichern zusätzlich parallelen Hermes-Zugriff, Doppelzustellung, Backend-Wakeup, Neustart, unbekannte Sendestände, Änderungen teilweise angewandter Quellen, Widerruf vor Versand und vollständigen Stop-/TP-Schutz ab. Sie verwenden ausschließlich Fake-Börsen und temporäre Signierer.

## Konfiguration und Wiederholung

```yaml
express:
  mode: "off"  # off, shadow, live
  master_path: hermes-home/skills/aqla-master/SKILL.md
```

Neue Installationen bleiben auf `off`. `shadow` prüft Vorlagen ohne exklusive Übernahme, Planung oder Versand; Hermes arbeitet weiter. `live` aktiviert den nachgewiesenen Pfad. Ein geänderter Master-Hash sperrt neue Express-Einstiege. `source_processing.mode` bleibt `legacy`; Decision Model, Max und OpenRouter werden nicht aktiviert. Status ergänzt Express-Modus, Master-Freigabe und Auftragsstände.

`scripts/evaluate_express.py --dataset PRIVATE.json --output PRIVATE_RESULTS` prüft einen versiegelten Datensatz ohne Modell- oder Börsenzugriff. Der native Vergleich nutzt `scripts/evaluate_high_tools.py --caveman skills/caveman/SKILL.md` mit privatem Master und Datensatz. Anschließend verwendet `evaluate_express.py --baseline PRIVATE_NATIVE_RESULTS` die abgeschlossenen nativen Ergebnisse für gepaarte Fake-Handelsläufe. Originale, Referenzen, Zugangsdaten und Einzelantworten bleiben privat.

Nach grüner CI zunächst `shadow`, danach nur bei bestandenem Nachweis `live` ausrollen; je Stufe mindestens zwei vollständige Sync-Zyklen kontrollieren. Rücknahme deaktiviert Express und stellt nötigenfalls vorige Software/Konfiguration wieder her. Finanzdatenbanken werden niemals zurückgesetzt. Der Nachweis gilt für die geprüften Vorlagen und Fälle.
