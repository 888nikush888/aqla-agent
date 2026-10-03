# VPS-Betriebsabnahme

Stand: 03.10.2026. Tatsächliche Installation auf Debian 13 x86_64, getrennt unter `/opt/aqla`; Quellpaket unter `/opt/aqla-source`. Vorher vorhandene ungenutzte Docker-/Containerd-Pakete und Caches wurden auf ausdrücklichen Nutzerauftrag entfernt. Debian, SSH, Netzwerk und Provider-Systemdienste bleiben erhalten.

## Nachgewiesen

- Release-ZIP per verschlüsseltem SFTP übertragen, SHA-256 vor Extraktion verglichen.
- Aqla Python 3.12.14, separat gepinntes Hermes mit Python 3.14.7 und geprüften Dependency-Overrides installiert.
- Dienstkonto `aqla`, systemd-Autostart, geheime Dateien mit Modus 0600, privates Hermes-Home und Secretverzeichnis.
- Tatsächlicher Modellaufruf über den nativen Hermes-AIAgent mit `glm-5.3-flash` über OpenCode Go erfolgreich. Provider-/Sessionrouting wird vom gepinnten Hermes unterstützt; [Provider-Endpunkte](https://opencode.ai/docs/go/).
- Telegram-Bot authentifiziert und in Pollingmodus verbunden. Nutzer bestätigt tatsächliche Antwort auf Systemstatus-Anfrage.
- Telegram-Kanalleser über QR authentifiziert; angemeldete Nutzer-ID entspricht dem konfigurierten Betreiber. StringSession als geschützte Datei, keine SQLite-Sitzung.
- Hyperliquid-API-Wallet an das konfigurierte **Testnet**-Hauptkonto gebunden; Konto und Marktmetadaten lesbar, Standardkonto-Abstraktion unterstützt.
- Keine Exchange-Mutationen während Einrichtung. Eine bereits im Konto vorhandene Position wurde nicht als eigener Aqla-Trade importiert oder verändert.
- Dienst läuft; keine Datenbankdateien in Aqla-Daten oder Hermes-Home, öffentlich lauscht nur der vorhandene SSH-Port.

Beim Live-Start erkannte ungenutzte native Cron-/Housekeeping-Factory wurde im Aqla-Profil deaktiviert und als nativer Regressioncheck abgesichert. systemd Stop-Frist beträgt 90 Sekunden, `KillMode=mixed` erhält MCP-Kinder bis zur geordneten Gatewayabschaltung. Native Lazy-Installationen ändern die vom Installer geprüfte Laufzeit nicht mehr.

## Bewusst noch zu konfigurieren

Handelsregeln und Quellen wählt der Nutzer über den Bot; sie wurden nicht automatisch festgelegt. Neue Trades bleiben bis zu gültiger Policy und zulässiger Quelle beziehungsweise Betreiber-Weiterleitung gesperrt. Bereits vorhandene fremde Positionen unterliegen weiterhin dem Entry-Besitzguard.

Google Sheets wird auf ausdrückliche Nutzervorgabe zuletzt eingerichtet; während der bisherigen Abnahme ist es deaktiviert. Tabellen-ID und Servicekonto-Zugang werden ausschließlich geschützt auf dem VPS hinterlegt. Dieser Bericht enthält keine Host-/Login-/Wallet-/Bot-/Modellschlüssel oder Sessionwerte.
