# Telegram-Steuerzentrale

`/aqla` öffnet das private Betreiberpanel. Untermenüs bearbeiten dieselbe Nachricht. Zurück, Hauptmenü, Aktualisieren und Seitenwahl bleiben erreichbar. Der Bot verwendet native Inline-Buttons; ein zusätzlicher Webdienst ist nicht nötig.

## Bereiche

| Bereich | Funktionen |
| --- | --- |
| Dashboard | Betriebszustand, belegter Netto-PnL, Gebühren/Funding, eigene Positionen, Abgleich und Warteschlange |
| Positionen | Eigene Mengen, Entries, SL/TP, Waverider und gezielte Verwaltung |
| Journal & Berichte | Historische Trades, Orders, Fills, Vorgänge, Filter und Monatsberichte |
| Profile | Risiko/Hebel, Entries/Ausführung, Stop/TP, Backup und Waverider |
| Quellen & Routing | Quellen, Anzeigenamen, Ränge, Rangregeln und Profilzuordnungen |
| Konten & Freigaben | Mainnet/Testnet, Limits, Aktivierung, Pause und Eigentumsprüfung |
| System & KI | Modell/Reasoning, Caveman, Express, Quellenverarbeitung, Replay und Meldungsregeln |
| Hermes & Hilfe | Setup und bestehende zugelassene Hermes-Befehle |

Die bisherigen `/aqla_…`-Befehle bleiben Kurzwege. Das sichtbare Bot-Befehlsmenü konzentriert sich auf `/aqla`, `/aqla_pause` und `/setup`.

## Änderungen

Ein gültiger letzter Wert wird sofort gespeichert. Zahlen akzeptieren Dezimalkomma; Listen verwenden `;` zwischen Werten. Jede Änderung validiert das vollständige Profil und die aktuelle Revision. Abhängige Werte müssen in gültiger Reihenfolge gesetzt werden, etwa erst der feste Positionswert, dann die Auswahl der festen Größenberechnung.

Eine neue Installation zeigt das noch leere Standardprofil zuerst mit allen Ausgangswerten. Erst die ausdrückliche Übernahme richtet es ein. Erste Kontolimits verlangen beide Werte: maximale offene Trades und Gesamtrisiko; danach wird jeder gültige einzelne Wert sofort gespeichert. Es werden dabei keine Kontolimits automatisch ergänzt.

Die private Setup-Freigabe dauert 300 Sekunden. Das Passwort wird ausschließlich im ursprünglichen nativen Passwortdialog verarbeitet und nach bestätigter Löschung geprüft. Lesen, Pause und Risikoreduktion bleiben nach ihren bestehenden Regeln erreichbar. Alte Buttons, fremde Nutzer, Gruppen und weitergeleitete Eingaben verleihen keine Autorität.

Positionsaufträge binden exakt einen Trade, die angezeigten Belege und eine dauerhafte Auftragsidentität. Doppelklick und Wiederanlauf erzeugen keine neue Handelsabsicht. Es gibt keinen Menüdialog zum Eröffnen neuer manueller Positionen. Bereits beendete Waverider werden nicht wieder aufgenommen.

## Meldungen

Automatisch: tatsächlicher Einstieg, reguläre TP-/Teilabschlüsse, vollständig belegte Waverider-Wellen, Schließung und relevante Störungen. Teilfills derselben Ausführung werden bis zu 15 Sekunden gebündelt. Schutzprobleme und ungeklärte Orderausgänge haben Vorrang.

Reine Lesestörungen werden erst nach 120 Sekunden gemeldet; kurze Ausfälle und ihre Wiederherstellung bleiben im Verlauf. Eine Wiederherstellung wird nur bei vorher gemeldeter Störung angekündigt. Unveränderte Probleme erzeugen keine Nachrichtenschleife. Unterdrückte Fakten bleiben als solche gespeichert, ohne behauptete Zustellung.

Routine-Statusrunden, Fortschrittsblasen und geplante Neustartmeldungen entfallen. Monatsberichte bleiben automatisch; Tageszahlen stehen auf Abruf im Dashboard. Journal und Berichte sind Telegram-Ansichten, kein zusätzlicher Dateiexport.
