# Zuverlässige Ausführung und weniger Routinearbeit

Der externe Befund zu `85ae2859` wurde gegen `bf6f619` im vollständigen
Repository geprüft. Reproduktionen verwenden den echten Zustands- und
Ausführungscode mit Fake-Börsen; keine realen Testorders.

| Bereich | Änderung | Nachweis |
| --- | --- | --- |
| Close-Recovery | Gespeicherte Close-Intents werden vor normaler Verwaltung für beide Lifecycle-Versionen fortgesetzt. Unklare bestehende Identitäten werden nie ersetzt. | Zustandsneustart bei vollständigem Close und Teilreduktion, einschließlich Split-Entry und Waverider |
| Lighter | Ein gemeinsamer Buchaufbau verarbeitet Snapshot, einseitige Deltas, Löschungen und Nonce-Kontinuität. Fehler löschen den Beleg und verlangen einen neuen Snapshot. | Tiefe Preisstufen ändern nicht den BBO; Lücken, Replay, ungültige Werte und veraltete Zeitstempel werden gesperrt |
| Managed-Refresh | Der Dispatcher unterscheidet `unchanged`, `sent`, `cancelled` und `unknown`. Nur mögliche finanzielle Änderungen erfordern einen zweiten Abgleich. | Ohne wartenden Entry genau ein Durchlauf; bestehende Storno-/Zwischenfill-Tests bleiben erhalten |
| Kontoarbeit | Kurze, exklusive Arbeitspakete statt einer Sperre über den gesamten Kontodurchlauf; priorisierte Schließungen und Abgleich unklarer Operationen. Unabhängige Update-Ziele laufen begrenzt parallel. | Prioritäts-, Abbruch-, Sperr- und Mehrkontentests |
| Ereignisse | Die vorhandene dauerhafte Outbox und der native Hermes-Adapter versenden in Legacy, Shadow und Hybrid. Eine reine Meldung weckt kein Modell. | Echter MCP-Outboxpfad mit gesperrten Modellaufrufen; verlorene Versandbestätigung bleibt ausdrücklich ungewiss |
| Zustand | Zusammenfassung und Kontoprojektionen filtern vor dem Kopieren. Metadaten werden in derselben SQLite-Zeile atomar mit `json_set` aktualisiert; leere Outbox-Polls schreiben nichts. | Neustart, Schreibfehler, exklusive Writer-Lease und `synchronous=FULL` |
| Marktfeeds und Replay | Beobachter teilen öffentliche Feeds je Börse/Netzwerk/Markt. Lighter-Kursleser nutzen denselben Buch-/Mark-Feed. Replay hat genau einen asynchronen Schreibpfad mit Budget, Aufbewahrung und Gesundheitsstatus. | Geteilte Verbindung, unveränderte Frischegrenzen, deaktivierte Aufzeichnung, Budget und Schutz aktiver Aufzeichnungen |
| Kombinierte Verwaltung | Ein typisierter Plan reduziert zuerst eine belegte Menge und ändert danach den Stop des nachgewiesenen Restes. | Neustart nach Reduktion, unbekannter Versand, Source-Edit und feste Mengenbasis |

## Grenzen der finanziellen Ausführung

Konto-, Eigentums-, Konfigurations-, Quellen- und Kursprüfungen bleiben erhalten.
Ein Marktbeleg darf weiterhin höchstens zwei Sekunden alt sein. Ein erneutes
Lesen des Feed-Caches erneuert keinen Zeitstempel. Private Kontobelege werden
nicht zwischen Konten geteilt.

Die Konto-Sperre ist reentrant, innerhalb derselben Priorität FIFO und
abbruchfest. Unklare Operationen haben Vorrang; explizite Verwaltungsaufträge
liegen vor Routinearbeit und optionalen Wellen. Routinepakete sind auf
höchstens 30 Sekunden begrenzt, ohne die bisherigen Börsen- oder
Frischegrenzen zu erweitern. Ein Zeitbudgetabbruch liefert einen Fehlerbeleg
und lässt spätere Trades weiter prüfen; eine abgebrochene Kontorunde speichert
ihre Fortsetzungsposition. Bereits bestätigte Schutzorders bleiben bestehen.

