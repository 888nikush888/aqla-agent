# Analyse: Was aqla Agent tatsächlich braucht

Stand: 02.10.2026. Analysiert wurde [TSX Commit e056b723](https://github.com/888nikush888/tsx-agent/tree/e056b723c13c6aafd9baca22a691f6cdaa70adc2). Der neue Projektname ist **aqla Agent**. Der Nutzer beauftragt Analyse und konkreten Bauplan, noch keine Implementierung.

## Urteil

TSX ist inzwischen eine Plattform für autonome Handelsverwaltung, Forschung, Auswertung und Bedienoberflächen. Für den gewünschten Ablauf bringt es nicht bloß zu viele Dateien mit, sondern die falsche Verteilung der Verantwortung: Der Agent bestimmt Handelsparameter weitgehend selbst, während der Nutzer feste Kriterien vorgeben möchte. Aqla sollte deshalb eigenständig neu entstehen.

Die entscheidende Aufteilung lautet: **Hermes versteht Sprache und Kontext. Python berechnet, prüft und führt feste Regeln aus. Hyperliquid hält die bestätigten Orders.** Ein kurzer Skill beschreibt diesen Ablauf. Er ersetzt keine mathematischen oder technischen Grenzen.

## Gemessener Umfang

| Merkmal | Geprüfter Stand |
|---|---|
| Repository-Dateien | 307, anhand des rekursiven Git-Baums |
| Python-Module unter `src/tsx_hyperliquid_mcp` | 82; zusammen 1.140.428 Bytes Quelltext |
| Eigene Skills | 22 |
| Master-Skill | 59.655 Bytes; 198 teils sehr lange Zeilen |
| MCP-Werkzeuge | 148 im aktuellen Katalog; README nennt veraltet 126 |
| Fachzustand | SurrealDB |
| Operationszustand | SQLite; zusätzlich SQLite-Usage-Spool im Hermes-Plugin |
| Telegram-Quellenadapter | Kotlin/TDLib mit eigener Build- und Ereignisinfrastruktur |
| Weitere Produkte | MiniApp, Browsertests, Forschung/Replays, Shadow/Trials, Ratings, Analytics, Export/Backupverwaltung |

Quellen: [Git-Baum](https://api.github.com/repos/888nikush888/tsx-agent/git/trees/e056b723c13c6aafd9baca22a691f6cdaa70adc2?recursive=1), [aktueller Werkzeugkatalog](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/docs/tools.md#L3), [Master-Skill](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/skills/tsx-master/SKILL.md), [Deployment](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/compose.yaml). Umfang ist hier ein Scope-Beleg, kein allgemeines Qualitätsurteil über die einzelnen Funktionen.

## Priorisierte Befunde für den Neubau

Aufwand S = Stunden, M = ungefähr ein Arbeitstag, L = mehrere Tage. Angaben beziehen sich auf den jeweiligen neuen Baustein einschließlich Tests, nicht auf eine TSX-Reparatur. Risiko bezeichnet das Implementierungsrisiko. Alle folgenden Befunde haben hohe Evidenzsicherheit.

| Priorität | Befund und Auswirkung | Aufwand | Risiko | Beleg |
|---|---|---|---|---|
| 1 | TSX lässt konkrete Handelsparameter autonom verwalten und lehnt freie numerische Nutzerwünsche ab. Das widerspricht festen Nutzerkriterien. | M | Mittel | [Master:10](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/skills/tsx-master/SKILL.md#L10), [Master:103](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/skills/tsx-master/SKILL.md#L103), [Master:179](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/skills/tsx-master/SKILL.md#L179) |
| 2 | Die allgemeine LLM-Deutung wird anschließend durch kanalgebundene Feldregex und Richtungsaliase eingeschränkt. Verständliche neue Formulierungen können dadurch abgelehnt werden. | M–L | Mittel | [signals.py:115](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/signals.py#L115), [signals.py:129](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/signals.py#L129) |
| 3 | Der typisierte Quellvertrag enthält keine Reply-/Threadzuordnung. Updatekandidaten werden nach Kanal, Symbol, Richtung und Neuheit sortiert. Für „Rest schließen“ braucht aqla eine belastbare Kontextbindung. | M | Mittel | [contracts.py:264](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/contracts.py#L264), [trade_updates.py:30](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/trade_updates.py#L30) |
| 4 | Fachliche Werkzeuge hängen tief an SurrealDB; Exchange-Aufrufe an SQLite. Container entfernen reicht nicht. Der Dateizustand muss neu und klein entworfen werden. | M | Hoch | [service.py:24](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/service.py#L24), [journal.py:56](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/journal.py#L56), [control.py:62](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/control.py#L62), [worker.py:67](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/hermes_plugin/tsx-events/worker.py#L67) |
| 5 | Der Master ist ausdrücklich nicht selbst bearbeitbar und im Deployment schreibgeschützt eingebunden; außerdem werden 20 Telegram-Menübefehle eingerichtet. Aqla benötigt einen anderen Bedienvertrag. | S–M | Mittel | [Master:14](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/skills/tsx-master/SKILL.md#L14), [compose.yaml:131](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/compose.yaml#L131), [hermes.yaml:87](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/config/hermes.yaml#L87) |
| 6 | Trotz 148 Tools ist der Exchangeumfang auf native USDC-Perpetuals begrenzt; HIP-3 und Vaults fehlen ausdrücklich. Ein vollumfassender MCP entsteht nicht durch Umbenennung. | L | Hoch | [service.py:612](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/service.py#L612), [models.py:17](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/models.py#L17) |

Zusätzlicher konkreter Befund: [trade_updates.py:72–80](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/trade_updates.py#L72) prüft vorhandene Updatebindungen nur für dieselbe Quelle-und-Trade-Kombination. Ein symbol-/richtungsloses Update kann deshalb an mehrere Trades desselben Kanals gebunden werden. Aqla speichert jede Updatequelle eindeutig und prüft diese Bindung vor einer Aktion. Aufwand S–M, geringes Änderungsrisiko bei einem seriellen Writer, hohe Sicherheit aus dem gelesenen Kontrollfluss; kein laufender Exploit oder Börsenversuch wurde durchgeführt.

## Was aus dem Konzept erhalten bleiben muss

Folgende Eigenschaften sind für Geld bewegende Software erforderlich und keine zusätzlichen Produktfunktionen:

- `Decimal`-Berechnungen und Markt-/Mengenraster; SL/TP reduzieren vorhandene Exposure.
- Stabile Quellen-, Trade- und Operationsidentitäten; identische Wiederholung sendet keine zweite Order.
- Absicht und Sendephase vor Netzwerkaufruf dauerhaft speichern. Timeout oder Absturz bedeutet gegebenenfalls „unklar“, nicht „fehlgeschlagen“.
- API-Wallet, tatsächliches Konto und Netzwerk sauber trennen; Nonces pro Signer serialisieren.
- Teilfills berücksichtigen. TP-Budgets beziehen sich auf eigene Fills, nicht auf Erfolgsmeldungen des Signalkanals.
- Alten SL erst entfernen, nachdem der Ersatzschutz bestätigt ist. Entry-Pause lässt Schutz und Exits weiterlaufen.
- Zu Beginn ein aktiver Trade je Konto und Markt. Mehrere Kanäle erzeugen auf Hyperliquid keine unabhängig verwaltbaren Positionen im selben Markt.

Belege: [service.py:194](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/service.py#L194), [service.py:219](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/service.py#L219), [service.py:538](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/service.py#L538), [models.py:70](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/src/tsx_hyperliquid_mcp/models.py#L70). Verhalten neu spezifizieren und testen; keine Pflicht zur Übernahme der Implementierung.

## Drei Konsequenzen für aqla

1. **Die bestehende Hermes-Telegram-Anbindung verwenden.** Nach Nutzerkorrektur entfällt die ursprünglich vorgeschlagene AIAgent-Einbettung mit eigenem Bottransport. Der Nutzer richtet Hermes selbst ein; Aqla ergänzt MCP, Master-Skill und bei Bedarf eine kleine Quellen-Ereignisanbindung. Der geprüfte native Gateway verwendet intern `state.db`. Das bleibt ein gesonderter Integrationspunkt gegenüber „keine Datenbank“; daraus folgt keine Berechtigung, einen Ersatzbot oder eigene Agentenlaufzeit einzubauen. [Gateway-Speicher](https://github.com/NousResearch/hermes-agent/blob/1a4508e2aff2db5f50409893a2115be777bd5643/gateway/session_persistence.py#L43).
2. **Telegram-Lesen vom Gespräch unterscheiden.** Der Betreiber spricht mit genau einem Bot. Der Quellenleser verwendet ein zugriffsberechtigtes Telegram-Konto für abonnierte Kanäle/Gruppen; er schreibt dort nicht. Telethon kann die Sitzung als `StringSession` statt SQLite speichern. Ein Bot allein erhält nicht sämtliche fremden Quellen. [Telegram-Empfangsregeln](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get), [Telethon-Sitzungen](https://docs.telethon.dev/en/stable/concepts/sessions.html).
3. **API-Vollständigkeit von Agentenbefugnissen trennen.** Der eigene MCP bildet die dokumentierten HyperCore-Info-/Exchange-/WebSocket-Funktionen ab. Der automatische Signalhandel erhält nur seinen kurzen, regelgeprüften Werkzeugpfad. Spot, HIP-3, Transfers oder administrative Funktionen werden dadurch nicht als Nebenwirkung eines Signals aktiviert. Die Abdeckung wird pro Funktion dokumentiert. [API-Referenz](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api), [Python-SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/tree/2fdb18f9517675ea03695a0962bd19eece9c83f0).

## Test- und Auditgrenzen

TSX hat umfangreiche Tests und CI-Befehle: `uv sync --frozen --all-extras`, `uv run --frozen --no-sync ruff check .`, `uv run --frozen --no-sync pytest -q`, Katalogcheck, CLI-Smoke und Build. [CI](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/.github/workflows/checks.yml#L15).

Die geprüften Signaltests geben jedoch die Interpretation bereits vor: [test_vip_coinsignals042.py:44](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/tests/test_vip_coinsignals042.py#L44) konstruiert ein fertiges `NormalizedSignal`; [Zeile 212](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/tests/test_vip_coinsignals042.py#L212) setzt `intent="update"` von Hand. Das belegt Validatorverhalten, nicht allgemeine Hermes-Erkennung. [Update-Integrationstests](https://github.com/888nikush888/tsx-agent/blob/e056b723c13c6aafd9baca22a691f6cdaa70adc2/tests/test_trade_updates029.py#L16) werden ohne SurrealDB-Konfiguration übersprungen.

Für aqla müssen Sprach-/Kontexterkennung und deterministische Ausführung deshalb getrennt geprüft werden. Diese Analyse hat keine Tests ausgeführt und weder Erkennungsqualität noch Live-Trading bestätigt. Keine Vollprüfung aller Dateien, Abhängigkeiten, Server, Medienfunktionen oder sämtlicher Exchange-Endpunkte. Die im Bauplan vorgeschlagenen Befehle sind künftige Abnahmebefehle des neuen Projekts.
