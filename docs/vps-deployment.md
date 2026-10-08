# VPS-Betriebsabnahme

Stand: 03.10.2026. Tatsächliche Installation auf Debian 13 x86_64, getrennt unter `/opt/aqla`; Quellpaket unter `/opt/aqla-source`. Vorher vorhandene ungenutzte Docker-/Containerd-Pakete und Caches wurden auf ausdrücklichen Nutzerauftrag entfernt. Debian, SSH, Netzwerk und Provider-Systemdienste bleiben erhalten.

## Vorbereitungspfade — Offlineabnahme 08.10.2026

Der Installer kodiert Zielpfade für die jeweilige systemd-Einstellung. Normale Pfade, Leerzeichen sowie literale Prozentzeichen, Dollarzeichen und geschweifte Klammern werden unterstützt. `WorkingDirectory` und `EnvironmentFile` verwenden rohe Pfadwerte mit maskierten Prozent-Spezifizierern; der vollständige Interpreterpfad ist ein einzelner gequoteter `ExecStart`-Token mit deaktivierter Environment-Substitution. Es wird keine Shell ausgeführt. Auch ein Linux-Verzeichnisname mit einem abschließenden Leerzeichen bleibt erhalten.

Ziele mit C0-/DEL-Kontrollzeichen oder einfachen/doppelten Anführungszeichen werden vor `resolve`, Verzeichnisanlage und Dateikopien abgewiesen. Unter POSIX gilt dies zusätzlich für einen literalen Backslash im Dateinamen: Der native systemd-Parser lehnt diese Executablezeichen auch nach korrekter Tokendekodierung ab. Normale Windows-Pfadseparatoren bleiben für `--prepare-only` zulässig. Wähle einen neuen oder leeren Zielordner; bestehende nichtleere Installationen werden weiterhin unverändert abgewiesen.

Die lokale Linuxprüfung verwendet vorhandene WSL-/systemd-Werkzeuge in einem isolierten Netzwerknamespace und frische synthetische Ziele. `systemd-analyze verify --generators=no --man=no` prüft einen tatsächlich vorhandenen ausführbaren Interpreterpfad; ohne genau diese Datei muss die Gegenprobe den vollständigen Pfad als nicht ausführbar melden. Secret-/Homeverzeichnisse bleiben 0700, Integrationsdatei und `vps.env` 0600. Diese statische Abnahme startet keinen Dienst und ist keine neue Liveabnahme des VPS.

## Recoverydiagnose — Offlineprüfung 09.10.2026

`status.account_recovery` zeigt höchstens die sechs Börsen-/Netzwerkpaare mit letztem Versuch, letztem erfolgreich abgeschlossenen Schutzdurchlauf, Dauer und sicherer Kategorie (`complete`, `incomplete`, `unavailable`, `timeout`, `cancelled`). Exceptiontext und Providerpayload werden nicht gespeichert oder ausgegeben. Ein Timeout oder ungeklärte Operationen/Mengen aktualisieren keinen Erfolgsbeleg; der frühere Erfolgszeitpunkt bleibt als Historie erhalten. Der Konstruktor begrenzt standardmäßig zwei parallele Konten und 40 Sekunden je Abgleich, ohne Signer-/Nonce-/Orderidentitäten oder die bestehenden Preisfreshnessguards zu ändern. Diese Grenzen und Ereignisfixtures sind lokale Funktionsprüfungen, keine neue VPS-/Live-Latenzabnahme.

## Nachgewiesen

