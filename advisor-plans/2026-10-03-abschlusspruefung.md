# Abschlussprüfung vom 03.10.2026

**Historisches Ergebnis für `d5d6e60`: zwölf bestätigte Befunde offen.** Nach dem ausdrücklichen Behebungsauftrag sind alle zwölf korrigiert; [aktueller Korrektur- und Testnachweis](../docs/quality-review.md). Der folgende Bericht bleibt als Ausgangsnachweis erhalten.

Geprüfter Quellstand: `d5d6e602c814375501fe614a2f7fee5a7ead2495`. Seit der [vorherigen Nachprüfung](2026-10-03-nachpruefung.md) gab es keine Sourceänderung. Die dortigen IDs 21–28 bleiben offen. Diese Abschlussprüfung bestätigt sie erneut und ergänzt vier unabhängige Befunde, IDs 29–32. Die ursprünglichen 20 Korrekturen bleiben vorhanden.

## Umfang und Verfahren

Lesender Review aller Aqla-Sourcepakete, relevanter Tests, nativer Hermes-Grenzen und Deployment-/Paket-/CI-Verträge. Getrennte Reviews für automatische Ausführung, vollständige API/Auth sowie Sheets/Journal/Deployment; der Hauptreviewer liest die genannten Stellen und führt jede neue Gegenprobe selbst erneut aus. Keine Source-/Test-/Konfigurationsänderungen, kein Commit, Push oder reale Exchange-/Telegram-/Google-/Modellaktion. Neue Reproduktionen verwenden ausschließlich lokale Fixtures, MockTransport und ein pausiertes Socket-Testobjekt in ignorierter `.cache`.

Besonders geprüft: TP-Budgets nach Reduktion und Zielersatz, Full-MCP-HIP-3-Preisraster, Signatur-/Vault-Scope, Subscriptions während Verbindungsaufbau, Queue-/Ack-Durability, Quellen-/Operator-Grenzen, Single-Writer-State, Conversation-Adapter und isolierter VPS-Vertrag.

## Gesamtstatus

| IDs | Bereich | Status |
|---|---|---|
| 01–20 | Ursprüngliche Vollprüfung | Korrekturen vorhanden |
| 21–23 | Ungesendete Absage, historische Referenzen, bekannte Fremdposition | Offen, Gegenproben erneut bestätigt |
| 24–27 | Native Folge-Turns, Abbruchbefehle, Adapter-Reconnect, Quellen-RPC-Isolation | Offen, Gegenproben mit tatsächlichen gepinnten Hostmethoden erneut bestätigt |
| 28 | Automatischer USDC-/Collateralguard | Offen, unveränderte Vertragslücke |
| 29 | Einstandsanhebung nach verändertem TP-Budget | Neu bestätigt, offen |
| 30 | HIP-3-Preisraster im Full-MCP-Markethelper | Neu bestätigt, offen |
| 31 | Vault-Scope im signierten Full-MCP-Payload | Neu bestätigt, offen |
| 32 | Subscriptionzustand während WS-Verbindungsaufbau | Neu bestätigt, offen |

## Neue Befunde

S = lokal begrenzte Korrektur plus Regression; S–M = mehrere zusammengehörige Verträge. Risiko bezeichnet das Änderungsrisiko. Alle vier Befunde haben hohe Konfidenz und Priorität P2; sie ersetzen keine der offenen P1-Korrekturen aus der vorherigen Prüfung.

| ID | Befund | Auswirkung | Evidenz | Aufwand / Risiko |
|---|---|---|---|---|
| 29 | Break-even vergleicht TP-Fills erneut mit ursprünglicher Entryverteilung statt fortgeschriebenem Zielbudget | Vollständig gefüllter aktueller TP1 löst nach Reduktion und Zielersatz die konfigurierte Einstandsanhebung nicht aus | `src/aqla_hyperliquid_mcp/trades.py:487–489`, `:792–800`, `:818`; `README.md:80` | S / mittel |
| 30 | Full-MCP übernimmt SDK-Spotklassifikation für HIP-3-Asset-IDs | `market_open` und entsprechend `market_close` können ungültige Perp-Wirepreise erzeugen | `src/aqla_hyperliquid_mcp/api.py:344–348`; SDK `hyperliquid/exchange.py:127–132` | S / niedrig |
| 31 | Wire-Vault wird für L1-Aktionen pauschal aus der Konfiguration übernommen | Principal-Aktionen wie `set_referrer` erhalten eine nicht zum gesendeten Vault-Scope passende Signatur | `src/aqla_hyperliquid_mcp/api.py:358–361`; SDK `hyperliquid/exchange.py:440–447` | S–M / mittel |
| 32 | Neue gewünschte Subscriptions während erster Resubscribe-Sends werden nicht nachgezogen | Client meldet Subscription als angefordert, auf der verbundenen Session fehlt sie bis zum nächsten Reconnect | `src/aqla_hyperliquid_mcp/streams.py:84–86`, `:141–145`; entsprechende Remove-Grenze `:149–151` | S / niedrig |

