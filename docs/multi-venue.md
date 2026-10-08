# Profile, Pipelines und Börsen

Der gemeinsame Python-Handelskern läuft mit je einem Konto auf Hyperliquid, Aster und Lighter. Der native Hermes-Bot ist der einzige Bedienweg. Es gibt keine neue Bot-Anbindung oder unabhängige Ausführungsschleife. SQLite hält den gemeinsamen Zustand mit genau einem Schreibprozess; jede Börse erhält darin eine gefilterte Sicht auf ihre Trades, Bindungen, Operationen und Nonces.

## Bedienung

1. Über Telegram zuerst Quellen abonnieren und benannte Profile erstellen. Profile speichern Risiko je Trade, Hebel, Entry, TP-Aufteilung und erlaubte Updates; Geldbeträge heißen `fixed_notional_quote` und `max_notional_quote` und gelten in der Abrechnungswährung des Ziels.
2. Je Börse explizite `max_open_trades` und `max_total_risk_percent` festlegen. Sie zählen alle eigenen Pipelines und ungeklärten Reservierungen dieses Kontos gemeinsam.
3. Pipelines aus einer Quellenliste und höchstens einem Ziel je Börse einrichten. Ein Ziel enthält `exchange` (`hyperliquid`, `aster`, `lighter`) und `profile_id`. Neue Pipelines sind zunächst deaktiviert; ihre Aktivierung ist eine Betreiberentscheidung.
4. Aster und Lighter bleiben deaktiviert, bis Konten, API-Zugänge, Limits und ein ausdrücklicher Aktivierungsauftrag vorliegen. Ihre vorkonfigurierten Netzwerke sind Mainnet; Hyperliquid behält seine vorhandene Umgebung.

`status` liefert `configuration_revision`, `profiles`, `pipelines` und `exchanges`. Neue `control`-Aktionen heißen `set_profile`, `set_pipeline`, `delete_profile`, `delete_pipeline`, `set_exchange_limits` und `set_exchange_enabled` und benötigen die aktuelle Konfigurationsrevision. `set_policy` bleibt als Kompatibilitätsweg für Profil „Standard“ und Hyperliquid-Kontolimits verfügbar.

Beim ersten Start migriert Schema 1 zu Schema 2: vorhandene Regeln werden Profil „Standard“, vorhandene Quellen und Betreiber-Weiterleitungen verwenden die aktive Pipeline `legacy`. Bestehende Trade-, Plan-, Order- und Operations-IDs werden nicht neu berechnet. Eine explizite aktive Pipeline verdrängt den Legacy-Fallback für ihre Quelle. Für andere Quellen bleibt das bisherige Verhalten bestehen. `legacy` und `standard` können nicht gelöscht werden; die Legacy-Pipeline kann deaktiviert werden.

## Ausführung und Wiederanlauf

Ein Signal wird einmal interpretiert; seine Zielauswahl wird dauerhaft eingefroren. `trade_preview` liefert je Ziel eine `plan_id` oder einen begründeten Fehler. `trade_execute` akzeptiert eine einzelne `plan_id` oder eine Liste `plan_ids`. Jedes Ziel besitzt eigene unveränderliche Parameterrevisionen und Ausführungsidentitäten. Es gibt keine Ausweichbörse und kein erneutes Senden unbekannter Ergebnisse. Ausfälle einer Börse blockieren keine andere Börse.

Pro Konto und Markt bleibt ein aktiver eigener Trade zulässig. Fremde Positionen werden nicht übernommen. Ungeklärte Operationen sperren weitere Entries des betroffenen Kontos. Pausen und Profiländerungen berühren bestehende Schutz- und Exit-Verwaltung nicht; ungesendete Pläne werden bei relevanten Konfigurationsänderungen ungültig. Quellenupdates verwenden die ursprüngliche Setupfamilie und jede gespeicherte Tradepolicy. Ein expliziter `bound_trade_id` wählt genau einen Trade; mehrere unabhängige Setups bleiben mehrdeutig.

Ohne bestätigten wirksamen Stop versucht der Kern eine Reduce-only-Schließung der eigenen Restmenge und sperrt neue Entries dieser Börse. Eine verlorene Schließungsantwort bleibt ungeklärt und wird nicht mit einer neuen ID wiederholt. Vorhandene Schlüssel, Regeln oder Handelszustände werden dadurch nicht ersetzt.

## Adapter und Zugangsdaten

