# Unabhängige Nachprüfung vom 03.10.2026

**Historisches Ergebnis für `d5d6e60`: acht weitere Befunde offen.** Nach dem ausdrücklichen Behebungsauftrag sind diese korrigiert; [aktueller Korrektur- und Testnachweis](../docs/quality-review.md). Die folgenden Befunde dokumentieren den damaligen Stand.

Geprüft wurde der veröffentlichte Commit `d5d6e602c814375501fe614a2f7fee5a7ead2495`. Die 20 früheren Korrekturen sind vorhanden. Die erneute Prüfung hat jedoch weitere Zustands- und native Lifecyclefälle gefunden, die von den grünen Tests bisher nicht erfasst werden. Quellcode, Konfiguration, bestehende Hermes-Installation und veröffentlichter Git-Stand wurden nicht verändert.

## Bestehende Gates und unabhängige Gegenproben

- Vollständiger erneuter Testlauf: **396 bestanden**, keine übersprungenen lokalen Sockettests. Ruff, tatsächlicher Wheel-/sdist-/VPS-ZIP-Inhalt, SHA-256 und README-Lintcheck im entpackten Paket bestanden.
- [Veröffentlichter CI-Lauf](https://github.com/888nikush888/aqla-agent/actions/runs/37117758123): alle sieben Jobs erfolgreich, einschließlich Python 3.12/3.13/3.14, nativer Hermes-Laufzeit, beiden Dependency-Scans und Paketjob. Git-Historienscan erneut ohne Secretfund.
- Ausführungsreview: zusätzlich 97 bestehende relevante Tests bestanden. Sechs isolierte Scratchfälle bestätigen unerwünschte Abläufe; dies sind Fehlernachweise, keine erfolgreichen neuen Schutztests.
- Sheetsreview: 41 Tests bestanden; keine neuen bestätigten Fehler bei Projektion, Queue, Revisionen, Zustellungsbestätigung, Datenintegrität oder begrenzten Transporten.
- Native Gegenproben verwenden die tatsächlichen Methoden des unveränderten gepinnten Hermes `1a4508e2aff2db5f50409893a2115be777bd5643`. Telegram-, Modell-, MCP- und Exchange-Aktionen sind darin durch lokale Testgrenzen ersetzt. Sie decken rekursive Folge-Turns, Abbruchbefehle und Adapterwechsel ab.
- Die bestehenden nativen Standardsmokes wurden erneut ausgeführt und bestanden: Session-/Ingress-/Befehlsprüfung sowie Source-/Owner-Pipelines mit je sechs lokalen geskripteten Modellaufrufen, beiden stdio-MCPs und null Exchange-Aktionen. Diese bisherigen Smokes decken die neuen Lifecycle-Gegenproben nicht ab.
- Öffentliche Hyperliquid-Metadaten wurden ausschließlich lesend abgefragt, um den Collateralvertrag zu verifizieren. Keine Nutzerkonten, Orders, Google- oder Telegram-Verbindungen wurden verwendet.

Alle Gegenproben wurden vom Hauptreviewer selbst erneut ausgeführt und die angegebenen Quellstellen gelesen. Scratchprogramme liegen ausschließlich in ignorierter `.cache`; sie gehören nicht zur Auslieferung.

## Neue Befunde

S = lokal begrenzte Korrektur und Tests; M = mehrere native beziehungsweise Handelsübergänge. Risiko bezeichnet das Änderungsrisiko. Nummerierung setzt die historische Prüfung fort.

| ID | Prio | Befund und konkrete Auswirkung | Evidenz | Aufwand / Risiko | Konfidenz |
|---|---|---|---|---|---|
| 21 | P1 | Absage eines zugelassenen, nachweislich noch ungesendeten Entries wird nicht dauerhaft gespeichert. Recovery eröffnet ihn später trotz bestätigtem `cancel_entry` oder `close`. Auch Betreiber-Close verwendet den fehlerhaften Pfad. | `trades.py:507–508`, `:601–605`, `:848–849`; `server.py:164–168` | S / mittel | hoch |
| 22 | P1 | Interpretation ist nicht an ihren auslösenden Event gebunden. Ein unabhängiger Liveevent kann einen Trade ausschließlich aus historischen Referenzen autorisieren. | `trades.py:99–117`, insbesondere `:104` und `:113` | S / niedrig | hoch |
| 23 | P1 | Spätere frische Kontoantworten zeigen bereits eine Fremdposition; der Entry-Guard ignoriert sie und erhöht trotzdem die vermischte Nettoposition. | `api.py:114–115`; `trades.py:192–198`, `:240` | S–M / mittel | hoch |
| 24 | P1 | Native rekursive Warteschlangen-Turns umgehen den Aqla-Adapterhandler. Weitergeleitete Quellen werden nicht eingelesen; Folge-Turns verlieren ihre Tool-Autorität. | `events.py:207–219`, `:294–304`; gepinnter Hermes `gateway/run_turn.py:3899`, `agent/turn_finalizer.py:769–784` | M / mittel | hoch |
| 25 | P1 | `/stop`, `/new` und `/reset` warten auf den Lock des laufenden Turns und erreichen die native Cancellation erst, wenn dieser von selbst endet. | `events.py:208–219`; gepinnter Hermes `gateway/platforms/base.py:3607`, `:4016–4028` | M / mittel | hoch |
| 26 | P1 | Nach nativer Telegram-Neuverbindung erhält der neue Adapter keine Aqla-Anbindung. Forward-Ingest und authentifizierte Tool-Kontexte fehlen bis zum Prozessneustart. | `events.py:195–197`; gepinnter Hermes `gateway/run_adapters.py:789–798`, `:853–886` | S–M / mittel | hoch |
| 27 | P1 | Ein noch aufgelisteter Kanal mit verweigertem Nachrichtenabruf blockiert die nachfolgenden gesunden Quellen. Der globale Reader verbindet sich immer wieder neu und scheitert am selben Kanal. | `telegram.py:89–122`, insbesondere `:96`, `:118`, `:121`; `events.py:120–124` | S–M / niedrig–mittel | hoch |
| 28 | P2 | Perpetual-Metadaten mit Nicht-USDC-Collateral werden pauschal als USDC modelliert. Dadurch lässt die automatische USDC-Beschränkung einen dokumentierten alternativen Collateralvertrag passieren. | `api.py:63–65`, `:99–100`; `models.py:192` | S–M / mittel | hoch für Vertragslücke; keine aktuelle Liveorder behauptet |

Pythonpfade ohne Präfix liegen unter `src/aqla_hyperliquid_mcp/` beziehungsweise `src/aqla_agent/`.

## Reproduktion und Korrekturgrenzen je Befund

### 21 — Absage vor Entryversand

Lokaler `FakeExchange`: nach `execute`-Zulassung vor `_entry_steps` abbrechen. Trade bleibt `preparing`, Entryoperation fehlt. Ein gebundenes `cancel_entry`-Update liefert `not_sent: true`; `close` liefert `already_closed: true`. Beide Updates werden als `updated` gespeichert. Der nächste `recover()` eröffnet trotzdem die Position.

Gegenprobe: `.cache/recheck-execution/test_recheck.py`, `test_cancel_update_before_entry_send_does_not_stop_recovery_entry`, beide Varianten.

Korrektur: Nachweislich ungesendeten Entry atomar terminal abbrechen und Risikoreservierung freigeben. Absage, Betreiber-Close und Recovery müssen denselben dauerhaften Zustand beachten. Tatsächlich gesendete, vorbereitete oder unbekannte Operationen weiter exakt abgleichen; deren Unsicherheit nicht mit einem lokalen Cancelstatus überdecken. Bestehende finanzierte Exits erhalten.

### 22 — Historische Referenzen unter unabhängigem Liveevent

Ein vollständiges historisches Setup wird als Kontext eingelesen; anschließend trifft ein unabhängiger Liveevent ein. `preview(live_event_id, interpretation)` mit ausschließlich der historischen `source_id` besteht, solange das historische Setup noch innerhalb der Altersgrenze liegt. `execute` eröffnet einen Trade, obwohl der auslösende Liveevent nicht in `source_refs` enthalten ist und die historische Quelle weiter `context` bleibt.

Gegenprobe: `.cache/recheck-execution/test_recheck.py`, `test_historical_only_refs_authorize_entry_from_unrelated_live_event`.

Korrektur: Aktionsfähige Interpretationen müssen ihren tatsächlichen auslösenden Event referenzieren; ausschließlich historische Referenzen dürfen keine neue Aktion autorisieren. Historische Nachrichten dürfen weiterhin Ergänzungskontext sein. Keine kanalbezogenen Pflichtmuster oder Regex einführen.

### 23 — Bereits beobachtete Fremdposition

Die erste Positionsabfrage ist leer. Vor der späteren Kontoabfrage erscheint eine fremde Position von `1.0`. Beide frischen `clearinghouseState`-Antworten vor Entryversand enthalten diese Position. `account_snapshot` verwirft `assetPositions`, und `validate_admission` prüft nur Equity/Margin. Es wird eine zusätzliche Entryorder gesendet; anschließend erkennt die Besitzprüfung die Abweichung und verweigert Verwaltung.

Gegenprobe: `.cache/recheck-execution/test_recheck.py`, `test_foreign_position_visible_in_fresh_account_is_ignored_before_entry`; Ausgabeprüfung `positive_before_entry == ['1.0', '1.0']`.

Korrektur: Positionsfreiheit aus der frischen Kontoantwort im endgültigen Entry-Guard berücksichtigen, auch beim Wiederanlauf. Nach bereits beobachteter Fremdposition keine neue Exposure senden. Das technisch verbleibende Rennen mit externen Akteuren nach der letzten Börsenabfrage lässt sich nicht allein lokal vollständig ausschließen und ist kein zusätzlicher Befund.

### 24 — Rekursive native Warteschlange

Der native Host ruft für eine während des laufenden Turns gepufferte Nachricht unmittelbar `_run_agent()` auf. Der Aqla-Handler wird nicht erneut betreten. Zuvor löscht der reguläre native Finalizer über `on_session_end` den aktiven Aqla-Kontext.

Gegenprobe: `.cache/recheck_native_pending_drain.py`, echte Methode `_run_agent_queued_followup()` unter dem echten Aqla-Wrapper. Ergebnis: `event_kind: source`, `active_context: null`, `interpret_hook: block`, `forwarded_ingests_before_agent: 0`.

Korrektur: Herkunft und idempotenten Forward-Ingest an jeder tatsächlichen nativen Agent-Turn-Grenze binden, einschließlich rekursiver Folge-Turns. Laufenden Owner-Turn mit eintreffendem Forward, Owner-Folgeauftrag und internem Wakeup prüfen. Quellensteuerungsrechte weiter gesperrt halten. Der normale nachgewiesene Fehler ist fehlende Autorisierung beziehungsweise verlorener Ingest; eine Privilegieneskalation wurde nicht bestätigt.

### 25 — Native Cancellation wartet auf laufenden Turn

Der Aqla-Handler hält `handler_lock` über den gesamten Agentlauf. Der native `/stop`-Pfad wartet erst auf eine Antwort aus genau diesem Handler, bevor er `cancel_session_processing` ausführt. Bei blockiertem Agentlauf erreicht `/stop` die Cancellation daher nicht.

Gegenprobe: `.cache/recheck_native_stop_lock.py`, echte native Methoden `_dispatch_active_session_command()` und `_dispatch_inline_reply()`. Während blockiertem Lauf: `stop_completed_while_run_blocked: false`, `native_cancel_reached: false`. Erst nach Freigabe des ursprünglichen Laufs wird Cancellation erreicht.

Korrektur: Authentifizierte native Abbruchbefehle unabhängig vom vollständigen Agent-Turn durchlassen. Herkunft anderer laufender Toolaufrufe darf dabei nicht durch globale Operatorumschaltung überschrieben werden. Kontextlösung gemeinsam mit Befund 24 prüfen; getrennte Regression für blockierten Lauf plus `/stop` erhalten.

### 26 — Neuer Adapter nach Reconnect

Der echte native Reconnect erzeugt und installiert einen neuen Adapter mit Standardhandlern. `attach()` prüft nur `self.started` und beendet sich auch bei einem neuen Adapter. Dadurch fehlen auf diesem Adapter Forward-Ingest und authentifizierte Turn-Kontexte.

Gegenprobe: `.cache/recheck_native_reconnect.py`, echte `_install_reconnected_adapter()` und echter Inbound-Hook. Ergebnis: vor Reconnect `operator`, danach Kontext `null`, `replacement_wrapped: false`, `forwarded_ingests: 0`.

Korrektur: Worker-Lifecycle vom Adapter-Lifecycle trennen, neuen Adapter idempotent anbinden und alte Handler aufräumen. Reader-/Wake-/Sheets-Worker nicht mehrfach starten. Adapterwechsel mit nachfolgendem Ownerauftrag und Forward in die native Prüfung aufnehmen.

### 27 — Kanalbezogener RPC-Fehler

Zwei abonnierte Quellen sind weiterhin in `iter_dialogs()` vorhanden. Der erste Kanal wirft beim Nachrichtenabruf den echten Telethon-Typ `ChannelPrivateError`; der zweite ist lesbar. Die Exception verlässt die gemeinsame Schleife, und der Worker beendet den ganzen Reader.

Gegenprobe: `.cache/recheck_reader_source_isolation.py`. Zwei Synchronisationsversuche ergeben `queried_sources: ['gone', 'gone']`, `good_was_read: false`, `status_error: null`. Der vorhandene Test für vollständig fehlende Dialoge prüft einen anderen Fall und besteht weiterhin.

Korrektur: Kanalbezogene Fehler beim Initialabruf, Revisionsreread und Backfill isolieren, sicher mit betroffener Quelle melden und andere Kanäle synchronisieren. Innerhalb des fehlerhaften Kanals Reihenfolge und Cursor erhalten. Globale Session-/Authentifizierungsfehler weiter global behandeln; dauerhafte Wakeups und Börsenschutz nicht blockieren.

### 28 — Collateralvertrag

Die [offizielle HIP-3-Spezifikation](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals) erlaubt andere Quote-/Collateralassets. Die [offizielle Metadatenantwort](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) enthält `collateralToken`. Öffentliche Metadaten bestätigten beispielsweise Tokenindex `360 = USDH`; Aqla berücksichtigt dieses Feld bei Perpetuals nicht und übernimmt den Modeldefault `quote_token = 'USDC'`.

Eine lokale `httpx.MockTransport`-Gegenprobe mit zulässigem gelistetem HIP-3-Markt, `collateralToken: 360`, passendem Mid und DEX-Index ergibt über den wirklichen `Hyperliquid.market()`-Pfad: `returned_quote: USDC`, `automatic_market_guard: accepted`.

Die öffentlich befragten `flx`-/`hyna`-Universen waren zum Prüfzeitpunkt delisted und wurden korrekt übersprungen. Es wird daher kein aktuell real ausgeführter alternativer Collateraltrade behauptet. Der dokumentierte zulässige Marktvertrag umgeht jedoch die explizite automatische USDC-Grenze, sobald ein solcher Markt gelistet ist.

Korrektur: Collateral aus echten Metadaten bestimmen; bei unbekanntem oder anderem Collateral die automatische USDC-Strecke verweigern. Vollständige rohe MCP-Funktionen dürfen solche Märkte weiterhin gemäß ihrem Vertrag bedienen. Keine ungeprüfte Währungsumrechnung und keine stillschweigende Änderung der Nutzerregeln einführen.

## Reihenfolge

21–23 zuerst als Handelsregressionen absichern und beheben. 24 und 25 gemeinsam an der nativen Turn-Grenze lösen; 26 mit einem echten Adapterwechsel verifizieren. 27 hält Kanalfehler lokal. 28 wahrt die bestehende USDC-Grenze. Danach vollständige Testmatrix, native Smokes und neue Gegenproben sowie Paket- und Dependency-Gates ausführen.

## Geprüfte, verworfene Verdachtsfälle und Grenzen

- Normale erste Owner-/Source-Turns, frühe Forwardklassifikation, Source-Slash-Sperre und getrennte Text-/Foto-/Album-/FIFO-Puffer funktionieren; Befund 24 betrifft das anschließende native Drain, nicht diese bereits korrigierten Puffer.
- Fehlende Dialoge werden isoliert, unbeachtete Livechats früh gefiltert und historische Bootstrap-IDs begrenzt wiederaufgenommen. Befund 27 betrifft RPC-Fehler trotz vorhandenem Dialog.
- Sheets-Revisionen, verlorene Antworten, RAW-Zellentext, Scope-/Digestprüfung, Pending-Pruning-Schutz und ausgewiesene Teilstatistiken bestehen die erneute Prüfung. Manuelles Sortieren generierter Zellen während des Abgleichs und die dokumentierten Scanbudgets sind Betriebsgrenzen.
- `Regeln.Konto` zeigt den Principal, die Sheets-Scopebindung das effektive Vaultziel. Ohne gegenteiligen Spaltenvertrag ist diese unterschiedliche Darstellung kein bestätigter Defekt.
- Ein nach dauerhaft gespeicherter Closeabsicht eintreffendes Edit ergibt angesichts bewusst eingefrorener Exitaktionen keinen eindeutigen zusätzlichen Vertragsverstoß.
- Beschädigte Gesprächsdateien werden absichtlich nicht als leere Historie geladen. Dieser explizite Fail-closed-Vertrag wurde nicht als neuer Implementierungsfehler gezählt.
- Es wurde keine erreichbare Überschreitung des Textlimits durch normale Telegram-Einzelnachrichten reproduziert.
- Keine vollständige formale Verifikation, Prüfung sämtlichen Hermes-/Dependency-Quellcodes, tatsächliche VPS-Dienstinstallation, reale Telegram-/Google-/Börsenausführung oder Evaluation eines echten Sprachmodells. Die allgemeinen Erkennungsfixtures bleiben eine separat mit Nutzerkonfiguration auszuführende Modellprüfung.

**Status: IDs 21–28 offen. Eine weitere Fertigmeldung ist erst nach deren Korrektur und erneuter Prüfung gerechtfertigt.**

Status nach Behebung am 03.10.2026: IDs 21–28 korrigiert und zusammen mit den vier Ergänzungen aus der Abschlussprüfung abgesichert. 498 zusammengeführte Tests sowie native Source-/Owner-/Lifecycle-Smokes bestanden. Der obige offene Status ist das historische Prüfergebnis vor Umsetzung.
