# Qualität und Fehlerbehebung

Stand: 03.10.2026. Geprüft wurden Ausführung und Wiederanlauf, Quellenaufnahme und Herkunft, native Hermes-Anbindung, Deployment, Abhängigkeiten sowie Git-/Artefakt-Hygiene. Die Reviews waren lesend und wurden durch gezielte lokale Regressionstests ergänzt. Keine echten Exchange-, Telegram- oder Modellaktionen.

## Vollprüfung und abgeschlossene Korrekturen

Die Vollprüfung von Commit `d9f35d3b5a1f2660f61526397083d15b6435de84` ergab 20 Befunde trotz grüner Ausgangstests. Auf den anschließenden Behebungsauftrag wurden alle 20 korrigiert. Die IDs beziehen sich auf diesen historischen Bericht; sie sind keine Behauptung einer vollständigen formalen Verifikation.

| ID | Korrektur | Regression oder Gate |
|---|---|---|
| 01 | Herkunft vor nativen Batches erfassen; Forwards nach Nachrichtenidentität trennen | `native_hermes_smoke.py`: beide Textreihenfolgen, mehrere Forwards, Fotos, Mediengruppen und Queue |
| 02 | Quellen vor Gatewaydispatch die nativen Steuerungsrechte entziehen; Betreiberrechte erhalten | `test_hermes_integration.py`, nativer Befehls-/Ingress-Smoke |
| 03 | State, Pläne und Operationen an Principal, effektives Konto/Vault und Netzwerk binden | `test_account_bindings.py`, einschließlich konservativer Legacy-Migration |
| 04 | Aktuelle Zulassung atomar und nach Signer-/Account-awaits unmittelbar vor Versand prüfen | `test_execution_admission.py`, `test_execution.py` |
| 05 | Exakte Close-/Reduktionsaktion vor Versand atomar stagen; nur fehlende Operationen wiederaufnehmen | `test_recovery_regressions.py`: Crash vor Versand und bestehende unbekannte Operationen |
| 06 | Cancel nur mit passender Orderidentität und terminalem Börsenstatus abschließen | `test_execution.py`: offene Orders, Fremdorders und jede Batch-Leg |
| 07 | Slippage-Limit zur erlaubten Grenze hin runden | `test_rules.py`, `test_updates.py`: beide Seiten und Preisraster |
| 08 | Gemeinsame zeitbasierte Revisionsauswahl für Evidenz und Plan verwenden | `test_source_revisions.py`: verspätetes Edit und gleichzeitiger Widerspruch |
| 09 | Reader und dauerhafte Wakeups getrennt ausführen; sichere konkrete Fehlercodes melden | `test_hermes_integration.py`, `test_telegram.py`: blockierter Start, Ausfall und Wiederanlauf |
| 10 | Verschachtelte TWAP-Ablehnungen anhand des Aktionsvertrags auswerten; unbekannte Formen unklar lassen | `test_native_actions.py`, `test_execution.py` |
| 11 | Entfernte Quellen vor jedem ungesendeten Entry erneut prüfen; finanzierte Exits fortsetzen | `test_execution_admission.py`, einschließlich vertrauenswürdiger Owner-Forwards |
| 12 | Gespeicherte Updatebindung vor Kandidatensuche prüfen; erfülltes Update finalisieren | `test_updates.py`: verlorene Closeantwort und Abschluss während Recovery |
| 13 | Frisch eingefügte alte Nachrichten/Edits bis zum atomaren Journalenqueue vor Pruning schützen | `test_retention.py`: voller pending Bestand, Zustellung und anschließende Freigabe |
| 14 | Auch bei gleichem Digest die höchste Sheets-Revision speichern | `test_sheets_transport.py`: A1/A3/B2 und verlorene Antwort |
| 15 | Begrenzte historische Auswahl vor erster Zustellung speichern; nur Auswahl und neue Posts nachholen | `test_telegram.py`: unterbrochener Bootstrap und Legacy-Cutoff |
| 16 | Unabonnierte Liveereignisse früh filtern; nur betroffene Quelle synchronisieren; Duplikate ohne Statewrite beantworten | `test_telegram.py`, `test_retention.py`; periodischer Gesamtcheck bleibt erhalten |
| 17 | Schmale Sheets-Indizes, gezielte Zeilen und begrenzte Scans statt vollständiger historischer Texte | `test_sheets_transport.py`: Transportgrenzen, Teilstatistik, Sortierung und Manipulation |
| 18 | Tatsächliche native Hermes-Abhängigkeiten auditieren; minimales Telegram-/MCP-Profil und kompatible Overrides ausliefern | `audit_hermes_runtime.py`, gemeinsame Installationsvorgabe, native Smokes und CI |
| 19 | Unabhängige positive Wire-Vektoren für jeden Info-Vertrag sowie Null-/CLOID-/Boundaryfälle prüfen | `info_contract_vectors.json`, `test_info_wire_contracts.py`: 61 HTTP-Fälle, 14 ungültige Grenzen, Pflicht-/Extrafelder |
| 20 | Prüfscripte im VPS-ZIP ausliefern und den README-Lintcheck auf dessen extrahierten Inhalt ausführen | `verify_artifacts.py`, Paketjob |