Aster verwendet Futures V3 mit EIP-712, korrekt übermittelten `user`/`signer`/`nonce`-Parametern und monoton reservierten Mikrosekunden-Nonces. Die offizielle MCP-Version `71fa3cf02401573f7450668c265c8f4b11c78db0` ist eine Referenz; ihr Nano-/Mikrosekundenfehler und die fehlende Client-Order-ID der öffentlichen Orderfunktion werden nicht übernommen. Mengen und Preise bleiben Dezimalstrings. One-way-Modus wird geprüft; ein gefülltes Konto wird nicht automatisch umgestellt. Leverage-Brackets, Lot- und Tick-Größen stammen aus der API.

Trade- und Reporting-Fillreads übergeben intern das gespeicherte Marktobjekt. Aster fragt dadurch nur dessen ursprünglichen nativen Kontrakt ab, unabhängig von anderen gecachten Märkten. Ein ausdrücklicher abweichender `symbol`-Beleg wird vor der Normalisierung abgewiesen; ein fehlendes Symbol wird nicht als Fremdmarkt interpretiert. Der allgemeine Adapterread bleibt kompatibel; Sättigungs-, Mengen-, Konto- und Fillidentitätsprüfungen bleiben aktiv. Hyperliquids öffentlicher `userFillsByTime`-Payload erhält dabei keine zusätzlichen Marktparameter.

Lighter verwendet `lighter-sdk==1.1.6` und dessen native Signierbibliothek. Nur `sign_*` wird zum Signieren verwendet; das eigene HTTP-Dispatch speichert jede signierte Transaktion vor ihrem einzigen Versand. `code: 200` ist eine Annahme, noch keine Ausführung: native Transaktionsstatus, Konto, API-Key-Index, Nonce und Hash müssen die Ausführung bestätigen. Integergrößen werden exakt geprüft; Marginfractions werden in Richtung geringeren Hebels gerundet und auch beim Sizing berücksichtigt. TPs können gemeinsam als signierter Transaktionsbatch versendet werden. Der initiale OTO-Stop benötigt native Nil-Werte für Menge und Client-ID; seine tatsächliche Order wird über die ursprüngliche Parent-Order, Markt und Trigger identifiziert. Ein zusätzlicher Test signiert diese Struktur mit der echten nativen SDK-Bibliothek, ohne sie zu versenden.

Die SDK-Metadaten begrenzen urllib3 auf eine veraltete Version. Das Projekt überschreibt dies mit dem bereits verwendeten und geprüften `urllib3==2.8.0`; der SDK-Transport für Beobachtung und Versand wird durch httpx ersetzt. Coincurve beschleunigt die ECDSA-Signaturen unter Python 3.12/3.13, ohne deren Inhalt zu ändern. Python 3.14 verwendet den kompatiblen eth-keys-Backend, da die gepinnte Beschleunigung hierfür kein passendes Linux-Wheel besitzt. Beide Abhängigkeiten gehören zum geprüften Lockfile.

Konfigurationsfelder unter `venues.aster`: `network`, `account` (Hauptwallet), `signer` (Pro-API-Wallet), `private_key_file`. Unter `venues.lighter`: `network`, `account_index`, `api_key_index` (dediziert, 4–254), `private_key_file`. Secrets gehören in geschützte Dateien unter `secrets/`, nie in Telegram, Sheets, Git oder den Skill. Fehlende oder nicht passende Keys dürfen den Hyperliquid-Dienst nicht verhindern.

## Journal und Prüfgrenzen

Trades, Orders, Fills und Vorgänge erhalten Börse, Netzwerk, Konto, Pipeline, Profilrevision und Abrechnungswährung. Der Synchronisierer erweitert alte Spalten vor den versteckten Digest-/Revisionsspalten und erhält bestehende IDs und Historie. Die neue Seite `aqla Pipelines` zeigt die Zuordnungen; `Handelsparameter` enthält Profile und gemeinsame Börsenlimits. Statistik gruppiert unterschiedliche Währungen und Netzwerke getrennt. Funding bleibt ausgeschlossen. Native Gebühren in einer nicht eindeutig umrechenbaren Einheit erzeugen keine erfundenen Nettogewinne.

Vertragstests verwenden lokale Exchange-Transportgrenzen und einen geskripteten Lighter-Signer. Sie prüfen Signatur, Nonces, Integerkonvertierung, verlorene Antworten, SL-Ausfall, Entries/TPs/Exits, unabhängige Ziele, Profilrevisionen, Quellenupdates, gemeinsame Limits und Journalmigration. Sie sind keine Behauptung ausgeführter Mainnet-Trades. Für die erstmalige Freischaltung bleiben dedizierte Kontoprüfung und ausdrückliche Nutzeraktivierung erforderlich.
