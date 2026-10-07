# Gold, Dezimalhebel, Kanalnamen und Backup

## Gold und Hebel

Hyperliquid löst `GOLD`, `XAU`, `XAUUSD`, `XAUUSDT` und `XAUUSDC` ausdrücklich als `xyz:GOLD` auf. Der Adapter prüft diesen HIP-3-Markt im konfigurierten Netzwerk, einschließlich DEX-Asset-ID, USDC-Kollateral, Präzision und Hebelmaximum. Nichtverfügbarkeit, Delisting oder unzureichende DEX-Margin bleiben Ablehnungsgründe. Es gibt keine automatische Wahl eines anderen Anbieters, keine PAXG-Substitution und keine Geldübertragung.

Signalhebel dürfen Dezimalwerte enthalten. Das belegte Original bleibt erhalten; Python rundet ab und deckelt anschließend am Börsenmaximum. `12,78×` ergibt `12×`, bei einem Marktmaximum von 10 ergibt sich `10×`. Werte unter eins, negative, nicht endliche oder mehrdeutige Werte liefern keinen Backup-Ersatz. Ganzzahliges JSON bleibt für bestehende Signale kompatibel.

## Kanalnamen

„Nenne Kanal -1001234567890 VIP Gold“ setzt einen eindeutigen Anzeigenamen. Über `control` entspricht das `set_source_name` mit `source_id`, `source_name` und `expected_revision=source_names_revision` aus `status`. `source_name=null` entfernt ihn.

Namen sind ohne Beachtung der Großschreibung eindeutig und höchstens 100 Zeichen lang. Sie dürfen nicht mit IDs, Usernames oder reservierten Selektoren verwechselt werden. „Verbinde VIP Gold mit Profil A“ ist möglich; gespeichert wird weiterhin die aufgelöste Telegram-ID. Benennen abonniert keinen Kanal und verändert weder Cursor noch Handelsregeln. Nach einer Abmeldung bleibt der Name für die Historie erhalten. Die Leser-Zuordnung `source_aliases` und die Eigentümer-Zuordnung `source_names` bleiben getrennt.

## Ein globales Backup-Profil

Die Migration ergänzt das normale Profil `backup` mit Namen „Backup“, festem Hebel 2 und Markt-Entry-Fallback. Andere Werte werden einmalig aus Standard kopiert. Nur fehlende Signalangaben werden ergänzt; Risiko, TP-Mengenverteilung, Margin und gemeinsame Börsenlimits bleiben beim gewählten Hauptprofil. Der feste Hauptprofilhebel hat Vorrang; im Signalmodus gilt der vorhandene Signalhebel, sonst der Backup-Hebel. Das Backup darf nicht gelöscht oder auf Signalhebel umgestellt werden.

`fallback_stop_loss_percent` und `fallback_take_profit_percentages` sind zunächst leer. Ohne konfigurierten Ersatz bleibt ein Signal mit fehlendem SL oder vollständig fehlender TP-Liste abgelehnt. Sie werden wie andere Profilparameter über authentifiziertes `set_profile` gespeichert. Beispiel: „Backup: Hebel 2, SL 1 Prozent und TPs bei 1, 2 und 3 Prozent.“ Diese Zahlen sind ein Bedienbeispiel; sie werden nicht automatisch aktiviert.

Prozentwerte liegen strikt zwischen 0 und 100. Die TP-Liste enthält bis zu 20 eindeutige, aufsteigende Werte. Markt und Richtung bleiben erforderlich. Vorhandene Signalpreise werden verwendet; fehlerhafte Preise werden abgelehnt. Fehlende einzelne spätere Ziele einer vorhandenen Liste werden nicht ergänzt. Updates verwenden die gespeicherte Tradepolicy und erhalten keine neue Backup-Ergänzung.

## Prozentpreise und Wiederanlauf

Prozente messen Kursbewegung ab dem tatsächlichen Entry-VWAP, unabhängig vom Hebel. Bei Entry 100 und SL 1 %, TP 1/2 % entstehen Long-SL 99 und TPs 101/102; Short-SL 101 und TPs 99/98. Die TP-Aufteilung und Stop-Leiter des Hauptprofils gelten weiterhin.

Die Vorschau prüft vorläufige Preise inklusive Slippage, Tick-Rundung, Mindestmengen und Risikobudget. Der Entry erhält bereits einen vorläufigen SL. Nach bewiesenen Fills und Beendigung einer ungefüllten Restorder wird der Referenz-Entry einmalig festgeschrieben. SL wird vom Entry weg, TP in Gewinnrichtung gerundet. Referenz, Prozentwerte und endgültige Preise liegen dauerhaft im Zustand, bevor Ersatz-SL oder TP-Absichten versendet werden.

Der bisherige SL bleibt bis zum bestätigten Ersatz bestehen. Fehlerhafte endgültige Preise, überschrittene Budgets oder abgelehnte Schutzorders lösen einen Reduce-only-Schließungsversuch und eine Entry-Sperre der betroffenen Börse aus. Ungeklärte Operationen werden über ihre gespeicherten Identitäten abgeglichen. Ein Neustart oder eine Profiländerung berechnet endgültige Preise nicht erneut. Noch ungesendete Pläne mit verwendeten Backup-Werten werden bei Änderungen am Backup ungültig.

Sheets ergänzt Quellenname sowie Originalsymbol, Original-/abgerundeten/wirksamen Hebel und Backup-Herkunft/Referenz. Bestehende ID-Spalten, Zeilen-IDs und Audit-Historie bleiben erhalten. Eine reine Namensänderung macht keine Tradepläne ungültig.
