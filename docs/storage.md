# State- und Journaltransaktionen

Der State verwendet weiterhin eine einzelne atomare SQLite-JSONzeile und einen
Writer. `State.update` bleibt der vollständige Fallback: detached Vollkopie,
Journalvergleich, JSONnormalisierung, persistierter Commit, danach RAMwechsel.

`update_metadata` übergibt ausschließlich detached deklarierte, freigegebene
Metadatenwurzeln. Finanz-, Operations-, Konto-, Sequenz- und Journalhistorienwurzeln
sind dort nicht beschreibbar. Zusätzliche Wurzeln oder falsche Wurzeltypen werden
vor dem Commit abgewiesen. Die geänderten Wurzeln werden JSONnormalisiert;
unveränderte Daten bleiben erhalten. Persistenz erfolgt vollständig atomar vor
RAMveröffentlichung und vor einem nachfolgenden Exchange-Send.

`read` kopiert nur benannte Wurzeln beziehungsweise ausgewählte Dictionaryeinträge.
Es gibt keine Callbackansicht mit mutable Stateinternals. Zurückgegebene nested
Daten sind unabhängig vom gespeicherten State. Accountviews delegieren solche Reads
mit denselben vorhandenen Scope-/Kontofilterregeln.

Journal-ACK ist eine eigene geprüfte Teiltransaktion. Sie entfernt einen Pendingkey
nur bei gleichzeitig passendem aktuellen und Pendingfingerprint. Historienzeilen
werden nur über diesen bestätigten ACKpfad freigegeben. Sequenz-, Zeit- und
Initialisierungswirkungen entsprechen dem Genericpfad, einschließlich leerer und
stale ACKs. Für Signale/Historie werden nur benötigte Records projiziert; andere
Tabellen verwenden zunächst den vollständigen Fallback. Historyoverrides und die
vorhandene Pendingreihenfolge bleiben erhalten.

Feste Monatsjoboperationen lesen aktuelle Finanzbelege unter demselben Lock. Sie
stellen nur die geprüften Job-/Requestfunktionen bereit, keinen beliebigen Callback
mit Journalbypass. Der bestehende vollständige Monats-ACK mit Notificationerzeugung
bleibt bis zur Integration seines Notificationvertrags auf dem Genericpfad.

`update_if_changed` vermeidet ausschließlich einen tatsächlichen JSONgleichen
Generic-No-op. Typen, Einfügereihenfolge und explizite Revisionseffekte bleiben
vergleichsrelevant. Der Rangaufruf in `Router.recover` verwendet dies; sonstige
Recovery-/Expirytransaktionen bleiben fachlich unverändert. Identische bereits
bekannte Router-Nachrichten erzeugen keine zusätzliche Rang-/Sequenzmutation;
erstmalige Rangbindung und neu aufgelöste Aliase werden weiterhin persistiert.

Der vollständige JSON-Diskwrite bleibt ein messbarer Restaufwand. Die Optimierung
ändert weder Persistenzformat noch Crash-, Ownership- oder Unknownoperationregeln.
Zeitmessungen verwenden synthetische Daten und begründen keine SLA- oder
Livequalitätsgarantie.
