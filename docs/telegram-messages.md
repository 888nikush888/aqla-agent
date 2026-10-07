# Telegram-Darstellung

`status.telegram_view` ergänzt die bestehenden Statusdaten um einen reinen Python-Formatter. `status` enthält die Übersicht als Liste von Textteilen; `messages` enthält nach Setup gruppierte Meldungen mit `texts`, `notification_ids` und `severity`. `remaining_notification_ids` benennt noch nicht dargestellte IDs. Pro Aufruf werden höchstens 100 Notifications verarbeitet; Schutzvorfälle haben Vorrang.

Hermes übernimmt die vorbereiteten Texte für Handelsmeldungen und bestätigt die dargestellten IDs über das bestehende `notifications_ack`. Darstellung und Aufteilung verändern keine Orders, Trades oder Handelsregeln. Neue Notifications enthalten frühere Werte für SL-/Positionsänderungen und eigene TP-Fills. Bestätigte wartende Entries erhalten ebenfalls eine Meldung. Alte Notifications bleiben lesbar; fehlende frühere Werte werden nicht erfunden.

Eine gemeinsame Signalübersicht enthält getrennte Börsenergebnisse. Eröffnung, wartender Entry, Teilfill, Schutzabgleich, Update, Ablehnung, Schließung und Schutzvorfall besitzen verschiedene Überschriften. Quelle, Markt, Richtung, Netzwerk und Profil bleiben erkennbar. Preise und Prozentwerte verwenden deutsches Zahlenformat; TP-Prozente bezeichnen Positionsanteile. Hebeländerungen und Backup-Herkunft erscheinen bei Bedarf.

PnL wird nur mit vollständigen eigenen Entry-/Exit-Fills und bekannten Gebühren angezeigt, getrennt nach Abrechnungswährung und ausdrücklich vor Funding. Unbekannte Ausgänge bleiben ungeklärt. Eine generische Preisordnungs-Ablehnung wird nicht als Beweis einer bestimmten aktuellen Kurslage ausgegeben.

Dynamische Labels werden als Daten behandelt, nicht als Markdown-Anweisungen oder Links. Lange Inhalte werden an vollständigen Blöcken beziehungsweise Zeilen aufgeteilt. Native Hermes übernimmt MarkdownV2-Konvertierung, Zustellung, Wiederholungen und die endgültige Telegram-Aufteilung. Kein zusätzlicher Bottransport wird eingeführt.

Der Master verwendet dieselben Gestaltungsregeln für kurze Rückfragen und Konfigurationsbestätigungen. Ein leerer automatischer Durchlauf ohne Statusänderung verwendet Hermes' nativen `NO_REPLY`-Marker. Direkte Betreiberfragen erhalten weiterhin Antworten. Native Tests simulieren Telegram-Versand und prüfen Formatierung, Aufteilung und unveränderte Speicherung in der Hermes-Sitzungsdatenbank ohne reale Nachrichten.
