# Native Telegram-Befehle

`/help` und `/commands [Seite]` zeigen die Abschnitte **Hermes Agent** und **aqla Agent-System**. Der Hermes-Abschnitt folgt dem aktiven nativen Befehlskatalog mit dessen Berechtigungsfiltern, Skillbefehlen und numerischen Seiten. Die Telegram-Menübeschreibungen beginnen mit `Hermes:` oder `aqla:`. Das private Betreibermenü reserviert Platz für sämtliche aqla-Befehle, auch bei der nativen Standardgrenze von 60 Menüeinträgen. Andere native Menüscopes und getippte Hermes-Befehle behalten ihre normale Funktion.

Die Systembefehle funktionieren ausschließlich für den tatsächlichen Betreiber im privaten Chat dieses Bots. Sie verwenden Python und die vorhandenen MCP-Gates; kein Sprachmodell interpretiert oder bestätigt Eingaben.

| Befehl | Inhalt | Setup |
| --- | --- | --- |
| `/setup [status\|aus]` | Private Freigabe starten, Zeit lesen oder widerrufen | Passwort nur nach Nachfrage |
| `/aqla_status` | Handelsvorlagen, Konten, Sync und verbleibende Setup-Zeit | Nein |
| `/aqla_kanaele` | Kanalränge, Metriken, Nachweise und Ausschlüsse | Nein |
| `/aqla_profile` | Profile mit ihren vollständigen Parametern | Nein |
| `/aqla_journal` | Link zum tatsächlich konfigurierten Journal-Buch und Syncstatus | Nein |
| `/aqla_rang` | Bestehenden Kanalrang setzen | Ja |
| `/aqla_parameter` | Einen Profilparameter ändern | Ja |
| `/aqla_maker` | Entry, TP oder beide Ausführungsmodi ändern | Ja |
| `/aqla_limits` | Offene Trades oder Gesamtrisiko eines Kontos ändern | Ja |
| `/aqla_pause` | Neue Entries sofort pausieren; Schutz läuft weiter | Nein |
| `/aqla_fortsetzen` | Neue Entries nach den konfigurierten Regeln freigeben | Ja |

Ohne Argumente führen Rang, Parameter, Maker und Limits durch **Ziel → Feld → Wert**. Eine Seite hat höchstens acht Auswahlwerte. Boolesche Werte und Aufzählungen besitzen Schaltflächen. Zahlen und JSON-Listen werden gezielt mit aktuellem Wert, Einheit und Beispiel abgefragt. `/cancel`, die Schaltfläche **Abbrechen** oder ein neuer Dialog beendet die vorherige Eingabe.

Direkte Beispiele:

```text
/aqla_rang "Kanalname" C
/aqla_parameter standard leverage 5
/aqla_parameter standard risk_percent 0,75
/aqla_parameter standard allowed_updates ["move_stop", "close"]
/aqla_parameter standard fallback_take_profit_percentages [1, 2]
/aqla_maker standard entry maker_only
/aqla_maker standard tp standard
/aqla_maker standard beide maker_only
/aqla_limits hyperliquid:testnet "offene Trades" 5
/aqla_limits aster:mainnet Gesamtrisiko 1,5
```

Profil-IDs und eindeutige exakte Profilnamen sind zulässig. Kanäle lassen sich über ihre vorhandene ID oder ihren eindeutigen Namen auswählen; ein mehrdeutiger Name führt zur Auswahl der konkreten ID. Konten sind immer `Börse:Netzwerk`. Parameterfelder verwenden die vorhandenen Schemanamen. `null` schaltet einen optionalen Wert aus. Dezimalwerte akzeptieren Punkt oder Komma; Zahlenlisten verwenden JSON, bei Dezimalkomma mit Strings, etwa `["1,5", "2,5"]`. Sämtliche vollständigen Profilregeln werden erneut validiert, ungewählte Werte bleiben erhalten.

Eine gültige letzte Eingabe wird unmittelbar gespeichert. Danach folgt eine kurze alte/neue Bestätigung. Bei zwischenzeitlich geänderter Konfiguration oder Rangrevision wird der aktuelle Wert erneut gezeigt und eine neue Eingabe verlangt. Es wird kein alter vollständiger Snapshot darübergeschrieben.

Ein Dialog lebt nur im Arbeitsspeicher, höchstens 300 Sekunden und höchstens bis zum festen Setup-Ablauf. Aktivität verlängert keine dieser Zeiten. Er gehört zum Betreiber, privaten Chat und aktuellen Gateway-Session; neue Session, `/new`, `/reset`, `/stop`, `/restart`, Reconnect oder Plugin-Unload verwerfen ihn. Schaltflächen enthalten zufällige, einmal verwendbare Tokens. Ein Klick eines anderen Nutzers, ein Gruppenchat, ein abgelaufener oder wiederholter Token ändert nichts. Passwortabfang läuft vor allen Dialogen und nativen Befehlen. `/setup` beendet eine offene Werteingabe, bevor die Passwortnachfrage beginnt.

Entry und TP besitzen unabhängige `standard`-/`maker_only`-Werte im Profil. Das vorhandene Routing übernimmt sie in neue unveränderliche Pläne und die nativen Hyperliquid-, Aster- und Lighter-Aufträge. Bestehende Pläne behalten ihre Identität und Ausführung. Standardprofile behalten ihre bisherige Serialisierung. Maker-Aufträge verlangen strikt Post-only; es gibt keine Taker-Ersatzorder. Schutzaufträge verwenden ihre bestehenden Regeln.

Die Runtime bindet ihre Anpassungen vor `connect()` an den tatsächlich vom nativen Gateway erzeugten Telegram-Adapter, einschließlich des Plugin-Registry-Factories. Verträge werden an dessen Klasse und tatsächlichen Methoden geprüft. Der früheste PTB-Handler läuft in Gruppe `-100`, bevor Text-, Foto-, Album- oder Busy-Batching. Eine fehlende private Bindung sperrt den Eingang, meldet bei Systembefehlen fehlende Bereitschaft und löscht weiter bekannte späte Passwortantworten. Der Startlog bestätigt ausschließlich Adaptermodul, Hook-/Handlerbereitschaft und Kataloganzahl; private Prüf- oder Freigabedaten werden nicht ausgegeben.
