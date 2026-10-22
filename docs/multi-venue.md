# Getrennte Hyperliquid-Konten

Der Agent führt ausschließlich auf Hyperliquid aus. Mainnet und Testnet bleiben
getrennte Kontokontexte mit eigenen Schlüsseln, Bindungen, Nonces, Limits und
Freigaben. Kanalrang und freigegebenes Profil bestimmen das Netzwerk. Fehlende
Märkte führen zu einer nachvollziehbaren Ablehnung. Es gibt keine Börsenersatz-
oder Netzwerkausweichroute und keine Erhöhung der Menge für Mindestgrößen.

Der primäre Kontext stammt aus `network`, `account`, `private_key_file` und
optional `vault_address`. Ein weiterer Kontext steht unter `execution_accounts`
mit dem Schlüssel `hyperliquid:mainnet` oder `hyperliquid:testnet`. Zusätzliche
Konten beginnen deaktiviert; die Freigabe verlangt bestätigten Signierer,
Kontoinventar, Limits und vollständigen Abgleich. Ein vorhandener Kontext darf
nicht an eine andere Kontoadresse umgebunden werden.

Die Migration archiviert frühere Ausführungskonfiguration. Bestehende Trades,
Orders, Fills, Gebühren, Funding, Nonces und Journaleinträge bleiben erhalten.
Ein entferntes Konto mit offener Handelsarbeit oder ungeklärtem Sendeausgang
verhindert die Migration; finanzielle Arbeit wird niemals still verworfen.
Veraltete SDKs, Signierer, Websocket-Feeds und aktive Zugangsdaten werden für
den laufenden Agenten entfernt. Historische Börsenlabels sind reine Daten.

Schutz und explizite Schließungen haben Vorrang vor Routinearbeit und optionalen
Waverider-Aktionen. Ein verlorener Sendeausgang verlangt Abgleich der bestehenden
Identitäten. Ein unbekannter Entry wird niemals durch eine neue Order ersetzt.
Siehe [Prozesssicherheit](process-reliability.md).
