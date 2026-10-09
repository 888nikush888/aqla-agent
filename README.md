# aqla Agent

[![Quality](https://github.com/888nikush888/aqla-agent/actions/workflows/quality.yml/badge.svg)](https://github.com/888nikush888/aqla-agent/actions/workflows/quality.yml)

Telegram-Quellen → Hermes versteht Signal/Update im Kontext → Python prüft Profile und Pipelines → Hyperliquid, Aster und/oder Lighter führen unabhängig aus → Hermes meldet das Ergebnis je Börse in Telegram.

Hyperliquid-Mainnet unterstützt manuelle Positionen neben Aqla auf freien Märkten. Gemeinsame Plätze und Unified-Account-Margin werden berücksichtigt; eigene Trades und fremde Orders bleiben getrennt verwaltet. [Mainnet und Parallelhandel](docs/mainnet-shared-account.md).

Gold/XAU wird auf Hyperliquid als `xyz:GOLD` erkannt; Dezimalhebel werden abgerundet und gedeckelt. Eigene Kanalnamen vereinfachen die Bedienung. Ein globales Profil „Backup“ ergänzt fehlende Signalangaben: anfangs Hebel 2 und Markt-Entry; SL-/TP-Prozentfelder bleiben bis zu deiner Konfiguration leer. [Bedienung und Ausführungsregeln](docs/gold-backup-and-names.md).

Eigenständiges Python-Projekt. Ein kontrollierter Handels-MCP, ein kurzer bearbeitbarer Master-Skill und ein read-only Telegram-Quellenleser für abonnierte Kanäle. SQLite speichert Handelszustand und Hermes-Gespräche. Keine eigene Bot-Anbindung, MiniApp oder autonome Strategieoptimierung. Manuell an den Hermes-Bot weitergeleitete Signale werden ebenfalls als Quellen erfasst.

Kanalränge F–S gewichten das Profilrisiko und wählen Testnet oder Mainnet für neue Entries. Bei einem bestätigten fehlenden Hyperliquid-Markt können Pipelines auf Aster oder Lighter im selben Netzwerk ausweichen. Belegte ROI-/ROE-TPs werden mit dem ursprünglichen Signalhebel in Kursziele übersetzt. [Ranking, Konten, Börsenersatz und Prozentziele](docs/ranking-routing-roi.md).

Betreiberänderungen an Rang, Handelsregeln, Quellen/Pipelines, Limits und Aktivierung sind durch den [privaten Setup-Modus](docs/setup-mode.md) geschützt: `/setup`, native Passwortprüfung und bestätigtes Löschen, dann 300 Sekunden Freigabe. Status, Pause/Disable und Schutz bleiben verfügbar.

Telegram-Handelsmeldungen verwenden kompakte, aus gespeicherten Fakten erzeugte Vorlagen mit getrennten Börsenergebnissen, bestätigten Preisen und klaren Statusüberschriften. [Nachrichtenansichten](docs/telegram-messages.md).

Ausdrücklich verzögerte Entries warten im lokalen Zustand auf ihren belegten Einstieg. Frische ausführbare Kurse und die native SL-Triggerreferenz werden vor Versand und nach Fills geprüft. Einstieg und Schließung bleiben als getrennte Ereignisse sichtbar; `trade_diagnostics` erklärt auch geschlossene Trades anhand ihrer tatsächlichen Orders und Fills. [Verzögerte Entries und Diagnose](docs/deferred-entries.md).

Monatsberichte zeigen jeden vollständig geschlossenen Aqla-Mainnet-Trade, belegten Netto-PnL einschließlich Gebühren/Funding und ROE auf die berechnete Anfangsmargin. Vollständige Trade-ROEs werden arithmetisch addiert, Geldbeträge je Währung getrennt. Die Tabelle liegt in Sheets, die Zusammenfassung kommt am Folgemonatsersten um 09:00 Europe/Berlin über Hermes. Erster regulärer Bericht: Oktober 2026. [Berechnung, Datenlücken und Bedienung](docs/monthly-reports.md).

## Unabhängig auf einem VPS installieren

Voraussetzungen: Linux mit systemd, Git und uv. Der Installer richtet eigene Python-Laufzeiten im Zielordner ein: 3.12 für Aqla, 3.14 für den exakt gepinnten Hermes-Stand. Eine vorhandene Hermes-Installation wird nicht angebunden oder verändert.

1. Quellpaket in einen separaten Ordner entpacken, beispielsweise `/opt/aqla-source`. Im Quellordner:

   ```sh
   uv sync --frozen --extra sources --extra sheets --extra exchanges
   uv run aqla-install-hermes --destination /opt/aqla --owner-id DEINE_TELEGRAM_USER_ID
   ```

   `/opt/aqla` muss neu oder leer sein. Mit ausreichenden Verzeichnisrechten ausführen. Der Zielordner erhält eigene Quellen, Konfiguration, Secrets-Verzeichnis, Hermes-Home, zwei venvs und eine systemd-Datei. Interpreter werden ebenfalls dort verwaltet; der Dienst hängt nicht an einem Python unter dem privaten Root-Home.

   Standardmodell ist **DeepSeek V4.1 Flash über OpenCode Go** (`deepseek-v4.1-flash`, `https://opencode.ai/zen/go/v1`) mit Hermes-Reasoning **ultra**. Hermes übersetzt `ultra` für DeepSeek in die API-Stufe `max`. Andere Modelle und Endpunkte lassen sich ausdrücklich mit `--model` und `--model-url` wählen; Reasoning kann anschließend über Hermes geändert werden.

2. `/opt/aqla/vps.env` ausfüllen:

   ```dotenv
   TELEGRAM_BOT_TOKEN=DEIN_BOT_TOKEN
   HERMES_MODEL_API_KEY=DEIN_OPENCODE_GO_API_KEY
   ```

   `HERMES_HOME`, `PYTHONPATH`, Owner und Rückkanal setzt der Installer. Zugangsdaten gehören in diese geschützten Dateien, nicht in den Skill oder Signalnachrichten.

3. `/opt/aqla/config.yaml` auf dein Konto einstellen:

   ```yaml
   network: testnet  # für deinen gewünschten Betrieb ausdrücklich mainnet oder testnet
   account: "0xDEINE_TATSAECHLICHE_KONTOADRESSE"
   signer_role: api_wallet
   private_key_file: secrets/hyperliquid.secret
   ```

   Das Secretfile enthält ausschließlich den Signierschlüssel. Bei einem API-Wallet ist `account` die Adresse des zugehörigen Hauptkontos. `vault_address` ist separat optional. `integration.secret` erzeugt der Installer; es bindet vertrauenswürdige native Hermes-Aufträge an den MCP.

4. Für die automatische Aufnahme abonnierter Kanalnachrichten den Quellenleser konfigurieren. Dieser liest Nachrichten über dein Telegram-Konto; Antworten und Bedienung übernimmt ausschließlich Hermes:

   ```yaml
   telegram_api_id: DEINE_TELEGRAM_API_ID
   telegram_api_hash_file: secrets/telegram-api-hash.secret
   telegram_session_file: secrets/telegram-session.secret
   ```

   Dann im Zielordner:

   ```sh
   .venv/bin/aqla-telegram-login --config config.yaml
   ```

   Die Anmeldung speichert eine `StringSession` als Secretfile; es entsteht keine SQLite-Telegram-Sitzung. Der Account muss bereits Zugriff auf die Quellen haben. Der Leser tritt keinem Kanal bei und schreibt keine Nachrichten. Quellen können als Chat-ID, `@username` oder `https://t.me/username` angegeben werden.

5. Ein eigenes Dienstkonto verwenden und die generierte Unit installieren. Beispiel bei einer Installation durch root:

   ```sh
   sudo useradd --system --create-home --home-dir /var/lib/aqla --shell /usr/sbin/nologin aqla
   sudo chown -R aqla:aqla /opt/aqla
   sudo chmod 700 /opt/aqla/secrets
   sudo chmod 600 /opt/aqla/vps.env /opt/aqla/secrets/*
   sudo install -m 644 /opt/aqla/aqla-agent.service /etc/systemd/system/aqla-agent.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now aqla-agent
   sudo journalctl -u aqla-agent -f
   ```

   Ein schon vorhandenes Konto nicht erneut anlegen. Die Unit verwendet `User=aqla`, restriktive Dateirechte und Neustart bei Fehlern. `KillMode=mixed` und 90 Sekunden Stop-Frist lassen den Gateway seine MCP-Kinder geordnet beenden. Kein Webserver und kein eingehender öffentlicher Port werden benötigt.

6. Dem Bot einmal eine normale Nachricht senden, beispielsweise „Hallo“. Regeln und Quellen über Hermes festlegen. Neue zulässige Signale benötigen anschließend keine Freigabe pro Trade.

## Bedienung

Profile sind wiederverwendbare Handelsparameter; Pipelines verbinden abonnierte Quellen mit Börse und Profil. Beispiel: „Erstelle Profil A mit meinen bisherigen Handelsparametern und Profil B mit 0,25 Prozent Risiko. Nutze Quelle 1 auf Hyperliquid mit A und auf Aster mit B.“ Hermes liest vorher die aktuellen Profile und erhält ungenannte Werte. Kontolimits werden gemeinsam je Börse gesetzt, etwa „Auf Aster höchstens zwei offene Trades und ein Prozent Gesamtrisiko“. Aster und Lighter sind für Mainnet vorbereitet, bleiben aber bis zu eingerichteten API-Zugängen, expliziten Limits und deinem Aktivierungsauftrag deaktiviert.

Eine explizite aktive Pipeline ersetzt für ihre Quelle die migrierte Hyperliquid-Standardroute; andere Quellen behalten ihr bisheriges Verhalten. Ein Signal wird einmal interpretiert und erzeugt je Ziel einen eigenen unveränderlichen Plan. Fehler stoppen nur das betroffene Ziel. Prozentwerte beziehen sich auf das Konto der jeweiligen Börse; feste Beträge `_quote` werden in der Abrechnungswährung des jeweiligen Markts gerechnet. USDT, USDC und verschiedene Handelsnetzwerke werden in der Auswertung getrennt. Pro Börse und Markt ist ein eigener Trade gleichzeitig erlaubt. Ohne bestätigten SL versucht Aqla, die eigene Position zu schließen, pausiert weitere Entries dieser Börse und meldet den Vorfall.

Die bestehenden Hyperliquid-Werkzeuge und `set_policy` bleiben kompatibel; `set_policy` pflegt Profil „Standard“ und die Hyperliquid-Kontolimits. Neue Steueraktionen heißen `set_profile`, `set_pipeline`, `delete_profile`, `delete_pipeline`, `set_exchange_limits` und `set_exchange_enabled`; sie verwenden `configuration_revision`. `trade_preview` liefert `targets` mit je eigener `plan_id`, `trade_execute` akzeptiert alternativ mehrere `plan_ids`. Quellenupdates werden auf die entstandenen Trades derselben Setupfamilie angewendet. Profiländerungen gelten für neue Trades. Native Skills benötigen weiterhin deine Freigabe vor einer Übernahme.

- „Lies auch `@mein_signal_channel`.“
- „Risiko pro Trade 0,5 Prozent, Hebel 2, maximal drei offene Trades.“
- „Übernimm den Hebel 1:1 aus dem Signal, gedeckelt am Börsenmaximum des jeweiligen Markts.“
- „Bei fehlendem Entry zum Marktpreis; maximal 0,3 Prozent Entry-Slippage.“
- „Verteile auf alle TPs gleichmäßig. Nach vollständig gefülltem TP1 den Stop auf Einstand.“
- „Adaptive TP-Verteilung und TP-Stop-Leiter: maximal fünf Ziele; nach TP1 Einstand, nach TP3 TP1, nach TP4 TP2.“ Siehe [adaptive TPs](docs/adaptive-tps.md).
- „100 Prozent an TP1.“
- „Pausiere neue Trades.“ / „Setze neue Trades fort.“
- „Schließe den SOL-Trade.“
- „Passe den Master-Skill so an, dass Replies deutlicher berücksichtigt werden.“

Hermes übersetzt diese Wünsche in validierte MCP-Aufträge und nennt die wirksamen Werte. Die Erstkonfiguration enthält keine automatisch ausgewählten Handelsregeln. Nicht genannte Werte bestehender Regeln sollen erhalten bleiben; unklare Erstvorgaben werden geklärt.

Hermes darf `aqla-master` selbst verbessern und erweitern, speichert Änderungen aber immer zuerst als Entwurf. Es fragt dich im Telegram-Chat vor der Übernahme: `/skills diff <id>` zeigt die Änderung, `/skills approve <id>` übernimmt sie, `/skills reject <id>` verwirft sie. Ohne Zustimmung bleibt der aktive Skill unverändert. Das gilt auch für ausdrücklich angeforderte Skilländerungen. Es gibt höchstens einen offenen Entwurf; andere Skills, zusätzliche Dateien und Änderungen über 8 KiB sind gesperrt. Bei zwischenzeitlich verändertem Master muss ein neuer Entwurf erstellt werden. Diese Freigabe ist im Aqla-Profil verpflichtend.

Ein neuer Trade braucht Markt, Richtung, SL und mindestens einen TP. Zusammengehörige Nachrichten/Replies können die Angaben vervollständigen. Updates wie „SL auf Einstand“ benötigen diese Felder nicht erneut. Berichte, mehrdeutige Zuordnungen und nicht auflösbare Märkte erzeugen keinen neuen Trade. Zahlen werden anhand der echten Quelltexte belegt; keine Pflichtregex je Kanal. V1 liest Text und Captions; reine Bildsignale werden nicht als erkannt ausgegeben.

Der automatische Signalhandel verwendet Perpetuals mit nativem Reduce-only-Schutz. Spot-Funktionen sind im vollständigen MCP verfügbar; die automatische Signalstrecke verweigert Spot, bevor eine Order entsteht.

Der Hebel ist wählbar: `leverage_mode: fixed` verwendet weiterhin den festen Nutzerwert `leverage`; ein Wert über dem Marktmaximum wird abgelehnt. `leverage_mode: signal` übernimmt den mit Originalzitat belegten Signalhebel und deckelt ihn ausschließlich am Marktmaximum aus der Hyperliquid-Metadatenabfrage. Ein Signal mit 50× wird bei einem Marktlimit von 20× mit 20× geplant. Ohne eindeutigen positiven ganzzahligen Signalhebel entsteht in diesem Modus kein neuer Trade; der feste Wert dient nicht als Ersatz. Der Plan speichert `signal_leverage` und `effective_leverage`, und derselbe effektive Wert gilt für Marginprüfung, Börsenauftrag und Wiederanlauf. Die Risiko- und Positionswertgrenzen bleiben wirksam.

## MCP und Zustand

Ein zweiter eigener Python-MCP führt das [Google-Sheets-Journal](docs/google-sheets.md): Signale, Trade-Updates, Orders, Fills, Gebühren, Ergebnisse, Regeln, einzeln lesbare Handelsparameter und Statushistorie werden automatisch übertragen. Hermes kann die Tabelle lesen und nach Quelle, Symbol oder Richtung auswerten. `state.sqlite3` hält die sichere Ausführung und noch ausstehende Übertragungen; eine zusätzliche Analysedatei entfällt. Tabellen-ID und Servicekonto werden einmalig auf dem VPS gesetzt.

```sh
uv run aqla-hyperliquid-mcp --config config.yaml
uv run aqla-hyperliquid-mcp --config config.yaml --profile full
uv run aqla-hyperliquid-mcp --check-coverage
uv run --extra sheets aqla-google-sheets-mcp --config config.yaml
```

Das Agentenprofil enthält nur den regelgeprüften Handelspfad. Das vollständige Profil bietet die typisierten öffentlichen HyperCore-Info-/Exchange-/WebSocket-Verträge, einschließlich Spot, HIP-3/HIP-4, Transfers, Kontofunktionen, Deployeraktionen und Multisig. Die [Abdeckungsmatrix](docs/api-coverage.md) dokumentiert Handler, Prüfdateien und Quellen. Rollen- und Testnet-Grenzen werden ausdrücklich geprüft; ein API-Wallet wird nicht als Masterwallet ausgegeben. HyperEVM-Contract-/Validatorbetrieb ist kein Teil dieses Exchange-MCP.

`data/state.sqlite3` enthält Regeln, Quellen, Cursor, Bindungen, eigene Trades, Fills, Operationsidentitäten und Nonces. Transaktionen und genau ein Schreibprozess sichern den Zustand. Vorhandene JSON-Dateien werden einmalig importiert und als Sicherung erhalten. Eine beschädigte oder nach Initialisierung fehlende Datenbank blockiert die Ausführung. Vor einem Exchange-Aufruf wird die Absicht dauerhaft gespeichert. Ein unklarer Ausgang wird anhand exakter Orderidentitäten abgeglichen; nie automatisch unter einer neuen ID erneut gesendet. Entry-Pause unterbricht Schutz und Exits nicht. Gefüllte TP-Budgets werden nicht aufgefüllt, und ein alter Stop wird erst nach bestätigtem Ersatzschutz entfernt. Widersprüchliche Positions-/Fill-Abfragen halten den bisherigen Schutz; Abweichungen werden nicht als bewiesene manuelle Reduktion verbucht. Nach einem Exit bleibt der Markt reserviert, bis alte eigene Orders nachweislich beendet sind.

Der State bindet Hauptkonto, effektives Handelskonto/Vault und Netzwerk dauerhaft. Für ein anderes Zielkonto einen neuen separaten State verwenden. Pause, Regeln, Quellenfreigabe und Nachrichtenrevision werden unmittelbar vor einem noch ungesendeten Entry erneut geprüft. Das Entfernen einer Quelle stoppt ihre neuen Entries; bestehende Trades werden weiter geschützt. Gespeicherte Close-/Reduktionsabsichten behalten ihre exakte Aktion über einen Neustart hinweg.

Eine Absage eines nachweislich noch ungesendeten Entries bleibt nach Neustart wirksam. Neue Handelsaktionen brauchen die aktuelle auslösende Live-Nachricht; historische Angaben können sie ergänzen. Die frische Kontoantwort muss Positionsfreiheit bestätigen. Für automatische HIP-3-Trades muss das Collateral nachweislich USDC sein. Der vollständige MCP bleibt für andere Collateralverträge verfügbar. Seine Kontoaktionen unterscheiden Principal- und Vault-Scope ausdrücklich; uneindeutige alte Principal-Vault-Vorgänge werden konservativ abgewiesen.

Hermes speichert Gespräche separat in `hermes-home/state.db` mit Volltextsuche; frühere JSONL-Verläufe werden importiert. Native Rückfragen, Aufgabenlisten und Gesprächssuche unterstützen Betreiberaufträge. Aktuelle Handelsregeln stammen weiterhin aus `status`. Native Nachrichtenübertragung, Befehle und Agentenlauf bleiben bei Hermes. Details: [Hermes-Werkzeuge und SQLite](docs/hermes-tools-and-storage.md).

Für einen vollständigen Snapshot den Dienst anhalten und den **gesamten Zielordner inklusive Secrets und State** geschützt sichern. Nach Wiederherstellung zuerst Konto, offene Orders, Fills und unklare Operationen abgleichen. Einen alten State nicht zum Freischalten einer unklaren Order zurückspielen. Ein dediziertes API-Wallet je Instanz verwenden.

## Prüfen

Die [GitHub-Qualitätspipeline](https://github.com/888nikush888/aqla-agent/actions/workflows/quality.yml) läuft bei Push, Pull Request und manuellem Start. Sie prüft Python 3.12/3.13/3.14, Ruff, Tests mit Branch-Coverage, unabhängige HTTP-Vertragsfälle für alle 59 Info-Typen, lokale WebSocket-Recovery, das gepinnte native Hermes und die Git-Historie auf Secrets. Abhängigkeitsscans erfassen sowohl die eingefrorene Aqla-Auflösung als auch die tatsächlich installierte separate Hermes-Laufzeit. Installer und CI verwenden dieselbe minimale Telegram-/MCP-Installationsvorgabe mit geprüften Versionskorrekturen aus `hermes-runtime-overrides.txt`. Erst danach werden Wheel, Source-Distribution und VPS-ZIP gebaut, geprüft und als CI-Artefakt gespeichert; der README-Lintcheck läuft auch auf dem entpackten ZIP. Der gemeinsame Check `Quality gate` ist für einen Branch-Schutz geeignet. Die CI benötigt keine Telegram-, Modell-, Google- oder Exchange-Zugangsdaten. Befunde und Prüfgrenzen stehen in [docs/quality-review.md](docs/quality-review.md).

```sh
uv sync --frozen --all-extras
uv run --no-sync ruff check src tests hermes_plugin scripts
uv run --no-sync pytest -q
uv run --no-sync aqla-hyperliquid-mcp --check-coverage
```

Zusätzlich existieren echte Native-Hermes-Prüfprogramme für den gepinnten Checkout:

```sh
AQLA_SOURCE=/opt/aqla-source
AQLA_DEPLOY=/opt/aqla
"$AQLA_DEPLOY/hermes-venv/bin/python" "$AQLA_SOURCE/tests/native_hermes_smoke.py" --hermes-source "$AQLA_DEPLOY/hermes-source" --home /tmp/aqla-session-qa
"$AQLA_DEPLOY/hermes-venv/bin/python" "$AQLA_SOURCE/tests/native_pipeline_smoke.py" --hermes-source "$AQLA_DEPLOY/hermes-source" --destination /tmp/aqla-source-qa --mcp-python "$AQLA_DEPLOY/.venv/bin/python"
"$AQLA_DEPLOY/hermes-venv/bin/python" "$AQLA_SOURCE/tests/native_pipeline_smoke.py" --hermes-source "$AQLA_DEPLOY/hermes-source" --destination /tmp/aqla-owner-qa --mcp-python "$AQLA_DEPLOY/.venv/bin/python" --mode owner
"$AQLA_DEPLOY/hermes-venv/bin/python" "$AQLA_SOURCE/tests/native_pipeline_smoke.py" --hermes-source "$AQLA_DEPLOY/hermes-source" --destination /tmp/aqla-lifecycle-qa --mcp-python "$AQLA_DEPLOY/.venv/bin/python" --mode lifecycle
```

Der Pipeline-Test verwendet einen lokalen geskripteten Modellserver. Er beweist native Agenten-/Hook-/stdio-Anbindung und verweigerte Quellenbefugnisse. Der Lifecycle-Modus prüft zusätzlich rekursive Quellen-/Owner-/Wake-Folgeturns mit wiederverwendetem Agenten, Kontextkopie in Worker, Rechtewiderruf, stop/new/reset während eines laufenden Turns und Adapter-Reconnect. Er ist kein Nachweis der Erkennungsqualität eines echten Modells. Es wurden keine realen Telegram-Nachrichten oder Hyperliquid-Mutationen zur Entwicklung ausgeführt. Native Stop-/TP-Ausführung und mögliche Markt-/Teilfill-Effekte müssen im gewünschten Konto/Betrieb separat geprüft werden; berechnetes SL-Risiko ist keine garantierte maximale tatsächliche Ausführungsverlusthöhe.

30 Kontextfälle für neue Signale, Replies, Updates, Berichte, unvollständige Angaben und Quellenanweisungen liegen in `tests/fixtures/context_cases.json`. Für eine ausdrückliche Prüfung deines echten Modells:

```sh
"$AQLA_DEPLOY/hermes-venv/bin/python" "$AQLA_SOURCE/tests/evaluate_context.py" --hermes-source "$AQLA_DEPLOY/hermes-source" --home /tmp/aqla-model-eval --model DEIN_MODELL --model-url DEINE_API_URL
```

`HERMES_MODEL_API_KEY` muss dafür gesetzt sein. Diese optionale Prüfung verursacht Modellaufrufe, schreibt ihren Bericht im neuen Eval-Ordner und besitzt keine Handels- oder Telegram-Werkzeuge. Sie wurde ohne deine Zugangsdaten nicht ausgeführt.

Das auslieferbare Quellpaket entsteht mit `uv run aqla-build-vps --output dist/aqla-agent-vps.zip`; die zugehörige SHA-256-Datei wird mitgeschrieben. Die gemeinsame Pfadregel für Repository, ZIP, Wheel und Source-Distribution verbietet bekannte lokale Secret- und Statepfade einschließlich `.env`-Varianten (außer `.env.example`), Datenbanken, SQLite-Sidecars, Sessions und Initialisierungsmarker. Der Build prüft alle erlaubten Quellen vor dem Öffnen des Archivs und lehnt Symlinks, Junctions und andere Reparse-Punkte ab. ZIP und SHA dürfen außerhalb der ausgelieferten Quellverzeichnisse liegen; vorhandene unvollständige oder ungültige Paare werden unverändert abgelehnt. Dies ist eine Pfadgrenze, kein Scanner für vertrauliche Inhalte in sonst erlaubten Dateien.

Die beiden Dateien werden zunächst privat im Zielordner geschrieben, geschlossen, synchronisiert und auf Mitglieder, ZIP-Integrität und SHA-256 geprüft. Ein vorhandenes gültiges Paar wird vor den beiden Ersetzungen als Recovery-Kopie gesichert. Bei behandelbaren Veröffentlichungsfehlern stellt der Build das alte Paar wieder her oder entfernt die selbst erzeugten Ziele; schlägt auch die Wiederherstellung fehl, bleibt die Recovery-Kopie am gemeldeten Pfad erhalten und der Build meldet keinen Erfolg. Die zwei Ersetzungen bilden keine atomare Paartransaktion: Prozessabbruch, Stromausfall oder ein Leser zwischen den Ersetzungen können ein gemischtes Paar beobachten. Nach einem solchen Abbruch können Recovery-Verzeichnisse neben dem Ziel verbleiben. `python scripts/verify_artifacts.py` lehnt einen abweichenden ZIP/SHA-Stand ab; die Prüfung ist vor Verwendung oder Upload erforderlich.
