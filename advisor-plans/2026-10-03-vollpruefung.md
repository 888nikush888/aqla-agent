# Vollprüfung vom 03.10.2026

**Historisches Ergebnis für `d9f35d3`: Die vorhandenen Prüfungen bestehen, aber der Stand ist nicht fehlerfrei.** Es gibt reproduzierbare Fehler bei Herkunft/Berechtigungen, Entry-Zulassung, Kontobindung, Wiederanlauf und Journalzustellung. Der anschließende Behebungsauftrag hat alle 20 Befunde korrigiert; die [Korrekturzuordnung](../docs/quality-review.md) beschreibt den aktuellen Stand und seine Regressionstests. Die folgenden Befunde dokumentieren unverändert den Ausgangsstand.

Geprüfter Commit: `d9f35d3b5a1f2660f61526397083d15b6435de84`. Drei getrennte lesende Reviews plus unabhängige Prüfung der genannten Fundstellen. Keine Sourceänderungen, realen Orders, Telegram-Nachrichten oder Google-Zugriffe. Die vorhandene JSON-Datei als technische Wahrheit, automatische Perpetual-Strecke, native Hermes-Kommunikation und das lesende Sheets-Analyseprofil sind bewusste Produktentscheidungen.

## Bestehende Gates

- Erneuter [GitHub-Lauf](https://github.com/888nikush888/aqla-agent/actions/runs/37087335782): alle sieben Jobs bestanden. Tests auf Python 3.12/3.13/3.14, native Hermes-/MCP-Prüfungen, Securityjob und Paketbuild.
- Aktueller Bestand: 190 Tests; letzte vollständige lokale Branch-Coverage 77,91 Prozent. Der Ausführungsreview führte zusätzlich die 33 bestehenden Finanz-/Auth-/Recoverytests erfolgreich aus.
- Ruff, Repo-Dateihygiene und Gitleaks über alle drei bestehenden Commits bestanden; keine gefundenen Secrets.
- API-Namen-/Schemasprüfung: 59 Info-Verträge, 40 Exchange-Typen, 52 SDK-Operationen und 24 Subscriptions vorhanden. Das beweist noch nicht jede semantische Wire-Eigenschaft, siehe Befund 19.
- Aktueller Wheel-/sdist-/VPS-ZIP-Inhalt, SHA-256 und extrahierte Offline-Deploymentvorbereitung bestanden.
- Zusätzlicher Scan des tatsächlich installierten separaten Hermes-venvs: 110 Distributionen, 21 Advisory-Meldungen in vier Paketen. Manche Meldungen teilen eine CVE; die Zahl ist kein Zähler eindeutig erreichbarer Exploits. Siehe Befund 18.

## Priorisierte Befunde

Aufwand S = lokal begrenzte Korrektur plus Test, M = mehrere zusammenhängende Übergänge beziehungsweise native Integration. Risiko bezeichnet das Änderungsrisiko, nicht die Auswirkung des Fehlers. P1 = Sicherheits-/Ausführungs-/verlässliche Steuerung, P2 = Zustellungs-/Konsistenz-/Durchsatz-/Prüflücke, P3 = Dokumentation.

| ID | Prio | Befund und Auswirkung | Aufwand / Risiko | Konfidenz | Evidenz |
|---|---|---|---|---|---|
| 01 | P1 | Native Batches vermischen Betreiber- und weitergeleiteten Quelleninhalt; Metadaten des ersten Events können dem ganzen Batch Betreiberrechte geben. Mehrere Forwards verlieren außerdem ihre getrennten Originalidentitäten. | M / mittel | hoch | `events.py:144–146`; gepinnter `gateway/platforms/base.py:2570–2588`, `:3826–3841`; Telegramadapter `:6540` |
| 02 | P1 | Quellen behalten `allow_gateway_control=True`; weitergeleiteter Befehlsinhalt kann vor dem Agenten-/Toolhook native Gatewaysteuerung auslösen. | S / niedrig | hoch | `events.py:169–171`, `:206–214`; gepinnter `gateway/platforms/event.py:99`, `:125–127`; `run_inbound.py:1271–1287` |
| 03 | P1 | State und gespeicherte Pläne sind nicht an das effektive Vault-/Handelskonto gebunden. Ein geändertes Vaultziel kann einen alten Plan auf einem anderen Zielkonto ausführen. | S–M / mittel | hoch | `state.py:30`; `api.py:39`; `trades.py:192–216` |
| 04 | P1 | Pause, Regeln und Quellrevision werden vor mehreren awaits geprüft, bei endgültiger Zulassung aber nicht erneut. Änderungen während der Kontoprüfung verhindern den Entry nicht zuverlässig. | M / mittel | hoch | `trades.py:195–214`, `:226–232`; `server.py:170–187` |
| 05 | P1 | Close-/Reduktionsabsicht wird vor Versand nur als Metadaten/Status, nicht als exakte wiederaufnehmbare Aktion gespeichert. Ein Crash in dieser Lücke verliert den Close-Auftrag. | M / mittel | hoch | `trades.py:550–552`, `:393–395`, `:756` |
| 06 | P1 | Reconciliation eines Cancels erkennt auch eine noch offene Zielorder als gelöst. Beobachtung der Order wird mit Nachweis ihrer Beendigung verwechselt. | S / niedrig | hoch | `api.py:374–376` |
| 07 | P1 | Tickrundung erweitert die festgelegte maximale Entry-/Close-Slippage. Kaufobergrenzen werden nach oben, Verkaufsuntergrenzen nach unten gerundet. | S / niedrig | hoch | `rules.py:110`; `trades.py:547` |
| 08 | P1 | Evidenz nutzt Einfügereihenfolge, Planbindung nutzt Editzeit. Ein verspätetes älteres Edit kann alte Preise erlauben, während der Plan die neueste Revision behauptet. | S–M / mittel | hoch | `rules.py:57`; `trades.py:146–156` |
| 09 | P1 | Readerfehler verhindern im selben try auch Wakeups für bereits persistierte Arbeit und Schutzmeldungen. Eine unerreichbare Quelle kann damit unabhängige Arbeit blockieren. | S / niedrig | hoch | `events.py:105–124`; `telegram.py:55–57` |
| 10 | P1 | Dokumentierte verschachtelte TWAP-/TWAP-Cancel-Ablehnungen werden als angenommen gespeichert, solange kein Batch-`statuses`-Array vorliegt. | S / niedrig | hoch | `api.py:217–220`; offizielle Exchange-Response-Verträge |
| 11 | P2 | Entfernte abonnierte Quellen werden vor neuen, bereits vorbereiteten Entries nicht erneut zugelassen/geprüft. Ein alter unsent Plan kann nach Quellenentfernung noch handeln. | S / niedrig | Verhalten hoch, gewünschte Semantik mittel | `trades.py:28–32`; `server.py:178–182`; `trades.py:195–216` |
| 12 | P2 | Erfüllte Close-Updates mit zuvor verlorener Antwort können nach Tradeabschluss nicht mehr über ihre gespeicherte Bindung finalisiert werden. Kandidatensuche geschieht vor Dedupe; die Quelle bleibt pending. | S / niedrig–mittel | hoch | `trades.py:616–630` |
| 13 | P2 | Ein neu eingefügtes altes Kontext-/Editereignis kann vor Aufnahme in die Journalqueue pruned und tombstoned werden. Es wird nie nach Sheets exportiert. | S / niedrig | hoch | `trades.py:41–54`; `state.py:78` |
| 14 | P2 | Gleicher Zellinhalt wird bestätigt, ohne eine neuere `_revision` zu speichern. Ein verspäteter Zwischenstand kann danach den neueren Inhalt überschreiben. | S / niedrig | Adapterverhalten hoch, Produktionsreichweite mittel | `aqla_sheets_mcp/client.py:126–130` |
| 15 | P2 | Unterbrochener historischer Bootstrap mit Cursor 0 liest beim Retry die gesamte Kanalhistorie statt der begrenzten Kontextmenge ein. Sheets-Pending-Schutz verstärkt das Speicher-/Schreibaufkommen. | S–M / mittel | hoch | `server.py:210–213`; `telegram.py:66–77`; `trades.py:54` |
| 16 | P2 | Auch unsubscribed Live-Nachrichten lösen zunächst einen Backfill/Reread aller abonnierten Quellen aus. Unveränderte Ingests schreiben erneut den gesamten State. | S–M / niedrig–mittel | hoch | `telegram.py:86–92`, `:121–122`; `trades.py:35–46`; `state.py:71–81` |
| 17 | P2 | Bounded Sheets-Paging/kleine Writes laden zuvor den gesamten betroffenen Tab einschließlich aller historischen Texte. Transport-/Speicherkosten wachsen mit der Historie. | M / mittel | hoch | `aqla_sheets_mcp/client.py:89`, `:115`, `:157–171` |
| 18 | P2 | Die separate Hermes-Laufzeit enthält bekannte Advisory-Versionen, wird vom CI-Abhängigkeitsscan aber nicht erfasst. Ein grüner Securityjob deckt nur die Aqla-Auflösung ab. | M / mittel | hoch für Versionen/Prüflücke; Reachability unterschiedlich | `install.py:96–105`; `.github/workflows/quality.yml:76–79`; gepinntes Hermes `pyproject.toml:95`, `:402–403` |
| 19 | P2 | Die Info-Abdeckungsmatrix verweist für sämtliche Verträge auf einen Test, der überwiegend Objektstrenge und Anzahl prüft. Keine 59 unabhängigen Request-/Wire-Vektoren; Vollständigkeitschecker prüft Namen/Schemas, nicht alle Response-/Bounds-Semantiken. | M / niedrig | hoch | `tests/test_api_contracts.py:6–31`; `coverage.py:34–48`; `docs/api-coverage.md` |
| 20 | P3 | Der README-Gesamtcheck enthält `scripts`, das VPS-ZIP liefert diesen Ordner nicht. Der dokumentierte Check scheitert im extrahierten Quellpaket. | S / niedrig | hoch | `README.md:116`; `release.py:13–15`; `scripts/verify_artifacts.py:39–43` |

Alle unpräfixierten Pythonpfade in der Tabelle liegen unter `src/aqla_hyperliquid_mcp/` beziehungsweise `src/aqla_agent/`, wie der Dateiname eindeutig angibt. Pinned-Hermes-Evidenz stammt aus Commit `1a4508e2aff2db5f50409893a2115be777bd5643`; der Checkout wurde nicht verändert.

## Reproduktionsnachweise und Korrekturgrenzen

1. **Herkunft/Batches:** Native Textbatches verwenden den privaten Sessionkey und hängen Text an das erste Event. Die Herkunftserkennung wertet weiterhin dessen einzelne rohe Nachricht aus. Herkunft deshalb vor Text-/Photo-/Queue-Debounce erfassen und Forwards trennen. Tests mit tatsächlichen nativen Enqueue-/Flushpfaden in beiden Reihenfolgen und mehreren Forwards; Betreiberbefehle weiterhin erlauben.
2. **Native Steuerung:** Quellenereignisse müssen vor nativer Dispatchverarbeitung `allow_gateway_control=False` erhalten. Ein Toolhook allein schützt die frühere Gatewaysteuerung nicht. Originaltext nicht verändern; echte Operatorereignisse bleiben steuerungsberechtigt.
3. **Effektives Konto:** Pure-memory/MockTransport-Preview auf dem Hauptkonto, anschließend anderes Vaultziel: Planaccount und Dispatchaccount unterscheiden sich, der Trade öffnet trotzdem. Principal und Ziel persistent binden; vor Entry und Recovery beide validieren. Keine automatische Migration historischer Trades auf ein neues Konto.
4. **Admission-Rennen:** Pause während des account_snapshot-awaits: nach Ausführung bleibt `paused=True`, aber Position `4.8` und drei Mutationen sind vorhanden. Autorisierung im finalen Statecallback prüfen; noch nicht gesendete Entryabsichten nach weiteren awaits erneut validieren. Gleichartige Tests für Regelrevision und Edit.
5. **Close-Crash:** Direkt vor der Close-Mutation abbrechen: Recovery stellt `open` wieder her, Position `4.8` bleibt, keine Closeoperation und keine Korrekturmutation. Exakte Closeaktion und ID stagen; nur wirklich ungesendete Absichten wiederaufnehmen. Unsichere bestehende Operationen niemals erneut senden.
6. **Cancel-Proof:** Zielorder bleibt `open`, Reconciliation meldet trotzdem `resolved=True` und `reconciled`. Für Cancel müssen aktionsspezifische Terminalzustände nachgewiesen sein; identifizierbare aktive Orders sind kein Beendigungsnachweis.
7. **Slippage:** Mid `102.04`, Größenpräzision `5`, Cap `0.3%`: generierter Kauflimitpreis `102.4` erlaubt etwa `0.352803%`. Maximumkaufpreise nach innen abrunden, Minimumverkaufspreise nach innen aufrunden; finale Grenze explizit prüfen.
8. **Revisionen:** Neuere SL-95-Revision vor verspäteter älterer SL-90-Revision einlesen. Ein Plan akzeptiert SL 90, bindet aber den Quellhash für SL 95. Eine gemeinsame autoritative Revisionsauswahl für Evidenz und Plan verwenden; verschiedene Nachrichten weiterhin kombinierbar halten.
9. **Reader-Ausfall:** Readerstart oder Sync wirft vor `snapshot`. Bereits vorhandene Pending-Forwards und Notifications erreichen keine Wakeprüfung. Readerfehler isolieren, Snapshot/Wake trotzdem ausführen, konkrete Quellenstörung als Status melden.
10. **TWAP:** Pure Dekodierung der offiziellen HTTP-200-Fehlerantwort mit `response.data.status.error` ergibt derzeit `acknowledged/accepted`. Nach Aktionsvertrag dekodieren; unerkannte Formen unknown lassen. [Primärvertrag](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint).
11. **Quellenentfernung:** Preview, Quellenliste leeren, Execute: Entry wird geöffnet. Festlegen, dass Quellenentfernung neue unsent Exposure stoppt; legitime Owner-Forwards gesondert behandeln und bestehende finanzierte Trades weiter schützen.
12. **Erfülltes Update:** Full-Close mit verlorener Antwort, Recovery schließt Trade, erneute Verarbeitung derselben Quelle schlägt vor Bindungsprüfung als mehrdeutig fehl. Gespeicherte Bindung zuerst prüfen und erfüllte Quelle ohne neuen Versand terminalisieren.
13. **Queueverlust:** 100 pending Kontextnachrichten bei `max_events=100`; ein älteres historisches Ereignis einfügen. Ingest liefert context, danach fehlen Ereignis und Journalrow, während die Tombstone existiert. Frisch eingefügte Ereignisse bis zum atomaren Enqueue vor Pruning schützen.
14. **Revisionguard:** A/Revision 1, identisches A/Revision 3, danach anderes B/Revision 2: B überschreibt A. Bei identischem Digest mindestens die maximale beobachtete Revision erhalten. Der native Einzelworker ist seriell; Reordering/Restart/zusätzlicher autorisierter Caller sind die relevanten Randfälle.
15. **Bootstrap:** Seed erfolgreich, erste historische Lieferung fehlgeschlagen: Cursor bleibt 0, Wiederanlauf wechselt in unbegrenzten min_id-Backfill. Die begrenzte historische Auswahl beziehungsweise Untergrenze erhalten; echte neue IDs oberhalb des Cutoffs vollständig nachholen.
16. **Unabonnierte Ereignisse:** Früh filtern und Live-Abgleich auf den betroffenen Chat beschränken. Vollständigen periodischen Recoveryabgleich beibehalten. Keine Source-I/O für andere Chats verursachen.
17. **Sheetsdurchsatz:** Ein einzelner Update-/Pageaufruf fragt `A:last-column` des ganzen Tabs ab; Offset/Limit wirken erst lokal. IDs/Metadaten schmal lokalisieren, benötigte Zeilen gezielt laden; Filter/Statistik chunkweise auswerten. Stable-ID-, Sortierungs- und Lost-response-Schutz erhalten.
18. **Hermesdependency-Grenze:** CI-Log des Wiederholungslaufs bestätigt im Hermesvenv PyJWT `2.13.0`, PyNaCl `1.5.0`, httpx2/httpcore2 `2.7.0`; das Aqla-venv hat hiervon abweichende aktuellere Versionen. Den separat tatsächlich ausgelieferten Runtimegraph auditieren, kompatiblen Hermes-Pin beziehungsweise kontrollierte Dependencykorrektur mit nativen Tests prüfen. Kein ungeprüftes Update der bestehenden Nutzerinstallation.
19. **API-Testvertrag:** Request-/Response-/Boundary-Vektoren unabhängig von den generierten Modellen definieren. Sonst kann dieselbe falsche Annahme Modell und Test zugleich bestehen lassen. Der aktuelle Checker ist ein Namens-/Schemascheck, keine vollständige Live-API-Zertifizierung.
20. **ZIP-/Dokumentvertrag:** Hilfsscripte gezielt ausliefern oder Git-Checkout- und VPS-ZIP-Prüfkommandos unterscheiden. Die README-Prüfung auf dem tatsächlichen extrahierten Paket als Gate ausführen.

## Dependency-Advisories: Einordnung

Der zusätzliche Hermes-Scan meldete PyJWT 2.13.0 (13 Meldungen), PyNaCl 1.5.0 (2), httpcore2 2.7.0 (1), httpx2 2.7.0 (5). Doppelte Alias/CVE-Bezüge sind möglich. Beispiele aus der primären GitHub-Advisory-Datenbank:

- [HTTPX2 decompression amplification, high](https://github.com/advisories/GHSA-8xx6-hgc6-gc2m).
- [HTTPX2 SSE buffering, medium](https://github.com/advisories/GHSA-f2fp-rgf2-35cp), gepatcht ab 2.10.0.
- [PyJWT pre-verification parsing, medium](https://github.com/advisories/GHSA-42vr-xj54-vc7v).
- [libsodium point validation, medium](https://github.com/advisories/GHSA-mrfv-m5wm-5w6w).

Die Default-MCPs dieses Projekts verwenden stdio; Discord/Voice, Dashboard-JWT und entfernte HTTP-MCPs gehören nicht zum konfigurierten Standardprofil. Deshalb wird keine pauschale Ausnutzbarkeit aller Meldungen behauptet. Die installierten betroffenen Versionen und die CI-Prüflücke sind jedoch belegt. Für die vollständige Dependencyquelle fehlt noch eine Reachability-/Updateabnahme; bis dahin darf „Dependency-Scan sauber“ nur den Aqla-Graph bezeichnen.

## Bewusste Grenzen und verworfene Fehlinterpretationen

- Lokaler Einzelwriter, JSON/JSONL und File-Lock sind gewünschter Entwurf, kein Datenbankmangel. Sheets ist bewusst nicht die Ausführungs-/Nonce-Wahrheit.
- Automatische Perpetual-Strecke, fehlende Bild-OCR, PnL vor Funding und kein kompletter Hermes-Chatexport stehen ausdrücklich in den aktuellen Produktdokumenten; keine neuen Defekte daraus konstruiert.
- Bereits korrigierte initiale TP-Staginglücke, Late-fill-Doppelzählung, Cleanup-Reservierung, reguläres Cutoff-Handling und Skillgrößenprüfung wurden als vorhandene Schutzmaßnahmen anerkannt. Neue Befunde beziehen sich auf nicht getestete zusätzliche Randfälle.
- Synchronous Journalprojektion erhöht CPUkosten: bei 5.000 1-KB-Events ungefähr 147 ms statt 57 ms für einen reinen In-memory-Nonceupdate; mit 15.000 pending Auditrows 359 ms statt 249 ms. Das sind Orientierungsmessungen ohne Disk-/Produktionsnachweis, kein eigenständiger behaupteter Latenzvorfall.
- Nebenläufige uv-Cache-Save-Warnings in CI sind kein Test-/Buildfehler.

## Nicht bewiesen

Keine formale Verifikation aller möglichen Zustandsübergänge, vollständige Prüfung des gesamten Hermes-/Dependency-Quellcodes, reale Börsenausführung, Telegram-Zustellung, Google-REST/OAuth/Quota-Abnahme oder sprachliche Trefferqualität des gewünschten echten Modells. Die 30 Kontextfixtures sind eine vorhandene explizit auszuführende Modellprüfung, kein bereits bestandener Modelltest.

**Status bei Abschluss der ursprünglichen Prüfung: alle 20 Befunde offen.** Die lesende Prüfung selbst veränderte oder behob sie nicht. Die damalige grüne CI war ein Regressionbaseline-Nachweis für den existierenden Testbestand.

**Status nach dem ausdrücklichen Behebungsauftrag am 03.10.2026: alle 20 Befunde korrigiert.** Die zusammengeführten Regressionen bestehen: 396 Tests, 80,12 Prozent Branch-Coverage, tatsächliche native Hermes-Batch-/Befehls-/MCP-Prüfungen und getrennte Dependency-Audits ohne bekannte Advisories. Die [vollständige Zuordnung je Befund](../docs/quality-review.md) gehört zum aktuellen Auslieferungsstand. Reale Exchange-, Telegram-, Google- und Modellabnahme bleiben außerhalb dieser lokalen/CI-Prüfung.
