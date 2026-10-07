# Mainnet und manueller Parallelhandel

Die aktive Hyperliquid-Konfiguration verwendet `shared_account: true`. Manuelle Positionen und offene Perpetual-Entry-Orders belegen Kontoplätze und Margin. Ein Markt zählt einmal, unabhängig von mehreren Orders oder einer bereits vorhandenen Position. Aqla-Reservierungen einschließlich ungeklärter Entries zählen ebenfalls; Reduce-only-TP/SL-Orders und Spot-Guthaben belegen keinen weiteren Perpetual-Platz.

Aqla eröffnet nur auf freien Märkten. Fremde Orders und Positionen werden nicht verändert. Das gemeinsame maximale Trade-Limit wird aus der Vereinigung beobachteter Märkte und eigener Reservierungen berechnet. Das SL-Risikobudget betrifft weiterhin Aqla-Trades; manuelle Stops oder unbekannte Verlustrisiken werden nicht erfunden.

Die Kontoübersicht liest sämtliche DEX-Positionen und detaillierte offene Orders mit begrenzter Parallelität. Bei Unified Account stammt die USDC-Equity aus der gemeinsamen Spot-Balance; die verfügbare Margin ist durch Holds und die zusätzliche Börsen-Maintenance-Obergrenze begrenzt. Einzelne DEX-Kontowerte werden nicht addiert. Standard bleibt unterstützt; Portfolio Margin und unvollständige oder veraltete Beobachtungen blockieren neue Entries. Vor Leverage- und Entry-Versand wird nach der Signerprüfung nochmals frisch zugelassen.

Manuelle Eingriffe in einen Aqla-Markt erzeugen `conflicted`. Bestehende Schutzorders bleiben erhalten, die automatische Verwaltung dieses Marktes hält an. Eine gemischte Position wird nicht automatisch geschlossen. Die Reservierung bleibt erhalten; andere freie Märkte können bei eindeutigen Budgets weiterarbeiten. `status.account_inventory` und Telegram zeigen Plätze, Margin und Konflikte.

HIP-3-Asset-IDs entsprechen dem offiziellen SDK: Arrayposition 1 in `perpDexs` beginnt bei 110000, anschließend folgen 10000er-Schritte. Die ursprünglichen Asset-IDs bereits gespeicherter Ausführungen werden nicht umgeschrieben. Beim Mainnet-Wechsel entstehen neue Ausführungsidentitäten aus neuer Netzwerk- und Kontobindung.

Mainnet verwendet einen getrennten SQLite-Zustand. Profile, Backup, Quellen, Namen, Pipelines und Limits werden übernommen. Alte Hyperliquid-Trades, Order-IDs und Nonces verbleiben im Testnet-Archiv. Quellen-Cursor und Cutover-Zeit verhindern die erneute Ausführung alter Nachrichten; alte Events werden ausschließlich historischer Kontext. Ein vom Betreiber manuell geschlossener Testnet-Trade wird nur nach belegtem Nullbestand und terminalen eigenen Orders als geschlossen bestätigt. Es wird weder erneut geschlossen noch fehlender PnL berechnet.

`google_sheets.tab_namespace: mainnet` verwendet `aqla Mainnet …`-Tabs mit eigener `aqla.scope.mainnet`-Kontobindung. `legacy` bleibt der Standard und erhält bisherige `aqla …`-Tabs; `testnet` unterstützt entsprechend getrennte neue Testnet-Tabs. Vorhandene Inhalte und Metadaten werden nicht gelöscht oder umgebunden.

Die gelieferten Mainnet-API-Zugangsdaten liegen ausschließlich in geschützten Serverdateien. Mainnet wird nach den Qualitäts- und lesenden Kontoprüfungen direkt aktiviert. Es wird kein künstlicher Echtgeld-Testtrade eröffnet; Entries beginnen mit neuen gültigen Signalen.
