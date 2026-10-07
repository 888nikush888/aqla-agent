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
