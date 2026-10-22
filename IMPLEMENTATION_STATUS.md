# aqla Agent — Auslieferungsstand

## Kanalranking, getrennte Konten und ROI-Ziele (08.10.2026)

- Schema 3 bewahrt historische Ausführungsidentitäten und gibt vorhandenen Quellen eine C-Startfreigabe; neue Quellen beginnen F. Netto-R je vollständigem Signalsetup, konservative Belegprüfung, langsamere Aufstiege, schnellere Abstiege und 5R-Mainnet-Sperre. Risikogewichtung betrifft neue Trades; Kontolimits bleiben separat.
- Getrennte Hyperliquid-Mainnet-/Testnet-Kontexte über einen SQLite-Schreibprozess; kein Börsenersatz oder Netzwerk-Ausweichpfad.
- Belegte Kurs-/ROI-Prozente verwenden den ursprünglichen Bezug und Dezimal-Signalhebel. VWAP-Finalisierung, Tick-/Mindestgrößenprüfung und unveränderliche Operationsidentitäten gelten weiterhin. Prozentupdates ändern den Quellenbeleg nicht.
- Telegram, Diagnose und Sheets zeigen Rang, Risiko, Herkunft und Börsenersatz. Ränge/Ranghistorie und getrennte Kontotabs erweitern die Analyse; Mainnet-Monatsberichte behalten tatsächliche Geldverluste unabhängig von Ranking-Ausschlüssen.
- Adapter-, Ranking-/Routing-, Migrations-, Quellenautoritäts- und native Hermes/Telegram-Tests ergänzen die bestehende Qualitätspipeline. Einrichtung und Abnahme ohne synthetische Mainnet-Orders. Siehe `docs/ranking-routing-roi.md`.

## Verzögerte Einstiege und getrennte Ereignisse (07.10.2026)

- Belegpflichtiges `entry_timing=deferred` und `source_exchange`; lokale, dauerhaft gespeicherte Entry-Reservierungen mit Annäherungsrichtung, Ablauf, Pause und Wiederanlauf. Ein zukünftiger SL wird erst beim tatsächlichen Einstieg gegen die Zielbörse geprüft.
- Frische native Hyperliquid-Bid-/Ask-/Mark-Belege und unveränderte Prüfungen vor Entry-Versand und nach Fills.
- Unveränderliche Entry-/Schließungsereignisse und chronologische Telegram-Karten, auch bei einem sofortigen SL-Exit. Schutzbestätigung prüft die tatsächliche Reduce-only-Triggerorder. Lesendes `trade_diagnostics` liefert Original, Wire-Orderart, Fills, Preisprüfungen und Schließungsgrund.
- Regressionen für den LINK-Vorfall, Long/Short, Kurslücken, Teilfills, Preisalter, Datenfehler, verlorene Antworten und unabhängige Pipelineziele. Alte IDs, Historie und abgeschlossene Signale werden nicht erneut ausgeführt oder ungefragt nachgemeldet.

## Monatsberichte mit ROE (07.10.2026)

- Lesendes `monthly_report` mit begrenzten Seiten, vollständigen Summen, Ausschluss unvollständiger Ergebnisse und separaten offenen/undatierten Trades. Aqla-Mainnet-Abschlüsse werden einmal im belegten Schließmonat ausgewertet.
- Festgeschriebener tatsächlicher Entry-Positionswert und berechnete Anfangsmargin nach bestätigter Entry-Endmenge. Native Margin-Hebelrundung und Teil-Entries/Exits werden berücksichtigt. SL-/TP-Preise ändern sich bei Hebeldeckelung nicht.
- Native Hyperliquid-Funding-Zahlungen mit stabilen Identitäten und begrenzter Pagination; historische eigene Abrechnungen bleiben erhalten.
- Idempotente Sheets-Monatstabellen, vollständige Rückleseprüfung vor Benachrichtigung, dauerhafte Revisions-/Versandmarken und Wiederanlauf. Startmonat Oktober 2026, erster Versand 01.11.2026 um 09:00 Europe/Berlin. Betreiberanfragen lösen intern die Veröffentlichung aus; Quellen erhalten keinen Publisher.
- Native Hermes-/Telegram-Prüfungen mit simuliertem Versand, Monatsgrenzen/Sommerzeit, Gebühren/Funding, Quellenautorität, Wiederanlauf und verlorenen Antworten. Keine synthetischen Börsenaufträge zur Abnahme.

Stand: 07.10.2026. Neues eigenständiges Python-Projekt und unabhängiges VPS-Quellpaket. Die bestehende Hermes-Installation des Nutzers wurde weder verändert noch angebunden. Kein TSX-Code oder alter Handelszustand wurde übernommen.

