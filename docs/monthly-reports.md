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
Inhaltslänge; der alte Tail wird gelöscht. Der bekannte vormals eigene Prüfumfang
bleibt im Manifest auch nach einer schrumpfenden bestätigten Generation erhalten.
Jede spätere Quittierung prüft diesen gesamten Tail auf leere Zellen; verlorene
Commit- oder Readbackantworten dürfen eine dort festgestellte Änderung nicht
durch eine verkürzte Prüfung vergessen. Die tatsächliche Inhaltslänge begrenzt
weiterhin neue Valueswrites. Große Rasterkapazität wird nicht als
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

## Begrenzter und fairer Belegabgleich

Der interne Reporting-Tick verarbeitet fällige geschlossene Trades seriell außerhalb der Ausführungssperre. Standardmäßig gelten höchstens acht Jobs, 32 tatsächliche HTTPX-Leseanfragen und zehn Sekunden je Tick; ein Job erhält höchstens acht Anfragen und acht Sekunden beziehungsweise die kleinere verbleibende Tickzeit. Bereits unveränderte Accounting-Fakten erzeugen keinen zusätzlichen Zustandswrite. Konstruktorparameter `max_jobs`, `max_requests`, `time_budget_seconds`, `job_requests` und `job_time_seconds` sind validiert. Jobgrenzen dürfen Tickgrenzen nicht überschreiten; Zeiten müssen positiv, endlich und höchstens 40 Sekunden sein. Bestehende History-/Cursorcaps jeder Börse gelten zusätzlich.

Nach einer vollständigen Prüfung bleibt das Intervall eine Stunde. Vorübergehende Lese-/Zeit-/Budgetfehler werden nach 60, 120, 240 und höchstens 300 Sekunden erneut geprüft; fehlende Lifecycle-Belege und positive Belegkonflikte bleiben bei der stündlichen Prüfung. `reporting_failure_count`, `reporting_next_retry_at` und der begrenzte strukturierte `reporting_last_attempt` werden am Trade gespeichert und überstehen Neustarts. Ein abgebrochener reservierter Versuch ist frühestens nach 60 Sekunden wieder fällig. Ältere unvollständige Datensätze ohne Retry-Marker werden nach einer Minute erneut geprüft; gespeicherte vollständige Abdeckung bleibt bei einem bloßen Leseausfall erhalten. Die Prüfung verändert keine Orders und ersetzt fehlende Funding-Belege niemals durch eine geschätzte Null.

Das Requestbudget zählt tatsächliche ausgehende HTTPX-Anfragen für Order-, Fill- und Fundinghistorien einschließlich Pagination, Redirects, Authfolgeanfragen und fehlgeschlagener Transportversuche. Es zählt keine Jobs oder bloßen Funktionsaufrufe. Requests nach Ausschöpfen des Budgets werden vor dem nächsten Transport verhindert. Budgetmarker bleiben lokal; Schreibpfade erhalten sie nicht und der originale Auth-/Signer-/Noncevertrag wird nicht geändert.

Vor jedem Versuch speichert der vorhandene generische Statewriter `reporting_attempt_sequence` und `reporting_attempted_at` am Trade. Noch nicht versuchte Jobs haben Vorrang; auch ein Abbruch und Neustart in derselben Millisekunde behält die Reihenfolge. Ein langsamer oder dauerhaft fehlerhafter Job beendet nicht die gesamte Runde, solange Tickbudget übrig bleibt. Cancellation und Shutdown werden weitergereicht. Scheitert die dauerhafte Reservierung, beginnt kein Reportingread.

Zeit-/Requestende meldet den aktuellen Abgleich unvollständig mit sicherem Grund. Gespeicherte eigene Fills, Gebühren und Fundingbelege bleiben erhalten; bereits verifizierte Coverage bleibt gültig, solange die zugehörigen Fingerprints weiterhin passen. Es werden keine fehlenden Gebühren oder Fundingwerte geschätzt. Die typisierten internen Tick-/Refreshresultate beschreiben den Belegabgleich; berechenbare Finanzwerte bleiben von den vollständigen Monatsreportprüfungen abhängig. Umfangreiche Historien können größere konfigurierte Jobbudgets benötigen und bleiben bis zu einem vollständigen belegbaren Abgleich unvollständig.

Fakeclock-/MockTransporttests belegen getrennt Zeit, tatsächliche Requests und Fairness: zwei einfache Jobs benötigen vier Transportanfragen in einer Runde; die eingefrorene alte Einjob-Basis verarbeitet im gleichen kontrollierten Zweijobfall nur einen Job. Slow-/Fast-/Errorfälle, verbleibende Zeitslices, SQLite-Neustart, Persistenzfehler, Cancellation und unveränderte Finanz-/Coverage-/Fingerprint-/Report-/JSON-Typeoracles werden geprüft. Diese gezählte Arbeit ist kein VPS-Benchmark und keine Zusage zur Live-Laufzeit oder Backlogdauer.
