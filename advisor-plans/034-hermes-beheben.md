# Plan 034: Native Turn-, Abbruch-, Reconnect- und Readergrenzen korrigieren

Geplant auf `d5d6e602c814375501fe614a2f7fee5a7ead2495`, 03.10.2026. P1, Aufwand M, Risiko mittel. Expliziter Nutzerauftrag für alle Fehler. Abhängigkeiten: keine.

Status: abgeschlossen. Executorcommit `354ceaa`, zusammengeführt als `46ca621`; native Source-/Owner-/Lifecycle-Smokes am zusammengeführten Stand bestanden. Lifecycle ist dauerhaft in CI verankert.

## Auftrag und Scope

IDs 24–27 im isolierten Worktree `C:/Users/nikla/.codex/worktrees/fix-final-hermes/aqla agent` beheben. Scope: `src/aqla_agent/events.py`, `hermes_runtime.py`, `telegram.py`; `tests/test_hermes_integration.py`, `test_telegram.py`, native Smokeprogramme und neue native Lifecycle-Regressionsprogramme. Nur bei Bedarf die bestehende native CI-Smokesequenz erweitern; Reviewer informieren. Kein Trading-/API-/State-/Sheets-/Installercode. Gepinnten Hermes-Checkout nicht ändern.

Native Botkommunikation und AIAgent erhalten, kein neuer Bot/Agent/DB. Quellenrechte bleiben gesperrt; echte Operatorbefehle funktionieren. Werkzeugkontext an den tatsächlichen Turn binden, nicht ungeschützt global auf Owner umschalten. Master höchstens 8KiB. Worker jeweils einmal; kein zusätzlicher Dienst.

## Schritte

1. Driftcheck `git diff --stat d5d6e60..HEAD` muss leer sein. Die bisherigen Batches stempeln Herkunft korrekt; anschließend ruft der echte Host `GatewayTurnMixin._run_agent_queued_followup` direkt `_run_agent` auf, ohne Aqla-Adapterhandler. Finalizer löscht vorher Kontext. Herkunft und idempotenten Forward-Ingest an jeder echten nativen Turn-Grenze erneut setzen, einschließlich rekursiven Owner-/Forward-/internen Wake-Folgeturns. Thread-/Async-/Cachegrenzen beachten.
2. Aqla hält `handler_lock` über kompletten Agentlauf. Native `/stop`/`/new`/`/reset` müssen vorher denselben Handler für Antwort betreten, bevor Cancellation erfolgt. Operator-Abbruchpfad während laufendem/hängendem Turn erreichbar machen, ohne Sourceautorität zu erweitern oder den laufenden Toolkontext zu überschreiben.
3. `attach` kehrt bei `self.started` auch nach Adapterwechsel zurück. Workerstart vom aktuellen Adapter-Lifecycle trennen; neuen nativen Adapter idempotent anbinden, alte Handler aufräumen, keine doppelten Worker.
4. Reader isoliert fehlende Dialoge, nicht kanalbezogene RPC-Fehler beim Initialhistory-/Reread-/Backfillabruf. Zugriffsfehler pro Quelle sicher melden und gesunde Quellen weiter bearbeiten. Globale Session/Authprobleme global behandeln. Keine Cursorfortschritte für unzugestellte Nachrichten, keine historischen Signale als neue Entries; Reader-/Sheets-/Wake-Tasks unabhängig halten.

## Repros und Verifikation

Lese Root `.cache/recheck_native_pending_drain.py`, `recheck_native_stop_lock.py`, `recheck_native_reconnect.py`, `recheck_reader_source_isolation.py` unter `C:/Users/nikla/Downloads/aqla agent`. Als reguläre Schutztests mit echten gepinnten Methoden übernehmen: Forward während laufendem Ownerturn wird eingelesen und hat Sourceauth, Ownerfolge hat Operatorauth, interner Wake hat Sourceauth. `/stop` erreicht Cancellation vor Freigabe eines blockierten Runs. Reconnectadapter verarbeitet Owner und Forward. Fehlerhafte erste Quelle blockiert zweite nicht, für alle drei Abrufphasen; globale Authfehler bleiben erkennbar.

Root-Interpreter `.venv/Scripts/python.exe` mit Worktree-PYTHONPATH verwenden; pytest ohne Cache mit frischem Worktree-basetemp. Gezielte Tests und vollständige Suite müssen bestehen; Ruff check und git diff --check sauber. Native Referenz `C:/Users/nikla/Downloads/aqla agent/.cache/hermes-pinned`; safe Interpreter dort `.cache/hermes-safe-venv/Scripts/python.exe`. Native Session/Ingress-Smoke sowie Source- und Owner-Pipelines auf neuen ignorierten Zielen ausführen, MCP-Python absolut Root `.venv/Scripts/python.exe`, Worktree-PYTHONPATH für MCP setzen. Standardsmokes UND echte neue Lifecycleprüfungen müssen bestehen. Null reale Telegram-/Google-/Exchange-/Modellaktionen; geskriptetes lokales Modell ist erlaubt.

## Abschluss

Nur isolierten Worktree committen; kein Rootedit/Merge/Push. Reviewer pflegt Index und Veröffentlichung. STATUS COMPLETE/STOPPED; STEPS mit Testresultaten; FILES CHANGED; NOTES, Abweichungen und Grenzen. Bei fehlender passender nativer Turn-Grenze oder Scopebedarf Reviewer benachrichtigen; keine Ersatz-Agentenschleife und kein ungeprüfter Hermes-Pinwechsel.