Ein belegbar nie eingereichter Einstieg darf keine fremde Net-Position
reservieren. Eine solche lokale Reservierung kann als abgelehnt freigegeben
werden, wenn **alle** Entry-Legs nie gesendet wurden und ihre nativen
Identitäten unbekannt sind. Ein unbekannter Sendeausgang oder eine signierte
weitere Split-Leg verhindert diese Auflösung. Fremde Positionen und Orders
werden dabei weder übernommen noch geändert.

## Unterstützter kombinierter Auftrag

`trade_update` unterstützt `update_action=sequence` mit genau zwei
`management_steps`: `reduce`, danach `break_even` oder `move_stop`.
Jede Aktion benötigt ihren vollständigen Originalsatz als Beleg; Zahlen
benötigen zusätzlich ihren eigenen Beleg. Beispiel:

```json
{
  "update_action": "sequence",
  "management_steps": [
    {"action": "reduce", "reduce_percent": "50"},
    {"action": "break_even"}
  ]
}
```

Die Prozentmenge bezieht sich auf den bestätigten eigenen Bestand vor dem
gesamten Auftrag. Python speichert diese Basis und jede Ausführungsidentität.
Weitere Reduktionsversuche berücksichtigen tatsächliche eigene Fills und
sind auf drei eindeutig zugeordnete Aufträge begrenzt. Eine unklare frühere
Operation sperrt weitere Aufträge. Der zweite Schritt wartet auf die
bestätigte Reduktion. Er übernimmt die bestehenden Stop-, Mengen- und
Risikoprüfungen. Eine geänderte Quelle beendet ungesendete Folgeschritte.
Ein erschöpfter oder abgelehnter Ablauf verlangt sichtbar eine Prüfung.

Andere Reihenfolgen, mehrere Märkte, zusätzliche Aktionen und bedingte oder
zukünftige Verwaltungsaufträge bleiben nicht ausführbar. Alte gespeicherte
Interpretationen behalten ohne `management_steps` ihr bisheriges Format.
Kandidaten und gemeinsame Beleg-/Semantikprüfungen liegen in
`source_candidates.py` und `interpretation_contract.py`, unabhängig vom
optionalen Modellanbieter. Bestehende Python-Importe bleiben reexportiert.

## Replay und Messung

Optionale Konfiguration in `config.yaml`:

```yaml
waverider_replay:
  enabled: true
  max_bytes: 536870912
  retention_days: null
```

Ohne Angabe gelten die bisherigen 512 MiB und keine automatische Löschung.
Eine gesetzte Aufbewahrungsfrist löscht nur vollständig abgelaufene oder als
beendet belegte Setup-Aufzeichnungen; aktive `until_target`-Setups bleiben
erhalten. Sättigung, Verzögerung und Ausfall erscheinen in
`status(view=summary).replay_recorder`. Fehlende Beobachtungen werden niemals
als bestandener historischer Nachweis ausgegeben.

`scripts/benchmark_execution_work.py` verwendet ausschließlich eine lokale
Mock-Börse und isolierten Zustand. Zehn Durchläufe mit 1000 synthetischen
historischen Nachrichten ergaben auf demselben Arbeitsplatz:

| Messgröße | `bf6f619` | Änderung |
| --- | ---: | ---: |
| Info-Abfragen je unverändertem Refresh | 30 | 15 |
| Lokaler Median | 573 ms | 82 ms |
| Python-/SQL-Payload einer kleinen Metadatenänderung | 1.492.168 Byte | 59 Byte |
| Native Legacy-Zustellung ohne Modell | nicht verfügbar | eine Zustellung, null Modellaufrufe |

Das ist kein Live-Latenzvergleich. SQL-Payload ist nicht gleich physischem
Schreibvolumen; SQLite verwaltet weiterhin einen atomaren JSON-Zustand.
Finanzielle Transaktionen behalten ihren vollständigen Commit. Es wurden
weder ein neues Datenbankschema noch zusätzliche Serverdienste eingeführt.
Reale Kontorunden und Wartezeiten lassen sich über `account_recovery` und
`status(view=summary).account_work` prüfen.

Lighter-Protokoll: [offizielle WebSocket-Referenz](https://apidocs.lighter.xyz/docs/websocket-reference).
