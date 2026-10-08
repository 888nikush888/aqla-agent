# Monatsberichte

Hermes versteht beispielsweise „Zeige mir den Monatsbericht Oktober 2026“.
Das lesende Aqla-Werkzeug `monthly_report(month="2026-10")` liefert belegte
Trade-Ergebnisse und den Link zur veröffentlichten Tabelle. Bei einer authentifizierten
Betreiberanfrage beauftragt die native Erweiterung intern die Sheets-Veröffentlichung;
das Modell erhält weder den Integrationstoken noch Schreibzugriff auf Tradefakten.

Der vorhandene Hintergrundlauf veröffentlicht abgeschlossene Monate ab Oktober 2026
am ersten Tag des Folgemonats um 09:00 Uhr Europe/Berlin. Der erste Termin ist
**1. November 2026, 09:00 Uhr MEZ**. Ein ausgefallener Server holt Termine nach.
Die Tabellen heißen `aqla Mainnet Monatsbericht YYYY-MM`. Die bestehenden Mainnet-
und Testnet-Journale bleiben erhalten. Nach verifiziertem Schreiben aller Tabellenwerte
entsteht eine dauerhafte Meldung für den nativen Hermes-Telegram-Versand.
Eine quittierte geplante Meldung wird bei Neustart nicht erneut erzeugt.

Jeder Monatsauftrag speichert seinen Status (`pending`, `publishing`, `retry`,
`blocked` oder `published`), Versuchszahl, letzten Versuch, nächste Retryzeit und
ausschließlich eine sichere Fehlerkategorie. Temporäre Ausfälle werden nach
60, 120, 240 Sekunden bis höchstens einer Stunde erneut versucht. Schema-,
Budget-, Inhalts- oder Revisionskonflikte bleiben sichtbar blockiert. Der lesende
Monatsabruf zeigt diese Diagnose in `publication_job`; Fehlerpayloads und Tokens
werden dafür nicht gespeichert. `monthly_report(month="2026-10", diagnostics_only=True)`
liefert ausschließlich den Monat und diesen begrenzten Diagnoseblock, auch wenn
ein einzelner Berichtseintrag zu groß für den Datentransfer ist. Andere fällige Monate werden nach dem letzten
Versuch fair ausgewählt, auch wenn eine Jobantwort verloren ging und ihre
60-Sekunden-Lease abläuft. Neue Belege oder eine frische authentifizierte
Betreiberanfrage erlauben eine neue Prüfung. Failure und ACK müssen zu
Fingerprint, Revision, Requestgeneration und konkretem Versuch passen; verspätete
Antworten dürfen keinen neueren Auftrag blockieren oder quittieren.

Ein Bericht bleibt auf 10.000 tatsächliche Tabellenzeilen einschließlich Kopf-
und Abschnittszeilen begrenzt. Schon 10.000 Tradezeilen überschreiten dieses
Budget; sie erzeugen einen blockierten Auftrag mit Diagnose `budget` und keine
vollständig veröffentlichte Teilmenge. Die normale Journalübertragung und andere
Monate laufen weiter.

Große zulässige Berichte werden intern als jobgebundener Kopf mit exakten Counts
und Seiten der drei Abschnitte übertragen. Jede vollständige native Antwort bleibt
einschließlich JSON-/Textwrapper und UTF-8-Escaping unter 600.000 Bytes; eine Seite
enthält höchstens 100 Zeilen. Jede Folge prüft die aktuelle Fingerprint-, Revisions-,
Request- und Versuchbindung erneut. Die native Erweiterung setzt alle Abschnitte
mit lückenlosen Offsets zusammen und prüft den vollständigen unveränderten
Berichtsfingerprint, bevor sie den einen vollständigen Bericht an Sheets gibt.
Eine einzelne Zeile, die nicht in dieses Antwortbudget passt, blockiert den Job
sichtbar mit `budget`; es gibt keine leere Fortschrittsschleife oder still
veröffentlichte Teilberichte.

