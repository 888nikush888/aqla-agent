# Installation

Voraussetzungen: Linux-VPS, systemd, Git und uv. Das Paket installiert Aqla mit Python 3.12 sowie den gepinnten nativen Hermes mit eigener Python-3.14-Laufzeit. Vorhandene fremde Hermes-Installationen werden nicht verändert.

```sh
uv sync --frozen --extra sources
uv run aqla-install-hermes --destination /opt/aqla --owner-id DEINE_TELEGRAM_USER_ID
```

Das Ziel muss neu oder leer sein. `--prepare-only` erzeugt die Installation auch unter Windows, ohne Linux-Dienste zu starten.

In `/opt/aqla/vps.env` werden `TELEGRAM_BOT_TOKEN` und `HERMES_MODEL_API_KEY` gesetzt. Standardmodell ist DeepSeek V4.1 Flash über OpenCode Go mit Reasoning high und Caveman; Fallback-Anbieter bleiben leer. Schlüssel gehören ausschließlich in geschützte Serverdateien.

In `config.yaml`: tatsächliches Hyperliquid-Konto, gewünschtes Netzwerk, API-Wallet-Rolle und Signierschlüsseldatei konfigurieren. Bei API-Wallets bezeichnet `account` das Hauptkonto. Ein zusätzlicher Mainnet-/Testnet-Kontext hat seine eigene Kontobindung und geschützte Zugangsdaten. Limits und Aktivierung werden ausdrücklich freigegeben.

Für abonnierte Quellen werden Telegram API-ID, API-Hash-Datei und Session-Datei konfiguriert. Danach:

```sh
.venv/bin/aqla-telegram-login --config config.yaml
```

Der Quellenleser verwendet das Telegram-Konto ausschließlich lesend. Er tritt keinen Kanälen bei und schreibt keine Nachrichten. Der native Hermes-Bot übernimmt Antworten und Menü.

Ein Dienstkonto `aqla` verwenden, das Installationsverzeichnis diesem Konto zuordnen und `secrets/` auf 700 sowie Zugangsdaten auf 600 beschränken. Die erzeugte `aqla-agent.service` nach `/etc/systemd/system/` installieren, `systemctl daemon-reload` und `systemctl enable --now aqla-agent` ausführen. Der Dienst benötigt keinen eingehenden öffentlichen Port.

Dem Bot `/aqla` senden und Profile, Quellen und Limits konfigurieren. Setup schützt Änderungen. Neue automatische Entries benötigen danach keine Einzelfreigabe, solange alle bestehenden Prüfungen bestanden sind.

Aktualisierungen bestehender Installationen folgen [Betrieb und Migration](operations.md), nicht einer Neuinstallation über vorhandene Daten.