- Release-ZIP per verschlüsseltem SFTP übertragen, SHA-256 vor Extraktion verglichen.
- Aqla Python 3.12.14, separat gepinntes Hermes mit Python 3.14.7 und geprüften Dependency-Overrides installiert.
- Dienstkonto `aqla`, systemd-Autostart, geheime Dateien mit Modus 0600, privates Hermes-Home und Secretverzeichnis.
- Aktives Modell auf Nutzerwunsch: `deepseek-v4.1-flash` über OpenCode Go, Hermes `agent.reasoning_effort: ultra`. Hermes übersetzt dies in DeepSeeks höchste API-Stufe `max`; tatsächlicher nativer AIAgent-Aufruf aus der gespeicherten Konfiguration mit `reasoning_effort=max`, zurückgeliefertem Reasoning und erfolgreicher Antwort geprüft. Provider-/Sessionrouting wird vom gepinnten Hermes unterstützt; [Provider-Endpunkte](https://opencode.ai/docs/go/), [DeepSeek-Modellparameter](https://api-docs.deepseek.com/api/list-models/).
- Telegram-Bot authentifiziert und in Pollingmodus verbunden. Nutzer bestätigt tatsächliche Antwort auf Systemstatus-Anfrage.
- Telegram-Kanalleser über QR authentifiziert; angemeldete Nutzer-ID entspricht dem konfigurierten Betreiber. StringSession als geschützte Datei, keine SQLite-Sitzung.
- Hyperliquid-API-Wallet an das konfigurierte **Testnet**-Hauptkonto gebunden; Konto und Marktmetadaten lesbar, Standardkonto-Abstraktion unterstützt.
- Keine Exchange-Mutationen während Einrichtung. Eine bereits im Konto vorhandene Position wurde nicht als eigener Aqla-Trade importiert oder verändert.
- Dienst läuft; Handelszustand und native Hermes-Gespräche werden seit der SQLite-Erweiterung in getrennten Datenbanken gespeichert. Öffentlich lauscht nur der vorhandene SSH-Port.

Beim Live-Start erkannte ungenutzte native Cron-/Housekeeping-Factory wurde im Aqla-Profil deaktiviert und als nativer Regressioncheck abgesichert. systemd Stop-Frist beträgt 90 Sekunden, `KillMode=mixed` erhält MCP-Kinder bis zur geordneten Gatewayabschaltung. Native Lazy-Installationen ändern die vom Installer geprüfte Laufzeit nicht mehr.

## Bewusst noch zu konfigurieren

Handelsregeln und Quellen wählt der Nutzer über den Bot; sie wurden nicht automatisch festgelegt. Neue Trades bleiben bis zu gültiger Policy und zulässiger Quelle beziehungsweise Betreiber-Weiterleitung gesperrt. Bereits vorhandene fremde Positionen unterliegen weiterhin dem Entry-Besitzguard.

Google Sheets wurde anschließend im geöffneten Nutzerbrowser eingerichtet und aktiviert: eigenes Dienstkonto ohne zusätzliche Projektrollen, auf die Journaltabelle beschränkte Bearbeiterfreigabe und geschützter Servicekonto-Schlüssel auf dem VPS. Der private RSA-Schlüssel wurde lokal erzeugt; ausschließlich sein öffentliches X509-Zertifikat wurde in Google Cloud registriert. Der ungenutzte Schlüssel aus dem nicht verfügbaren Browserdownload wurde widerrufen.

Tatsächliche Abnahme: sieben Journal-Tabs vorhanden, eine verifizierte aktuelle Regelzeile übertragen, `journal_pending=0`, letzter Sync bestätigt und keine Exchange-Mutationen. [Journaltabelle](https://docs.google.com/spreadsheets/d/1hLVjDSZJ_m5h6EDCubjzCksme7xfR9H7iwwVMh09JdQ/edit). Der Dienst bleibt aktiv; Tabellen-ID und Schlüssel liegen geschützt auf dem VPS. Dieser Bericht enthält keine Host-/Login-/Wallet-/Bot-/Modellschlüssel oder Sessionwerte.

Anschließend auf Nutzerwunsch ergänzt: [aqla Handelsparameter](https://docs.google.com/spreadsheets/d/1hLVjDSZJ_m5h6EDCubjzCksme7xfR9H7iwwVMh09JdQ/edit#gid=8). Acht Journal-Tabs, 25 Parameterzeilen (19 Handelsregeln plus sechs Betriebswerte), alle Zeilen verifiziert und identisch zum aktuellen Ausführungszustand. Standardwerte stehen getrennt; die weiterhin nicht festgelegte Policy wird nicht automatisch aktiviert. Der bestehende Handelszustand und sämtliche Laufzeitkonfiguration wurden bei der Quellaktualisierung erhalten. Die Darstellung wurde im Google-Browser geprüft; `journal_pending=0`, Dienst aktiv, weiterhin keine Exchange-Mutationen.

Update 05.10.2026: Signalhebel mit Deckelung am Marktmaximum auf dem VPS installiert; Master ergänzt, Quellen und vollständiger Ausführungszustand erhalten. Die reale Laufzeit berechnet bei einem synthetischen 50×-Signal und einem 20×-Marktlimit den effektiven Hebel 20×, ohne Börsenaufrufe. Das Journal enthält nun 26 verifizierte Parameterzeilen (20 Handelsregeln plus sechs Betriebswerte), einschließlich `Hebelmodus`; Queue leer, Dienst aktiv. Bestehende oder noch nicht festgelegte Regeln werden durch die Installation nicht automatisch umgestellt. Aktuell ist eine Quelle konfiguriert, die Policy noch nicht gewählt.

Die Skill-Verbesserung verwendet Hermes' nativen JSON-Freigabespeicher unter `hermes-home/pending/skills`. Hintergrundprüfungen dürfen einen einzelnen Entwurf für `aqla-master` vorbereiten und fragen über die native Telegram-Zustellung nach Zustimmung. `/skills pending` listet den Entwurf; `/skills diff <id>`, `/skills approve <id>` und `/skills reject <id>` erlauben Prüfung und Entscheidung. Das Aqla-Profil erzwingt die Freigabe auch bei falscher Konfiguration oder ausgeschaltetem nativen Toggle. Unauthentifizierte und Quellenfreigaben bleiben gesperrt; zwischenzeitlich geänderte Master machen einen Entwurf ungültig. Die bisherige Größenbegrenzung von 8 KiB bleibt bestehen. Native Regressionchecks prüfen Entwurf, Frage, Zustimmung, Ablehnung, Grenzen und veraltete Inhalte ohne Modell-, Telegram- oder Börsenaufrufe.
