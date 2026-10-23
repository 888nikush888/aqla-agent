# Aqla

[![Quality](https://github.com/888nikush888/aqla-agent/actions/workflows/quality.yml/badge.svg)](https://github.com/888nikush888/aqla-agent/actions/workflows/quality.yml)

Aqla verarbeitet Telegram-Signale mit Hermes, prüft Profile und Risiken in Python und verwaltet eigene Positionen auf Hyperliquid Mainnet und Testnet. Das private Bot-Menü **/aqla** bündelt Dashboard, Positionen, lokales Journal, Profile, Quellen, Konten und Systemsteuerung.

Die Auslieferung installiert ihre eigenen gepinnten Hermes- und Python-Laufzeiten. Es gibt einen nativen Telegram-Transport und einen kontrollierten Handels-Writer. Zugangsdaten, lokale Handelsdaten und private Telegram-Exporte gehören nicht ins Quellpaket.

## Einstieg

Linux-VPS mit systemd, Git und uv; im entpackten Quellpaket:

```sh
uv sync --frozen --extra sources
uv run aqla-install-hermes --destination /opt/aqla --owner-id DEINE_TELEGRAM_USER_ID
```

Danach Konto, geschützte Zugangsdaten und Quellenleser konfigurieren und den generierten Dienst installieren: [Installation](docs/installation.md). Das Zielverzeichnis muss neu oder leer sein. Eine bestehende Installation wird über den kontrollierten Migrationsweg aktualisiert.

## Bedienung

- **/aqla** öffnet eine aktualisierbare Nachricht mit allen Untermenüs.
- **/aqla_pause** pausiert neue Entries sofort; Positionsschutz läuft weiter.
- **/setup** öffnet die private, zeitlich begrenzte Freigabe für geschützte Änderungen.
- Freie Fragen und ausdrücklich weitergeleitete Signale bleiben über Hermes möglich.

Gültige Einstellwerte werden sofort gespeichert. Bestehende Trades behalten ihre gespeicherten Pläne; gezielte Verwaltungsaufträge erfolgen in ihrer Positionsansicht. Es gibt keinen Menüdialog für neue manuelle Entries. Details: [Telegram-Menü](docs/telegram.md), [Handelsregeln](docs/trading.md).

Automatische Meldungen beschränken sich auf wichtige Ausführungen, Abschlüsse, belegte Waverider-Wellen und relevante Störungen. Routineabgleiche und Ablehnungen stehen im Verlauf. Der lokale Monatsbericht erscheint am Folgemonatsersten um 09:00 Uhr Berlin.

## Daten und Betrieb

SQLite hält die tatsächlichen Trades, Orderidentitäten, Fills, Gebühren und Fundingbelege. Dashboard und Journal lesen diese Belege direkt. Fehlende Werte sind unbekannt, niemals erfundene Nullwerte. Mainnet/Testnet und verschiedene Währungen werden getrennt ausgewertet.

Das System benötigt keinen Tabellenanbieter und keinen Webserver. Frühere Börsenpositionen bleiben als Historie lesbar; aktive Ausführung verwendet ausschließlich Hyperliquid. Manuelle Positionen werden nicht als Aqla-Eigentum verwaltet. [Betrieb und Migration](docs/operations.md).

Standard ist DeepSeek V4.1 Flash über OpenCode Go mit `high` und Caveman. Weitere Modell-/Reasoning-Kombinationen benötigen einen passenden Qualitätsnachweis; es gibt keinen OpenRouter- oder `max`-Fallback.

## Entwicklung und Qualität

```sh
uv sync --frozen --all-extras
uv run ruff check src tests hermes_plugin scripts
uv run pytest -q
uv run aqla-hyperliquid-mcp --check-coverage
uv run aqla-build-vps --output dist/aqla-agent-vps.zip
```

Die CI prüft die unterstützten Python-Versionen, native Hermes-Integration, API-Verträge, Secret- und Abhängigkeitssicherheit sowie die getrennten Laufzeit- und Quellpakete. Ausführungstests verwenden isolierte Zustände und Fake-Börsen. [Qualitätsgrenzen](docs/quality.md), [historischer High-Nachweis](docs/high-tools-qualification.md), [API-Abdeckung](docs/api-coverage.md).