## Behebung der Nach- und Abschlussprüfung

Die anschließenden unabhängigen Prüfungen von `d5d6e60` fanden zwölf zusätzliche Fälle. Auf den ausdrücklichen Behebungsauftrag sind auch die IDs 21–32 korrigiert. Die alten Berichte bleiben als historische Ausgangsnachweise erhalten.

| ID | Korrektur | Regression oder Gate |
|---|---|---|
| 21 | Nachweislich ungesendeten Entry atomar absagen, Risiko freigeben und Wiederanlauf/Dispatch dauerhaft sperren | `test_trade_guard_regressions.py`: Cancel/Close/Operator, Signer-await, Teilfills und unklare bereits vorbereitete Vorgänge |
| 22 | Aktionsfähige Interpretation an tatsächlichen Liveanker und dessen aktuelle Revision binden | `test_trade_guard_regressions.py`: historische-only Referenzen verweigert, Live-Reply und jüngstes Edit zulässig |
| 23 | Frische Marktposition aus Kontoantwort im letzten Entry-/Recoveryguard prüfen; fehlende Beobachtung bleibt unbekannt | `test_trade_guard_regressions.py`: spätere Fremdposition und fehlende Position vor Versand |
| 24 | Herkunft und Ingest für jeden tatsächlichen nativen Turn setzen; kopierte Workerrechte nach Finalizer/Abbruch widerrufen | `native_pipeline_smoke.py --mode lifecycle`, `test_hermes_integration.py` |
| 25 | Native Abbruchbefehle während laufendem Turn ohne gemeinsamen Laufzeitlock erreichen | Lifecycle-Smoke: echtes stop/new/reset, blockierter Worker behält Quellenrechte und verliert sie nach Abbruch |
| 26 | Adapterbindung unabhängig vom einmaligen Workerstart bei Reconnect erneuern | Lifecycle-Smoke und `test_hermes_integration.py`: tatsächlicher Adapterwechsel und keine doppelten Worker |
| 27 | Kanalbezogene RPC-Fehler lokal halten, gesunde Quellen weiter synchronisieren und globale Authfehler getrennt melden | `test_telegram.py`: History, Reread, Backfill und erfolgreiche Wiederaufnahme ohne übersprungene IDs |
| 28 | Collateral-ID aus Metadaten bestimmen; automatische Strecke nur mit bewiesenem USDC, Full-MCP bleibt vollständig | `test_api_regressions.py` und Recovery-Metadatenfälle in `test_trade_guard_regressions.py` |
| 29 | Break-even anhand aktueller positiver TP-Budgets und bewiesener eigener Fills auslösen | `test_updates.py`: Reduktion, Zielersatz, TP1-Fill sowie Zero/Null/fehlende Budgets |
| 30 | Isolierter SDK-Markethelper rundet nach Produkttyp statt Asset-ID-Schwelle | `test_api_regressions.py`: unabhängige native/Spot/HIP-3-Wirepreise, Öffnen/Schließen, beide Seiten |
| 31 | Explizite SDK-Scopekarte bindet Signatur, tatsächlichen Wire-Vault und dauerhaften Vorgang | `test_api_regressions.py`: alle 50 SDK-Dispatcher aus Vault-Konfiguration, unabhängige L1-Recovery, Legacy-/Manipulationsfälle |
| 32 | Gewünschte Subscriptions nach jedem Send-await bis ready abgleichen; Send an ursprünglichen Socket binden | `test_streams.py`: Hinzufügen/Entfernen beim Aufbau, Duplikate, Socketwechsel und verlorene Aktion ohne Wiederholung |