### 29 — Fortgeschriebenes TP-Budget

Lokale Gegenprobe `.cache/final-execution-review/recheck_current_tp_budget.py` nutzt echte `Trading`-Methoden und den vorhandenen `FakeExchange`. Policy `break_even_after_tp=1`; Entry 4,80, anschließend 50 % reduzieren und Ziele durch 115/125 ersetzen. Der neue TP1 mit Menge 1,20 wird vollständig gefüllt. Zustand: ursprüngliches TP1-Budget 2,40; aktuelles `target_budgets[0]=1.20`; `tp_filled[0]=1.2`; Restposition 1,20; `stop_price=90.000`, obwohl `entry_vwap=100`.

Korrekturgrenze: Vollständigkeit anhand fortgeschriebener konsumierter und noch vorgesehener Zielbudgets prüfen. Erfüllte TP-Budgets nicht auffüllen; bewusst entfallene oder auf null reduzierte Ziele nicht als bewiesenen TP-Fill werten. Reduktion → Zielersatz → echter Fill → Stopersatz als Regression absichern.

### 30 — HIP-3 ist Perp trotz hoher Asset-ID

Lokale Gegenprobe `.cache/final-api-audit-repro.py` ruft den wirklichen `sdk_action`-Pfad mit MockTransport auf. Gelisteter HIP-3-Perp `xyz:LOW`, `szDecimals=2`, gültiges Mid `0.0123`: der SDK-Markethelper erzeugt `p=0.012915`. Das sind sechs Nachkommastellen; zulässig sind vier. Die [offizielle Tick-/Lot-Spezifikation](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size) verlangt für Perps höchstens `6 - szDecimals`.

