# VPS-Betriebsabnahme

Stand: 03.10.2026. Tatsächliche Installation auf Debian 13 x86_64, getrennt unter `/opt/aqla`; Quellpaket unter `/opt/aqla-source`. Vorher vorhandene ungenutzte Docker-/Containerd-Pakete und Caches wurden auf ausdrücklichen Nutzerauftrag entfernt. Debian, SSH, Netzwerk und Provider-Systemdienste bleiben erhalten.

## Nachgewiesen

- Release-ZIP per verschlüsseltem SFTP übertragen, SHA-256 vor Extraktion verglichen.
- Aqla Python 3.12.14, separat gepinntes Hermes mit Python 3.14.7 und geprüften Dependency-Overrides installiert.
- Dienstkonto `aqla`, systemd-Autostart, geheime Dateien mit Modus 0600, privates Hermes-Home und Secretverzeichnis.
- Aktives Modell auf Nutzerwunsch: `deepseek-v4.1-flash` über OpenCode Go, Hermes `agent.reasoning_effort: ultra`. Hermes übersetzt dies in DeepSeeks höchste API-Stufe `max`; tatsächlicher nativer AIAgent-Aufruf aus der gespeicherten Konfiguration mit `reasoning_effort=max`, zurückgeliefertem Reasoning und erfolgreicher Antwort geprüft. Provider-/Sessionrouting wird vom gepinnten Hermes unterstützt; [Provider-Endpunkte](https://opencode.ai/docs/go/), [DeepSeek-Modellparameter](https://api-docs.deepseek.com/api/list-models/).
- Telegram-Bot authentifiziert und in Pollingmodus verbunden. Nutzer bestätigt tatsächliche Antwort auf Systemstatus-Anfrage.
- Telegram-Kanalleser über QR authentifiziert; angemeldete Nutzer-ID entspricht dem konfigurierten Betreiber. StringSession als geschützte Datei, keine SQLite-Sitzung.
- Hyperliquid-API-Wallet an das konfigurierte **Testnet**-Hauptkonto gebunden; Konto und Marktmetadaten lesbar, Standardkonto-Abstraktion unterstützt.
- Keine Exchange-Mutationen während Einrichtung. Eine bereits im Konto vorhandene Position wurde nicht als eigener Aqla-Trade importiert oder verändert.
- Dienst läuft; keine Datenbankdateien in Aqla-Daten oder Hermes-Home, öffentlich lauscht nur der vorhandene SSH-Port.

Beim Live-Start erkannte ungenutzte native Cron-/Housekeeping-Factory wurde im Aqla-Profil deaktiviert und als nativer Regressioncheck abgesichert. systemd Stop-Frist beträgt 90 Sekunden, `KillMode=mixed` erhält MCP-Kinder bis zur geordneten Gatewayabschaltung. Native Lazy-Installationen ändern die vom Installer geprüfte Laufzeit nicht mehr.

## Bewusst noch zu konfigurieren

Handelsregeln und Quellen wählt der Nutzer über den Bot; sie wurden nicht automatisch festgelegt. Neue Trades bleiben bis zu gültiger Policy und zulässiger Quelle beziehungsweise Betreiber-Weiterleitung gesperrt. Bereits vorhandene fremde Positionen unterliegen weiterhin dem Entry-Besitzguard.

Google Sheets wurde anschließend im geöffneten Nutzerbrowser eingerichtet und aktiviert: eigenes Dienstkonto ohne zusätzliche Projektrollen, auf die Journaltabelle beschränkte Bearbeiterfreigabe und geschützter Servicekonto-Schlüssel auf dem VPS. Der private RSA-Schlüssel wurde lokal erzeugt; ausschließlich sein öffentliches X509-Zertifikat wurde in Google Cloud registriert. Der ungenutzte Schlüssel aus dem nicht verfügbaren Browserdownload wurde widerrufen.

Tatsächliche Abnahme: sieben Journal-Tabs vorhanden, eine verifizierte aktuelle Regelzeile übertragen, `journal_pending=0`, letzter Sync bestätigt und keine Exchange-Mutationen. [Journaltabelle](https://docs.google.com/spreadsheets/d/1hLVjDSZJ_m5h6EDCubjzCksme7xfR9H7iwwVMh09JdQ/edit). Der Dienst bleibt aktiv; Tabellen-ID und Schlüssel liegen geschützt auf dem VPS. Dieser Bericht enthält keine Host-/Login-/Wallet-/Bot-/Modellschlüssel oder Sessionwerte.

Anschließend auf Nutzerwunsch ergänzt: [aqla Handelsparameter](https://docs.google.com/spreadsheets/d/1hLVjDSZJ_m5h6EDCubjzCksme7xfR9H7iwwVMh09JdQ/edit#gid=8). Acht Journal-Tabs, 25 Parameterzeilen (19 Handelsregeln plus sechs Betriebswerte), alle Zeilen verifiziert und identisch zum aktuellen Ausführungszustand. Standardwerte stehen getrennt; die weiterhin nicht festgelegte Policy wird nicht automatisch aktiviert. Der bestehende Handelszustand und sämtliche Laufzeitkonfiguration wurden bei der Quellaktualisierung erhalten. Die Darstellung wurde im Google-Browser geprüft; `journal_pending=0`, Dienst aktiv, weiterhin keine Exchange-Mutationen.

Update 05.10.2026: Signalhebel mit Deckelung am Marktmaximum auf dem VPS installiert; Master ergänzt, Quellen und vollständiger Ausführungszustand erhalten. Die reale Laufzeit berechnet bei einem synthetischen 50×-Signal und einem 20×-Marktlimit den effektiven Hebel 20×, ohne Börsenaufrufe. Das Journal enthält nun 26 verifizierte Parameterzeilen (20 Handelsregeln plus sechs Betriebswerte), einschließlich `Hebelmodus`; Queue leer, Dienst aktiv. Bestehende oder noch nicht festgelegte Regeln werden durch die Installation nicht automatisch umgestellt. Aktuell ist eine Quelle konfiguriert, die Policy noch nicht gewählt.