Der zusammengeführte Stand besteht lokal **498 Tests bei 80,79 Prozent Branch-Coverage**, ohne übersprungene Sockettests. Die separaten nativen Prüfungen bestehen mit dem tatsächlichen gepinnten Hermes unter Python 3.14: keine Sessiondatenbank, Betreiberbefehle erhalten, Quellenbefugnisse getrennt, beide stdio-MCPs und begrenzte Masterbearbeitung. Die neue Lifecycle-Prüfung verwendet einen tatsächlich wiederverwendeten AIAgent, native rekursive Folge-Turns, Executor-Kontextkopie, echte Toolhooks und Finalizer sowie Cancellation und Reconnect. Source-/Owner-Smokes verwenden je sechs, Lifecycle zehn lokale geskriptete Modellaufrufe. Keine tatsächlichen Exchange-Aktionen. Aqla- und native Hermes-Abhängigkeitsscans melden am 03.10.2026 keine bekannten Advisories; der Git-Historienscan meldet keine Secrets.

## Korrigierte Befunde

| Befund | Auswirkung | Korrektur und Nachweis |
|---|---|---|
| Positionsabfrage konnte einen später sichtbar werdenden eigenen TP-Fill als zusätzliche externe Reduktion verbuchen | Falscher Abschluss und Entfernung des Stops bei noch offener Position | Keine abgeleiteten Reduktionsbuchungen; widersprüchliche Abfragen behalten Schutz bis zum Fill-Abgleich. `test_fill_between_history_and_position_reads_keeps_live_protection` |
| Terminalstatus wurde vor dem Aufräumen eigener Exit-Orders gespeichert | Nach einem Absturz konnten alte Orders eine spätere Position beeinflussen | Dauerhafter Cleanup-Zustand hält Marktbesitz; exakte Orders werden vor Freigabe als beendet bestätigt. Wiederanlauf umfasst auch alte Terminaldatensätze. `test_crash_before_exit_cleanup_keeps_market_reserved_until_restart` |
| Historischer Cutoff fehlte bei Live-Edits und beim erneuten Abruf bekannter Nachrichten | Eine historische Kontextrevision konnte als neuer Entry erscheinen | Einheitliche Cutoff-Klassifikation für Backfill, Rereads und Live-Ereignisse. `test_historical_edits_stay_context_in_reread_and_live_paths` |
| Native Prüfkommandos vermischten Quell- und Deploymentverzeichnisse | Die dokumentierten Befehle waren nach Installation nicht ausführbar | README nutzt explizite `AQLA_SOURCE`- und `AQLA_DEPLOY`-Pfade |
| Source-Distribution konnte trotz ignoriertem QA-Verzeichnis eine dortige `.env.example` aufnehmen | Lokale Referenz-/Testdateien gelangten unbeabsichtigt ins Quellpaket | Explizite sdist-Dateiliste und Prüfung jedes Archivmitglieds über `scripts/verify_artifacts.py` |
| Native MCP-Prüfung löste den Linux-venv-Interpreter als Symlink auf | Der Systeminterpreter fand die getrennt installierten Aqla-Abhängigkeiten nicht | Absoluter Interpreterpfad ohne Symlinkauflösung; durch den tatsächlichen Linux-CI-Lauf aufgedeckt |

## Dauerhafte CI-Prüfungen

Beim tatsächlichen Debian-VPS-Start wurde zusätzlich eine außerhalb der Startup-Mixin liegende native Cron-/Housekeeping-Factory erkannt. Sie startete ungenutzte SQLite-Produkte trotz des Aqla-Profils. Das Profil deaktiviert nun auch diese Factory; native asynchrone Transport-/Sessionwatchdogs bleiben aktiv. Der native Smoke ruft die tatsächliche Factory auf und prüft die kompatible leere Thread-Shutdownstrecke. Die erzeugte systemd-Unit verwendet 90 Sekunden Stop-Frist und `KillMode=mixed`, damit zuerst der native Gateway seine MCP-Kinder geordnet beendet. Der Installer verwaltet die Dependencies; native Lazy-Installationen sind im Aqla-Launcher deaktiviert.

