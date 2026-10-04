# aqla Agent

[![Quality](https://github.com/888nikush888/aqla-agent/actions/workflows/quality.yml/badge.svg)](https://github.com/888nikush888/aqla-agent/actions/workflows/quality.yml)

Telegram-Quellen → Hermes versteht Signal/Update im Kontext → Python prüft deine Regeln → Hyperliquid führt aus → Hermes meldet das Ergebnis in seinem Telegram-Bot.

Eigenständiges Python-Projekt. Ein eigener Hyperliquid-MCP, ein kurzer bearbeitbarer Master-Skill und ein read-only Telegram-Quellenleser für abonnierte Kanäle. Keine eigene Bot-Anbindung, MiniApp, Datenbank, Kanalbewertungen oder autonome Strategieoptimierung. Manuell an den Hermes-Bot weitergeleitete Signale werden ebenfalls als Quellen erfasst.

## Unabhängig auf einem VPS installieren

Voraussetzungen: Linux mit systemd, Git und uv. Der Installer richtet eigene Python-Laufzeiten im Zielordner ein: 3.12 für Aqla, 3.14 für den exakt gepinnten Hermes-Stand. Eine vorhandene Hermes-Installation wird nicht angebunden oder verändert.

1. Quellpaket in einen separaten Ordner entpacken, beispielsweise `/opt/aqla-source`. Im Quellordner:

   ```sh
   uv sync --frozen --extra sources --extra sheets
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

- „Lies auch `@mein_signal_channel`.“
- „Risiko pro Trade 0,5 Prozent, Hebel 2, maximal drei offene Trades.“
- „Bei fehlendem Entry zum Marktpreis; maximal 0,3 Prozent Entry-Slippage.“
- „Verteile auf alle TPs gleichmäßig. Nach vollständig gefülltem TP1 den Stop auf Einstand.“
- „Pausiere neue Trades.“ / „Setze neue Trades fort.“
- „Schließe den SOL-Trade.“
- „Passe den Master-Skill so an, dass Replies deutlicher berücksichtigt werden.“

Hermes übersetzt diese Wünsche in validierte MCP-Aufträge und nennt die wirksamen Werte. Die Erstkonfiguration enthält keine automatisch ausgewählten Handelsregeln. Nicht genannte Werte bestehender Regeln sollen erhalten bleiben; unklare Erstvorgaben werden geklärt.

Ein neuer Trade braucht Markt, Richtung, SL und mindestens einen TP. Zusammengehörige Nachrichten/Replies können die Angaben vervollständigen. Updates wie „SL auf Einstand“ benötigen diese Felder nicht erneut. Berichte, mehrdeutige Zuordnungen und nicht auflösbare Märkte erzeugen keinen neuen Trade. Zahlen werden anhand der echten Quelltexte belegt; keine Pflichtregex je Kanal. V1 liest Text und Captions; reine Bildsignale werden nicht als erkannt ausgegeben.

Der automatische Signalhandel verwendet Perpetuals mit nativem Reduce-only-Schutz. Spot-Funktionen sind im vollständigen MCP verfügbar; die automatische Signalstrecke verweigert Spot, bevor eine Order entsteht.

## MCP und Zustand

Ein zweiter eigener Python-MCP führt das [Google-Sheets-Journal](docs/google-sheets.md): Signale, Trade-Updates, Orders, Fills, Gebühren, Ergebnisse, Regeln, einzeln lesbare Handelsparameter und Statushistorie werden automatisch übertragen. Hermes kann die Tabelle lesen und nach Quelle, Symbol oder Richtung auswerten. `state.json` bleibt für sichere Ausführung und noch ausstehende Übertragungen erhalten; eine zusätzliche Analysedatei entfällt. Tabellen-ID und Servicekonto werden einmalig auf dem VPS gesetzt.

```sh
uv run aqla-hyperliquid-mcp --config config.yaml
uv run aqla-hyperliquid-mcp --config config.yaml --profile full
uv run aqla-hyperliquid-mcp --check-coverage
uv run --extra sheets aqla-google-sheets-mcp --config config.yaml
```

Das Agentenprofil enthält nur den regelgeprüften Handelspfad. Das vollständige Profil bietet die typisierten öffentlichen HyperCore-Info-/Exchange-/WebSocket-Verträge, einschließlich Spot, HIP-3/HIP-4, Transfers, Kontofunktionen, Deployeraktionen und Multisig. Die [Abdeckungsmatrix](docs/api-coverage.md) dokumentiert Handler, Prüfdateien und Quellen. Rollen- und Testnet-Grenzen werden ausdrücklich geprüft; ein API-Wallet wird nicht als Masterwallet ausgegeben. HyperEVM-Contract-/Validatorbetrieb ist kein Teil dieses Exchange-MCP.

`data/state.json` enthält Regeln, Quellen, Cursor, Bindungen, eigene Trades, Fills, Operationsidentitäten und Nonces. Nur ein Prozess darf schreiben. Vor einem Exchange-Aufruf wird die Absicht dauerhaft gespeichert. Ein unklarer Ausgang wird anhand exakter Orderidentitäten abgeglichen; nie automatisch unter einer neuen ID erneut gesendet. Entry-Pause unterbricht Schutz und Exits nicht. Gefüllte TP-Budgets werden nicht aufgefüllt, und ein alter Stop wird erst nach bestätigtem Ersatzschutz entfernt. Widersprüchliche Positions-/Fill-Abfragen halten den bisherigen Schutz; Abweichungen werden nicht als bewiesene manuelle Reduktion verbucht. Nach einem Exit bleibt der Markt reserviert, bis alte eigene Orders nachweislich beendet sind.

Der State bindet Hauptkonto, effektives Handelskonto/Vault und Netzwerk dauerhaft. Für ein anderes Zielkonto einen neuen separaten State verwenden. Pause, Regeln, Quellenfreigabe und Nachrichtenrevision werden unmittelbar vor einem noch ungesendeten Entry erneut geprüft. Das Entfernen einer Quelle stoppt ihre neuen Entries; bestehende Trades werden weiter geschützt. Gespeicherte Close-/Reduktionsabsichten behalten ihre exakte Aktion über einen Neustart hinweg.

Eine Absage eines nachweislich noch ungesendeten Entries bleibt nach Neustart wirksam. Neue Handelsaktionen brauchen die aktuelle auslösende Live-Nachricht; historische Angaben können sie ergänzen. Die frische Kontoantwort muss Positionsfreiheit bestätigen. Für automatische HIP-3-Trades muss das Collateral nachweislich USDC sein. Der vollständige MCP bleibt für andere Collateralverträge verfügbar. Seine Kontoaktionen unterscheiden Principal- und Vault-Scope ausdrücklich; uneindeutige alte Principal-Vault-Vorgänge werden konservativ abgewiesen.

Der native Hermes-Gateway wird im separaten VPS-Prozess auf Dateiverlauf eingestellt; SQLite-Zugriffe sind in diesem Profil deaktiviert. Native Nachrichtenübertragung, Befehle und Agentenlauf bleiben bei Hermes. Kleine lokale JSON/JSONL-Dateien ersetzen keine Börsenbelege.

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

Das auslieferbare Quellpaket entsteht mit `uv run aqla-build-vps --output dist/aqla-agent-vps.zip`; die zugehörige SHA-256-Datei wird mitgeschrieben. Es enthält keine lokalen Umgebungen, Kontozustände oder Zugangsdaten.