## Inhalt

- Hyperliquid unterstützt als gemeinsames Konto manuelle Positionen auf anderen Märkten. Kontoübersichten zählen Positionen, fremde Entry-Reservierungen und Aqla-Plätze; Unified Account verwendet gemeinsame USDC-Balances und Holds. Markt-Konflikte sind lokal, fremde Orders bleiben erhalten. Die HIP-3-Asset-ID-Berechnung entspricht nun dem offiziellen SDK. Mainnet-Zustand, Journal-Tabs und Cutover-Cursor bleiben vom Testnet getrennt. Siehe `docs/mainnet-shared-account.md`.

- Additive `status.telegram_view` mit reinem Python-Formatter für kompakte Telegram-Meldungen. Vorlagen unterscheiden Entry-Warten, Teilfill, Eröffnung, Update, Ablehnung, ungeklärten Ausgang, Schutzvorfall und Schließen. Setupfamilien bündeln Börsenergebnisse; Schutzvorfälle haben Vorrang. Native Hermes-Konvertierung, Zustellung und gespeicherte Antworten bleiben erhalten. Siehe `docs/telegram-messages.md`.

- Gold/XAU-Aliase verwenden den geprüften Hyperliquid-Namensraum `xyz:GOLD` im konfigurierten Netzwerk. Dezimalhebel werden mit Originalbeleg gespeichert, abgerundet und am Marktmaximum gedeckelt. Frei wählbare Kanalnamen dienen der Telegram-Bedienung und Anzeige; IDs und Leserauflösung bleiben erhalten.
- Das globale Profil `backup` ergänzt fehlende Signalwerte mit festem Hebel 2, Markt-Entry und optionalen Prozent-SL/TPs. Prozentfelder starten leer. Schutzpreise werden aus realem Entry-VWAP einmalig finalisiert und vor Versand dauerhaft gespeichert; Wiederanlauf und Änderungen verwenden die gespeicherten Werte. Risiko, Verteilung und Kontolimits bleiben beim Hauptprofil. Siehe `docs/gold-backup-and-names.md`.

- Ausschließlich Hyperliquid-Ausführung. Die Migration archiviert frühere Kontokonfiguration und erhält Finanzidentitäten; offene fremde Börsenarbeit wird nicht aufgegeben. Siehe `docs/multi-venue.md`.
- Neunte Journal-Seite `aqla Pipelines`; zusätzliche Ziel-/Währungszuordnung in Trades, Orders, Fills und Vorgängen sowie Profile/Kontolimits unter Handelsparameter. Bestehende Spalten und Zeilen werden erweitert, nicht gelöscht. Unterschiedliche Währungen und Netzwerke werden nicht zusammengefasst.