- Frozen-Installation, Lint, Tests und Branch-Coverage auf Python 3.12, 3.13 und 3.14. Die Pipeline verlangt mindestens 70 Prozent.
- API-Abdeckung und lokaler WebSocket-Test für Reconnect, Unsubscribe und verlorene Antworten.
- Tatsächlicher gepinnter Hermes-Checkout: Dateiverlauf ohne DB, native Befehle, Quellenautorität, stdio-MCP, Nutzerregeln und Masterbearbeitung. Modellantworten sind dabei lokal geskriptet.
- Checksum-gepinnter Gitleaks-Scanner über die Git-Historie, Verbot lokaler Secret-/Statepfade und pip-audit über gehashte, eingefrorene Aqla-/Telegram-/Sheets-Abhängigkeiten sowie die tatsächlich installierte separate Hermes-Laufzeit. GitHub-Actions sind auf Commit-SHAs gepinnt; Dependabot prüft deren Updates monatlich.
- Paketbuild, Archivinhalt, SHA-256, getrennte Wheel-Installation und Offline-Deploymentvorbereitung. CI-Berichte und geprüfte Pakete liegen als GitHub-Artefakte vor.

## Zusätzliche Vollprüfung vom 14.10.2026

Sieben weitere Befunde sind durch gemeinsame Korrekturen und Regressionen abgesichert:

- Hyperliquid-Fillhistorien bleiben zeitlich und nach Requests begrenzt; unvollständige Belege bleiben ausdrücklich unvollständig.
- Kontoantworten tragen den Beginn ihrer Abfrage als `observed_at`; langsamer Transport erneuert die Zehn-Sekunden-Frischegrenze nicht.
- Backup-ROE verwendet den tatsächlichen nativen Margin-Hebel, auch bei Lighters gerundeten Marginfraktionen. Gespeicherte und finalisierte Altpreise werden nicht umgeschrieben.
- Ein angenommener TP-Ersatz speichert die aktuelle Generation vor jeder Börsenänderung. Wiederanlauf verwendet dieselben Operations- und Orderidentitäten. Neuere Ziele verdrängen ältere ungesendete Generationen; unbekannte Vorgänge werden ausschließlich abgeglichen. Nur vor der ersten Operationspersistierung darf eine nachgewiesene eigene Teilreduktion die ungesendeten Mengen verkleinern.
- TP-Wiederaufnahme erhält einen bestätigten SL bei vorübergehenden Fehlern, respektiert spätere Stop-Anweisungen und manuelle Konflikte und beendet nach vollständigem eigenem Exit die nicht mehr anwendbare Absicht. Quellenstatus folgt den gespeicherten Managementfakten; ein neuer Signalentry wird dadurch nicht erneut freigegeben.
- Benachrichtigungen mit erhaltenen opaque Alt-Ereignissen rendern ohne erfundene Quellzuordnung. Dynamische Anhangsmarker werden vor der nativen Telegram-Auswertung sichtbar neutralisiert; bewusst erzeugte Betreiberanhänge bleiben möglich.
- Interpretationsgültigkeit wird nach positivem `source_record` getrennt vom späteren Anwendungserfolg gemessen. Hybrid-/Decision-Verarbeitung bleibt im produktiven Legacy-Modus inaktiv.

`tests/test_audit_recovery_fixes.py` prüft echte lokale Adapter, SQLite-Neustart, verlorene Antworten, Zielgenerationen, spätere Reduktion/Stop-Anweisung, manuelle Exposure, vollständigen SL-Exit und den öffentlichen Status-/Metrikpfad. Der native Nachrichtentest enthält nun einen tatsächlich vorhandenen harmlosen Anhangspfad und die nativen globalen Anhangsmarker.

## Grenzen

Die Prüfung ist kein formaler Beweis aller Zustandsübergänge, kein vollständiges Audit der Hermes-Abhängigkeiten und kein Live-Trading-Test. Ein sauberer Advisory-Scan bedeutet, dass zum Scanzeitpunkt keine bekannten Advisories für die geprüften Versionen gemeldet wurden. Sprachliche Signalerkennung muss mit dem tatsächlich gewählten Modell über den beiliegenden Eval-Runner geprüft werden. VPS-Dienststart und echte Telegram-Zustellung sind getrennte Betriebsprüfungen.
