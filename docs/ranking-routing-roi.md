# Kanalranking, Konten und ROI-Ziele

Die Migration gibt bestehenden Quellen C als gekennzeichnete Startfreigabe. Neu abonnierte Quellen beginnen F. F/E/D handeln ausschließlich auf eingerichteten Testnet-Konten; C/B/A/S auf Mainnet. Fehlende Konten blockieren das betroffene Ziel. Alte Signale werden nicht erneut ausgeführt.

| Rang | Netzwerk | Profil-RPR-Faktor | Mainnet-Positionsdeckel | Score | Nachweise für Aufstieg |
|---|---|---:|---:|---:|---:|
| F | Testnet | 0,25 | – | 0 | Start |
| E | Testnet | 0,5 | – | 30 | 5 |
| D | Testnet | 0,75 | – | 45 | 10 |
| C | Mainnet | 1 | 1× | 60 | 15 |
| B | Mainnet | 1,25 | 1,25× | 70 | 30 |
| A | Mainnet | 1,5 | 1,5× | 80 | 50 |
| S | Mainnet | 2 | 2× | 90 | 75 |

Ein Faktor multipliziert RPR und einen gewählten festen Soll-Positionswert. Testnet behält den Profildeckel. Kontolimits je Börsenkonto/Netzwerk werden nicht erhöht; Margin, Börsenminima und manuelle Marktbelegung gelten weiterhin.

## Bewertung

Ein ursprüngliches Signal zählt einmal. Alle eröffneten Pipelineziele müssen vollständig ausgewertet und weitere Entry-Reservierungen beendet sein. Je Ziel gilt `Netto-R = Netto-PnL / anfängliches SL-Risiko`; Gebühren und belegtes Funding gehören zum Netto-PnL. Das tatsächliche anfängliche SL-Risiko wird nach endgültigen Entry-Fills als Menge × Abstand zwischen VWAP und anfänglichem SL ohne Kostenreserve eingefroren. Der Setup-Wert ist der gleichgewichtete Mittelwert seiner Ziele.

Systemfehler, manuelle Eingriffe und unvollständige Belege werden separat ausgewiesen. Sie liefern weder Null-Ergebnisse noch Nachweise. Ihre Geldverluste bleiben im Journal und im Mainnet-Monatsbericht. Der frühere LINK-Ausführungsfehler ist anhand seiner ungültigen ursprünglichen Entry-/SL-Ordnung ausgeschlossen.

Für die letzten 50 gültigen Setups:

```text
clip(x) = min(1, max(0, x))
G = clip(mean(Netto-R) / 0,5)
P = clip((Profit-Faktor - 1) / 1,5)
D = clip(1 - größter Rückgang der kumulierten R-Kurve / 5)
W = (Gewinne + 1) / (Setups + 2)
Score = 100 × (0,40G + 0,25P + 0,20D + 0,15W)
```

Ohne Verluste bei vorhandenen Gewinnen ist P=1, ohne Gewinne/Verluste P=0. Ohne Ergebnisse ist der Score leer. Aufstieg: nächste Nachweiszahl und Score mindestens Schwelle+5, dann fünf weitere qualifizierende Abschlüsse. Höchstens eine Stufe pro Abschluss; C frühestens nach 20 Setups. Abstieg: ab fünf ausgewerteten Setups zwei neue Abschlüsse unter Schwelle−5. Ab 5R Drawdown gehen Mainnet-Ränge unmittelbar auf D; E/F werden nicht hochgestuft. Solange die Grenze verletzt bleibt, kein Mainnet-Aufstieg. IDs, Zähler und Historie verhindern doppelte Nachweise bei Neustart oder nachträglichen Belegen.

## Bedienung und Konten

Hermes verwendet `ranking_status` und `status`. Beispiele: „Zeige den Rang und ausgeschlossene Ergebnisse von Sakura“, „Setze den Risikofaktor von B auf 1,2“, „Pipeline Gold: Quelle Eins auf Hyperliquid mit Profil Standard“. `control(set_ranking_policy)` braucht die vollständige aktuelle Policy und deren Revision. Quellen, Weiterleitungen und Tabellen können diese Regeln nicht ändern.

Der primäre Hyperliquid-Kontext bleibt durch `network`, `account`, `private_key_file` bestimmt. Ein weiterer Kontext steht unter `execution_accounts` mit Schlüssel `hyperliquid:network`. Mainnet und Testnet behalten getrennte Bindungen, Signieridentitäten, Nonces, Limits und Reservierungen. Zusätzliche Konten beginnen deaktiviert; ihre Aktivierung prüft den Signierer lesend.

Filled Trades behalten Profil/Rang/Netzwerk. Neue Rang- oder Risikorevisionen beenden ungefüllte Entries; mögliche Fills während einer Stornierung werden abgeglichen und geschützt. Bestehende Positionen werden nicht übertragen.

Jeder neue Preview bindet `ranking_policy_revision` und `ranking_enabled` an dieselbe vor Markt-/Kontoprüfungen eingefrorene Grundlage wie Pipeline, Profil und Risikogewichtung. Das gilt auch bei deaktiviertem Ranking. Ein Konfigurationswechsel während des Preview verwirft den Entwurf und verlangt einen frischen Preview; aktuelle Revisionen werden niemals nachträglich auf alte Risikowerte gestempelt. Ein wegen Konfigurationsdrift verworfenes Ziel ohne Trade oder Entry-/Leverageoperation darf bei einem neuen Preview seine aktuelle Route aus derselben frischen Grundlage beziehen. Quellen-, Zeit- und Kontoguards gelten weiterhin. Geplante oder möglicherweise gesendete Ziele werden dabei nicht zurückgesetzt. Die Bindung gehört zur Plan-ID und wird vor Admission, Vorbereitung und Versand erneut geprüft. Aktivierung und Deaktivierung machen daher beide einen alten ungesendeten Plan ungültig. Der Quellenrang bleibt eine zusätzliche Prüfung.