Ursache ist die [gepinnte SDK-Helferlogik](https://raw.githubusercontent.com/hyperliquid-dex/hyperliquid-python-sdk/2fdb18f9517675ea03695a0962bd19eece9c83f0/hyperliquid/exchange.py), die `asset >= 10000` pauschal als Spot betrachtet. Aqla verwendet diesen Klassenhelper unverändert; eigene HIP-3-Metadaten mit Asset-ID ab 100000 fallen ebenfalls darunter.

Korrekturgrenze: Im SDK-Facade produktspezifisch anhand Aqla-Metadaten runden, mit unabhängigen Wirepreis-Vektoren für native Perps, HIP-3 und Spot. Die automatische Signalstrecke benutzt bereits ihren eigenen Perp-Preisguard und ist von diesem konkreten Helperfehler getrennt. Kein globales SDK-Monkeypatch und keine neue Slippagevorgabe einführen.

### 31 — Signatur und tatsächlicher Wire-Scope

Dieselbe lokale Gegenprobe konfiguriert ein Vault und ruft `set_referrer` auf. Der SDK-Vertrag signiert die Principal-Aktion mit `vault=None`; Aqla-Capture schreibt trotzdem das konfigurierte Vault in den Payload.

Unabhängige Wiederherstellung mit `recover_agent_or_user_from_l1_action` anhand des tatsächlich abgefangenen Payloads ergibt `signature_matches_wire_vault=false`, aber `signature_matches_null_vault=true`. Es wurde kein Request an die Börse geschickt. Die [offizielle Signierbeschreibung](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/signing) warnt ausdrücklich vor Signaturen, deren Recoverypayload von der gesendeten Aktion abweicht.

Korrekturgrenze: Principal- und Vault-Aktionen explizit zuordnen; Signatur, Wirepayload und dauerhafte Operationsbindung müssen denselben Scope tragen. Alternativ unzulässige Scopekombinationen vor Versand verweigern. Unabhängige Recoverytests für konfigurierte Vault- und Principal-Aktionen ergänzen; die vorhandenen Fakes allein erkennen keine Signaturabweichung.

### 32 — Gewünschte Subscriptions während Aufbau

Gegenprobe `.cache/final-stream-audit-repro.py`: pausierter lokaler Socket innerhalb des ersten Subscribe-Sends für BTC. Während `ready=False` eine ETH-Subscription anfordern; danach ersten Send freigeben. Ergebnis nach erfolgreichem Aufbau: `second_state=requested`, `desired_coins=[BTC, ETH]`, `sent_coins=[BTC]`. ETH fehlt, obwohl die Verbindung bereit ist.

Korrekturgrenze: Gewünschten Subscriptionzustand mit Aufbau und Sends synchronisieren. Hinzufügen und Entfernen während pausiertem Resubscribe prüfen, doppelte Subscriptions vermeiden. Diese Reparatur betrifft Informationsabonnements; Exchange-Writes weiterhin niemals bei Reconnect wiederholen.

## Gates und Einordnung

Die veröffentlichte [CI für diesen unveränderten Commit](https://github.com/888nikush888/aqla-agent/actions/runs/37117758123) bleibt grün. Der erneute vollständige lokale Lauf ist abgeschlossen: **396 Tests bestanden**, keine übersprungenen Sockettests; Ruff, API-Abdeckungscheck und tatsächlicher Wheel-/sdist-/VPS-ZIP-/SHA-256-Check inklusive README-Lint im entpackten Paket bestanden. Der API-Namens-/Schemascheck meldet 59 Info-Typen, 40 Exchange-Typen, 52 SDK-Operationen und 24 Subscriptions vollständig. Er beweist keine korrekten Preise oder Signaturen in allen Kontexten, wie Befunde 30/31 zeigen.

Git-Historienscan erneut ohne Secretfund; gepinnter Hermes-Checkout unverändert. Die sechs früheren Ausführungs-Gegenproben sowie die Quellen-RPC-, native Drain-, Stop- und Reconnect-Gegenproben wurden ebenfalls erneut ausgeführt und bestätigen die offenen Fehler. Alle vier neuen Gegenproben wurden vom Hauptreviewer selbst erfolgreich erneut ausgeführt. Die bisherigen grünen Gates decken diese unerwünschten Verhaltensfälle nicht als Regressionen ab.

## Verworfene Verdachtsfälle und Prüfgrenzen

- Keine neuen bestätigten Defekte in Sheetsprojektion, Revisionen, Digest-Ack, Retention, Queue-Wiederanlauf oder den dokumentierten Transport-/Statistikgrenzen.
- Source-/Full-Profiltrennung, gesperrte Operatorsteuerung aus Quellen und geschützte interne Journaltools bleiben vorhanden. Das absichtlich privilegierte Full-Profil für explizite lokale Clients ist keine Autorisierungslücke.
- Ein unreservierter Multisig-Prepare-Nonce ist ausdrücklich dokumentiert; daraus wird kein zusätzlicher Defekt abgeleitet.
- Beschädigte Conversation-Dateien brechen absichtlich ab, statt leere Historie vorzutäuschen. Native Telegram-Service-Nachrichten werden als leerer Kontext verarbeitet; Telethon überspringt `MessageEmpty` beziehungsweise ersetzt fehlende IDs durch `None`.
- Bestehender Stop bleibt bei nicht abschließbaren Ersatzschutz-/Fillabfragen erhalten. Stopausführung und tatsächliche Marktverluste sind nicht mit berechnetem SL-Risiko gleichzusetzen; daraus wurde keine zusätzliche Fehlerbehauptung abgeleitet.
- Isolierter VPS-Installer, Secretpfade, Laufzeitvorgabe, Wheel-/Quellordnervertrag, Artefaktallowlist und CI wurden gelesen; kein weiterer belegter Defekt in diesem Prüfbereich.
- Keine formale Vollständigkeitsgarantie, gesamte Hermes-/Dependency-Quellcodeprüfung, Installation auf einem realen VPS, echte Telegram-/Google-/Börsenabnahme oder Evaluation des gewünschten echten Sprachmodells. Die allgemeine Erkennungsqualität bleibt eine eigene Modellprüfung.

## Nächster notwendiger Arbeitsschritt

Die offenen IDs 21–32 beheben und ihre Gegenproben als reguläre Regressionen übernehmen. Zuerst Handelsabsage, Eventbindung und bekannte Fremdposition; native Turn-/Abbruch-/Reconnect-Grenzen gemeinsam behandeln. Quellen-RPC-Isolation und USDC-Guard erhalten. Danach TP-Budget, Full-MCP-Raster/Signatur und WS-Lifecycle korrigieren. Anschließend vollständige Testmatrix, native Prüfungen, Dependency-/Secretgates und Paketcheck erneut ausführen.

**Diese Abschlussprüfung schließt den Review ab, nicht die noch offene Fehlerbehebung.**

Status nach Behebung am 03.10.2026: IDs 21–32 korrigiert. Die Behebungspläne 033–035 wurden in getrennten Worktrees umgesetzt, reviewed, zusammengeführt und erneut geprüft: 498 Tests, 80,79 Prozent Branch-Coverage und tatsächliche native Source-/Owner-/Lifecycle-Smokes bestanden. Der oben beschriebene offene Status dokumentiert die Ausgangsprüfung vor Umsetzung.
