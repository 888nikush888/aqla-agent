# Verzögerte Einstiege und Trade-Diagnose

Ein ausdrücklich verzögerter Einstieg (`entry_timing=deferred`) benötigt eine
belegte Entry-Zahl oder Range und einen Kontextbeleg für die Verzögerung. Hermes
interpretiert die Absicht in natürlicher Sprache, beispielsweise „erst bei Entry“
oder „вход отложенный, не сейчас“. Eine genannte `source_exchange` benötigt ebenfalls
einen Beleg. Die Pipeline bestimmt weiterhin die Ausführungsbörse.

Aqla speichert einen lokalen `waiting_entry` mit Ausgangskurs und Annäherungsrichtung.
Es gibt vor der Auslösung keine Börsen-Entryorder und keine Hebeländerung.
Der vorhandene Backend-Lauf prüft alle zehn Sekunden; langsame oder fehlende
Preisdaten können die Beobachtung verzögern. Der Wartezustand reserviert Markt,
Trade-Platz und Risiko. Pausen halten ihn an, Ablauf und relevante Quellen-/Profil-
änderungen beenden ihn. Schließen/Entry-Streichen beendet die lokale Reservierung.
Erlaubte SL-/TP-Updates verändern vorgesehene Werte; Einstand/Reduktion benötigen Fills.

Der zukünftige SL invalidiert den noch ungefüllten verzögerten Einstieg nicht.
Beim beobachteten Erreichen des Entry wird neu geprüft. Überspringt der Kurs die
bestehende Slippage-Toleranz, folgt kein nachträglicher Einstieg. Ein Neustart
verlängert die Signalfrist nicht. Nach erfolgreicher Prüfung verwendet Aqla IOC
innerhalb der bestehenden Slippage-Toleranz, keine unbegrenzte Marktorder.
Ein definitiv ungefüllter IOC ist ein beendeter Versuch; ungeklärte Antworten
werden anhand derselben Operations-/Orderidentitäten abgeglichen, nie blind wiederholt.

Ein Preis-Snapshot enthält Bid/Ask, Mid, Triggerreferenz, Börsenzeit und Lesezeiten.
Alle Belege müssen frisch sein (maximal zwei Sekunden). Hyperliquid nutzt den
Mark-Preis für SL, Aster den im bestehenden Adapter gewählten `CONTRACT_PRICE`.
Lighter nutzt ausdrücklich zeitgestempelte REST-Daten oder die öffentlichen
`market_stats`-/`order_book`-WebSocket-Snapshots mit Mark-Preis; fehlende Referenzen
werden nicht durch einen Last-Trade-/Mid-Schätzwert ersetzt.

Vor dem Signieren und nach dem dauerhaften Vorbereiten, aber vor Versand, werden
Preisstruktur, Konto, Margin, Marktbelegung und Budgets erneut geprüft. Ein belegter
Abbruch vor Versand bleibt als `not_sent` gespeichert und verbrauchte Nonces werden
nicht wiederverwendet. Nach neuen Entry-Fills werden VWAP, Positionswert und SL-Risiko
gegen tatsächliche Belege geprüft. Schutzverletzungen führen zum bestehenden
Reduce-only-Schließungsversuch und zur Entry-Sperre nur der betroffenen Börse.
Ein vollständig belegter SL-Exit erzeugt keine zusätzliche Schließungsorder.

Neue Trades speichern immutable Ereignisse für den ersten Entry-Fill und den
vollständigen Abschluss. Die Telegram-Ansicht zeigt „Einstieg ausgeführt“ und
„Trade geschlossen“ getrennt und chronologisch, auch bei einem Exit innerhalb
derselben Sekunde. „Schutz bestätigt“ benötigt die Prüfung der tatsächlichen
Reduce-only-Triggerorder; ein bloßes Order-Ack reicht nicht. Schutzvorfälle bleiben
priorisiert. Der native Hermes-Adapter besitzt weiterhin Versand und Wiederholung.

`trade_diagnostics(trade_id=...)` liest auch geschlossene Trades, Originalsignal,
gesendete Orderart, Fills, Preisprüfungen und belegten Schließungsgrund.
`symbol=...` liefert bei mehreren Treffern Kandidaten zur eindeutigen Auswahl.
Historisch fehlende Preisbelege werden nicht rekonstruiert oder erfunden.

Der LINK-Vorfall vom 07.10.2026 betraf einen verzögerten Bybit-Short-Entry bei
13,052, der als gewöhnliches GTC-Sell-Limit auf Hyperliquid sofort bei 13,368
gefüllt wurde. Der SL bei 13,328 schloss 0,681 Sekunden später. Eine Sell-Limitorder
erlaubt einen höheren („besseren“) Fill und ersetzt daher keine Verzögerungsabsicht.
Alte LINK-Signale werden weder erneut ausgeführt noch ungefragt nachgemeldet.
Bestehende IDs, Historie, Monatsberichte und Handelsparameter bleiben erhalten.