Legacypläne ohne vollständige globale Rangbindung bleiben lesbar und behalten ihre IDs. Vor einem neuen Versand benötigen sie einen frischen Preview. Bereits vorbereitete, gesendete oder unbekannte Aktionen werden mit ihren ursprünglichen Operations-/Orderidentitäten abgeglichen; sie werden nicht als neue Aktion versendet. Bestehende Fills, Schutzorders und Exits verwenden weiterhin den ursprünglichen Plan.

## Börsenersatz

Frisch bestätigtes Fehlen oder Delisting eines Hyperliquid-Markts führt zur Ablehnung. Es gibt keine Ersatzbörse und keine Netzwerk-Ausweichroute. Die ursprüngliche Signalfrist und alle Preis-, Mengen-, Risiko- und TP-Mindestgrößenprüfungen bleiben verbindlich. Frühere Ablehnungen werden nicht erneut ausgeführt.

Hyperliquid verwendet ausschließlich die native Mark-Referenz und ausführbare Bid-/Ask-Daten. Anfragebeginn, Börsenzeit und Beobachtung bleiben auf die bestehenden zwei Sekunden begrenzt.

Der Plan speichert ursprüngliche Setup-ID, Quelle, Pipeline, Routing-Leg, angefordertes und tatsächliches Ziel, Konto, Netzwerk, Revisionen und Risikoparameter. Updates verwenden diese Identitäten, nicht den heutigen Rang. Diagnose, Telegram und Sheets zeigen die Herkunft.

## Prozentziele

Neue `take_profit_unit=roe_percent`-Ziele folgen der festen Betreiberregel `source-roe-v1`: nominaler Gewinn auf die eigene eingesetzte Anfangsmargin, vor Gebühren/Funding. Zahlen und Prozentzeichen müssen im Original belegt sein, auch ohne ROI-/ROE-Beschriftung. Netto/annualisierte Sonderdefinitionen werden abgelehnt. Absolute Preise bleiben `price`; gekürzte Zahlen aus Prozentangaben sind keine Preisbelege.

```
Long-TP  = eigener finaler Entry-VWAP × (1 + ROE / (100 × eigene Margin-Leverage))
Short-TP = eigener finaler Entry-VWAP × (1 - ROE / (100 × eigene Margin-Leverage))
```

Der Originalsignalhebel bleibt Evidenz. Ausführungshebel wird abgerundet und gedeckelt oder vom Fixed-/Backup-Profil bestimmt. Die eigene Margin-Leverage folgt dieser tatsächlichen Basis. Previewpreise sind vorläufig; nach bestätigten vollständigen eigenen Entry-Fills einschließlich Teilfill-/Cancel-Reconciliation wird die VWAP-Basis dauerhaft eingefroren. Updates behalten diese eigene Marginbasis. Tick-Rundung, Mindestgrößen, adaptive Verteilung und Stop-Leiter gelten weiter. Gespeicherte Legacy-`roi_percent`-/`course_percent`-Pläne behalten ihre Originalhebel-/Signalentry-Semantik und Operationsidentitäten.

## Analyse und Einführung

Jede Rangbewertung baut einmal einen lesenden Index aus den Tradefamilien, offenen Zielbezügen und noch ungefüllten Legs auf. Der Index verändert weder die vollständige Proofhistorie noch das konfigurierte Bewertungsfenster; spätere Fundingbelege und alte Rang-/Netzwerkbindungen bleiben maßgeblich. Die vollständigen Ergebnis- und Ausschlussdaten werden gegen die eingefrorene bisherige Funktion geprüft. Scan-Zählungen für 5.000 Familien belegen einen Durchlauf je Trade-/Ereignismapping; lokale Zeitmessungen sind keine Zusage einer VPS- oder Börsenlatenz.

Bei vielen HL-DEXs erfasst eine frische [native WebSocket-Kontoübersicht](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions) alle Positionen und jede DEX-Orderliste. Nichtleere Orderlisten werden mit vollständigen Reduce-only-Belegen gelesen. Fehlende, fremde oder veraltete Teilansichten erlauben keinen Entry. Damit sind auch Testnet-Konten mit mehr als 64 DEX-Kontexten vollständig prüfbar.

Sheets ergänzt Ränge/Ranghistorie und Herkunfts-/Risikospalten, ohne bestehende Zeilen-IDs zu ersetzen. Primäre HL-Mainnet-Tabs bleiben erhalten; weitere Konten erhalten eigene Börsen-/Netzwerktabs. `journal_read`/`journal_statistics` können `account_key` auswählen. Monatsberichte bleiben auf tatsächliche Mainnet-Abschlüsse beschränkt. Telegram zeigt Rang, Netzwerk, Risikofaktor, RPR und belegtes SL-Risiko, ROI mit Kurs sowie Ersatz. Leere Läufe bleiben stumm.

Die Migration bewahrt frühere Order-/Trade-IDs, Nonces, Historie und Quellen-Cursor. Alte Testnet-Archive werden nicht importiert oder wieder ausgespielt. Vor Veröffentlichung: Gesamtprüfungen, Adapterverträge, native Hermes-Autorität/Telegram, Paketprüfung und Datensicherung. Konten werden lesend geprüft; keine synthetischen Mainnet-Testorders.