- Native Hermes-Telegram-Kommunikation; eigene schmale Erweiterung für Quellenaufnahme, Wakeups, Herkunft und Skill-Neuladen. Kein eigener Bottransport oder Agentenhost.
- Read-only Telethon-Kanalleser mit StringSession, Kontext, Replies, Edits, periodischem Backfill und serieller Aufnahme vor Cursorfortschritt. Native Owner-Weiterleitungen werden ebenfalls gespeichert.
- Kurzer durch Hermes bearbeitbarer Master; Python prüft Nutzerregeln, Zahlen, Mengen, Marktpräzision, Budgets und Operationsidentitäten.
- Hermes darf den Master verbessern und erweitern, aber jede Änderung wird zuerst im nativen Freigabespeicher als Entwurf abgelegt. Die authentifizierte Telegram-Zustimmung ist verpflichtend; andere Skills, mehrere Operationen, zusätzliche Dateien, übergroße und veraltete Entwürfe sind gesperrt. Hintergrundentwürfe erzeugen eine Frage mit Ansehen-/Zustimmen-/Ablehnen-Befehlen; der aktive Skill bleibt bis zur Freigabe unverändert.
- Zweiter eigener Google-Sheets-MCP mit automatischem Journal, Queue im vorhandenen State, stabilen Zeilen-IDs, Statushistorie und lesenden Auswertungen. Quellenleser und Wakeups laufen unabhängig vom Google-Task. Die Konfiguration benötigt auf dem VPS eine eigene Tabelle und einen Servicekonto-Zugang; technische Order-/Nonce-Sicherung bleibt lokal.
- Automatischer Perpetual-Signalhandel mit Entry/SL, anteiligen Limit-TPs, Updates, Einstand, Reduktion, Schließen und Wiederanlauf. Spot liegt im vollständigen MCP; die automatische Signalstrecke lehnt Spot vor einer Order ab.
- Hebelmodus `fixed` oder `signal`: Signalhebel werden belegt, am Marktmaximum gedeckelt und als effektiver Hebel unveränderlich im Plan gespeichert. Marginprüfung, Börsenauftrag und Recovery verwenden denselben Wert; alte feste Regeln und Pläne bleiben lesbar.
- Eigener vollständiger HyperCore-MCP: 59 Info-Verträge, 40 dokumentierte Exchange-Aktionstypen, 52 öffentliche SDK-Operationen einschließlich der gesonderten Approval-/Multisig-Pfade, 24 WebSocket-Abonnements, HIP-3/HIP-4 und Deployeraktionen. Siehe docs/api-coverage.md. API-Snapshot: 02.10.2026; keine Behauptung unbekannter zukünftiger API-Erweiterungen.
- Transaktionaler SQLite-Handelszustand mit genau einem Schreibprozess; native Hermes-Sitzungsdatenbank mit Volltextsuche. Alte JSON-/JSONL-Daten werden unter Erhalt der Originale importiert. Fehlende oder beschädigte initialisierte Handelsdatenbanken blockieren die Ausführung. Native Rückfragen, Aufgabenplanung und Sitzungssuche sind für authentifizierte Betreiber aktiviert; ausbleibende Antworten erlauben keine Änderungen. Kanaltext erhält keine Betreiberwerkzeuge. Ungenutzte Hintergrundprodukte bleiben deaktiviert.
- Installer mit eigenem Hermes-Checkout/Home und systemd-Unit; Aqla Python 3.12, Hermes Python 3.14, beide im Zielordner verwaltet. Gepinnter Hermes-Commit: 1a4508e2aff2db5f50409893a2115be777bd5643.
- Neuinstallationen verwenden standardmäßig DeepSeek V4.1 Flash über OpenCode Go und Hermes-Reasoning `ultra` (DeepSeek-API `max`). Modell und Endpunkt bleiben über Installerargumente überschreibbar; die CLI-Vorgaben sind als tatsächliche neue Konfiguration getestet.
- Source-ZIP mit SHA-256, Wheel und Source-Distribution; keine lokalen Secrets, Daten oder QA-Umgebungen im Archiv. Eigenständiges privates GitHub-Repository `888nikush888/aqla-agent` mit Qualitätspipeline.

## Geprüft

Mainnet/Parallelhandel: initial **690 Tests bestanden, ein plattformabhängiger Test übersprungen, 77,98 Prozent Coverage**. Zusätzliche Journal-Namespace- und Inventarprüfungen bestanden. Die echte Mainnet-API-Wallet wurde lesend bestätigt; Unified Account, manuelle Positionen und freie USDC-Margin wurden über die vollständige Kontoübersicht geprüft. Kein künstlicher Mainnet-Auftrag wurde gesendet.

Telegram-Ansichten: **677 Tests bestanden, ein plattformabhängiger Test übersprungen, 80,16 Prozent Coverage** im initialen Gesamtlauf. Weitere gezielte Fälle prüfen ungeklärte Ziele und unvollständige Exit-Fills. Der native Telegram-Test bestätigt MarkdownV2, Sonderzeichen, Nachrichtenteilung und dieselben gespeicherten Antworttexte ohne reale Telegram- oder Börsenaufrufe.

Gold/Backup/Kanalnamen: **657 Tests bestanden, ein plattformabhängiger Test übersprungen, 79,60 Prozent Coverage** im lokalen Gesamtlauf. Weitere 82 gezielte Fälle nach zusätzlicher Absicherung des endgültigen Fill-Referenzwerts bestanden. Native Hermes-Smokes prüfen Betreiberänderungen an Backup und Kanalnamen sowie die Abwehr derselben Aufträge aus Quellen. Alle drei Adapter-Verträge prüfen die Prozent-Schutzstrecke; verlorene Antworten, Teilfills, Neustart, unveränderte Referenzwerte und Schließen bei TP-Ablehnung sind abgesichert.

SQLite-/Werkzeug-Erweiterung: lokal **612 Tests bestanden, ein plattformabhängiger Test übersprungen, 79,10 Prozent Coverage**. Echte native Hermes-Prüfungen bestätigen importierte Volltextsuche, transaktionalen Import mit Fehler-Rollback, persistierte Aufgabenlisten, Telegram-Rückfragekarten und Quellen-Abwehr. Die Handelszustandsprüfungen decken JSON-Migration, unveränderte Sicherung, exklusive Schreibsperre über beide Pfadformen, abgebrochene SQLite-Transaktion und fehlende/beschädigte Datenbanken ab. Die SQLite-Views erlauben lesende Kontrolle ohne zweiten Writer.