Das Sheets-Monatsmanifest bindet Revision, Beleg-Fingerprint, tatsächliche
Inhaltslänge und bestätigten Tabelleninhalt. Ältere Revisionen sowie andere
Fingerprints bei gleicher Revision werden vor dem Schreiben abgewiesen. Eine
identische bestätigte Generation wird erst nach Prüfung aller Inhaltszellen
quittiert und benötigt keine Valueswrites. Unbestätigte gleiche Generationen
können nach einer verlorenen Antwort repariert werden. Neue und schrumpfende
Berichte schreiben und prüfen nur die größere alte beziehungsweise neue
Inhaltslänge; der alte Tail wird gelöscht. Große Rasterkapazität wird nicht als
Inhaltslänge behandelt. Legacytabs ohne Manifest werden konservativ im gesamten
vorhandenen Raster bis zum bisherigen Verifikationsbudget geprüft beziehungsweise
überführt.

Sichtbare Änderungen bestätigter Inhaltszellen oder eine konkurrierende neue
Generation vor Commit beziehungsweise Readback verhindern eine Erfolgsquittierung.
Die Veröffentlichung verwendet weiterhin RAW, den Clientlock und die vorhandene
native Writerstrecke. Sie behauptet keine atomare CAS-Garantie zwischen mehreren
unabhängigen Googleprozessen. Ein erfolgreicher erster Write oder allein der
gespeicherte Fingerprint beweist keinen vollständig bestätigten Bericht.

Nur Aqla-Mainnet-Trades mit belegtem vollständigem Abschluss zählen zum Schließmonat.
Der Bericht enthält das gesamte Ergebnis einschließlich früherer Teil-Exits. Am
Monatsende offene Trades und Abschlüsse ohne belegbares Schließdatum stehen separat.
Eine manuelle Schließung ohne eigene Exit-Belege wird keiner Monatssumme zugeordnet.

`Anfangsmargin = ausgeführter Entry-Positionswert / wirksamer Margin-Hebel`

`ROE % = (Brutto-PnL − Gebühren + Funding) / Anfangsmargin × 100`

Entry-VWAP, Positionswert und Anfangsmargin werden nach bestätigtem Ende der Entry-
Restorder dauerhaft festgeschrieben. Lighters Margin-Rundung wird berücksichtigt.
Im Cross-/Unified-Konto ist das eine berechnete Vergleichsbasis und kein separat
abgegrenztes Guthaben. Alle Berechnungen verwenden Decimal.

Die **Summe der Trade-Renditen — keine Kontorendite** addiert vollständige ROEs
arithmetisch. Sie beschreibt weder Zinseszins noch die Rendite des Gesamtkontos.
Gewinnquote, Gewinne, Verluste und neutrale Ergebnisse beziehen sich auf vollständige
Datensätze. Netto-Geldbeträge werden pro Währung summiert; USDT wird nicht in USDC
umgerechnet. Fehlende Gebührenwährung, Exit-Fills oder Funding-Abdeckung lassen
Netto-PnL und ROE leer; die Anzahl ausgeschlossener Ergebnisse wird angezeigt.

Funding stammt aus `userFunding` (Hyperliquid), `/fapi/v3/income` mit
`FUNDING_FEE` (Aster) beziehungsweise `/api/v1/positionFunding` (Lighter).
Der Abgleich verwendet begrenzte Zeitfenster, sichere Pagination und unveränderliche
Zahlungsidentitäten. Vollständige eigene Fills müssen mit der Börsenhistorie
übereinstimmen. Zusätzliche manuelle Fills, widersprüchliche Positionsmengen,
gleichzeitige nicht eindeutig geordnete Fills/Funding oder erschöpfte Lesebudgets
verhindern eine vollständige Zuordnung. Funding-Sätze werden nie als Zahlung geschätzt.
Ein stündlicher, fair verteilter lesender Abgleich ergänzt verfügbare Belege;
Änderungen aktualisieren dieselbe Tabelle mit neuer Revision.

Die Veröffentlichung hat keine Orderfunktion. Profile, Kontolimits, Börsenfreigaben,
SL und TP bleiben unverändert. Ein gedeckelter Hebel ändert die Margin/ROE,
aber weder absolute Signalpreise noch Backup-Kursprozente.