Alle 32 bestätigten Befunde der Voll-, Nach- und Abschlussprüfung sind korrigiert und durch gezielte Regressionstests beziehungsweise Paket-/CI-Prüfungen abgesichert. Die Zuordnung steht in `docs/quality-review.md`. Die Abnahme nach der VPS-Startkorrektur, vor der Handelsparameter-Erweiterung, bestand lokal 498 Tests einschließlich der lokalen WebSocket-Prüfungen, mit 80,74 Prozent Branch-Coverage. Die Parameterseite ist zusätzlich durch drei Regressionstests für vollständige Feldabdeckung, unverbindliche Standardwerte, authentifizierte Regeländerungen, veraltete Bestätigungen und Erhalt vorhandener Tabellen abgesichert. Native Hermes-Smokes prüfen zusätzlich echte Text-/Foto-/Queue-Batchpfade, rekursive Folgeturns mit wiederverwendetem Agenten, Workerautorität, Befehlsabbruch, Adapterwechsel, deaktivierte Cron-/Housekeeping-Factory und beide MCPs. Beide Abhängigkeitsgraphen melden zum Prüfzeitpunkt keine bekannten Advisories.

Initialer lokaler Gesamtlauf nach der Signalhebel-Erweiterung: **528 Tests bestanden, 81,51 Prozent Branch-Coverage**. Die Erweiterung prüft Signalhebel, symbolabhängige Deckelung, fehlende/ungültige Werte, Originalzitate, unveränderte Risikogrenzen, tatsächliche `updateLeverage`-Payloads, frische Marginprüfung, gesunkenes Marktmaximum, idempotenten Wiederanlauf und alte feste Pläne/Interpretationen. Nach zusätzlicher Absicherung von Satzpunkten in Hebel-/Preisbelegen sind 48 gezielte Tests bestanden. Ein tatsächlicher nativer DeepSeek-V4.1-Flash-Aufruf erfasste den synthetischen 50×-Signalhebel mit gültigem Originalbeleg; Python deckelte den Plan auf 20×. Dieser Modelltest prüft den neuen Hebelpfad, nicht die gesamte Erkennungsqualität aller Preisfelder.

Ruff; eingefrorene Installation; Paketbuild; separate Wheel-Installation und Offline-Installer; regel- und zustandsbezogene Tests einschließlich Crash vor TP-Versand, verlorenen Antworten, echten Teilfills, beendeter eigener Position mit fremdem Wiedereröffnen, frischer Equity bei Recovery und verlorener niedriger Telegram-ID.

Mit dem tatsächlichen gepinnten Hermes: SQLite-Import und Volltextsuche, Aufgabenwiederherstellung, Telegram-Rückfragekarten, Timeout ohne Änderungsfreigabe und Unterbrechung einer wartenden Rückfrage für Handelsarbeit; außerdem native AIAgent-Hooks und stdio-MCP. Ein lokales geskriptetes Modell prüfte Owner-Regeländerung, Master-Patch, Größenbegrenzung und die Abwehr von Kontrollaufrufen aus einer echten nativen Weiterleitung. Native Boot-Anbindung startet ohne neue Owner-Nachricht; geänderte Skills leeren den nativen Agentencache vor dem nächsten Turn.

Ein echter lokaler WebSocket-Server prüfte Reconnect, Unsubscribe und einen angenommenen Auftrag mit verlorener Antwort: kein erneuter Versand.

## Prüfgrenzen und Einrichtung

Die anschließende VPS-Installation auf ausdrücklichen Nutzerauftrag ist in `docs/vps-deployment.md` dokumentiert: Dienst läuft, echter Modellaufruf funktioniert, Telegram-Bot antwortet und der Kanal-Leser ist per QR angemeldet. Hyperliquid-Testnet-Kontobindung wurde lesend geprüft; keine Exchange-Mutationen. Google Sheets ist aktiv; acht Journal-Tabs, verifizierte Regelzeile, 26 aktuelle Parameterzeilen und leere Zustellungsqueue wurden tatsächlich geprüft. Handelsregeln und Quellen wählt der Nutzer über den Bot.

Geskriptete Modellprüfungen beweisen Integration und Befugnisse, keine allgemeine sprachliche Erkennungsqualität. Für das gewünschte echte Modell liegen 30 Kontextfälle und ein ausdrücklich auszuführender Eval-Runner ohne Handels-/Telegram-Werkzeuge bei. Text und Captions werden verarbeitet; reine Bildsignale nicht. Native Stop-Ausführung ist kein garantierter maximaler tatsächlicher Verlust.
