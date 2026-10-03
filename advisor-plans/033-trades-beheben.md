# Plan 033: Handelsabsagen, Eventbindung, Positionsguard und Break-even korrigieren

Geplant auf `d5d6e602c814375501fe614a2f7fee5a7ead2495`, 03.10.2026. P1/P2, Aufwand M, Risiko mittel. Der Nutzer hat die Behebung aller offenen Fehler ausdrücklich beauftragt. Abhängigkeiten: keine; gemeinsame Integration mit Plan 035.

Status: abgeschlossen. Executorcommit `81aad06`, zusammengeführt als `d80346d`; gemeinsam mit den anderen Paketen durch 498 Tests und native Prüfungen bestätigt.

## Auftrag und Grenzen

Im isolierten Worktree `C:/Users/nikla/.codex/worktrees/fix-final-trades/aqla agent` IDs 21, 22, 23 und 29 implementieren. Python-Konventionen, `AqlaError`, atomaren JSON-Einzelwriter, permanente Sourcebindungen, gefrorene Regeln und konservative Unknown-Behandlung erhalten. Keine DB, Botanbindung, Features oder Refaktoren hinzufügen. Keine echten Orders, Schlüssel, Modelle oder externen Dienste verwenden.

Scope: `src/aqla_hyperliquid_mcp/trades.py`; nur `AccountSnapshot` in `models.py`; nur `account_snapshot` in `api.py`; vorhandene Handels-/Admission-/Update-/Recoverytests und neue passende Regressiondateien. Andere Bereiche in api.py/models.py gehören Plan 035. Native Dateien, State, Sheets, Docs und CI nicht ändern. Bei notwendiger Scopeerweiterung Reviewer informieren.

## Ausgangslage und Schritte

1. Driftcheck `git diff --stat d5d6e60..HEAD` muss leer sein. `trades.cancel_entry` gibt bei fehlender Entryoperation nur `not_sent` zurück; `close_position` gibt ohne Position `already_closed` zurück. Recovery nimmt den aktiven Trade später wieder auf. Nachweislich ungesendete Absage atomar terminal speichern, Risiko freigeben und dauerhaftes Dedupe behalten. `cancel_entry`, Close-Update und Betreiber-Close müssen Recovery stoppen. Gesendete/unklare Entries weiter abgleichen; finanzierte Teilfills schützen.
2. `record_interpretation` prüft nur gemeinsamen Chat und historische Eigenschaft des Ankerevents. Aktionsfähige Interpretation an tatsächlichen Ankerevent binden, historische Referenzen weiterhin als Kontext erlauben. Ausschließlich historische Referenzen unter unabhängigem Liveevent verhindern. Tests für Entry und Update sowie legitime Replies/Edits.
3. `api.account_snapshot` verwirft `assetPositions`; spätere frische Accountantworten können bereits Fremdposition zeigen, obwohl erster Check leer war. Frische Marktposition im AccountSnapshot erhalten und im endgültigen Admission-/Dispatch-/Recoveryguard prüfen. Unbekannte Beobachtung nicht still als Null behandeln. Bestehende Regeln, Accountbindung und Risiko-/Marginprüfung erhalten.
4. `refresh_trade` vergleicht Break-even mit ursprünglichem `split_targets(filled_quantity)`. Stattdessen fortgeschriebene aktuelle Budgets benutzen, konsumierte/noch vorgesehene Mengen und Null-/entfallene Ziele konsistent behandeln. Erfüllte Budgets nicht auffüllen.

## Nachweise und Done-Kriterien

Root-Scratchquellen dürfen gelesen werden: `C:/Users/nikla/Downloads/aqla agent/.cache/recheck-execution/test_recheck.py`, `.cache/final-execution-review/recheck_current_tp_budget.py`. Unerwünschte Assertions in reguläre Schutztests umwandeln; keine bloßen Implementationstests.

Regressionen: Crash nach Entryzulassung + jede Absagevariante + Recovery ergeben keine Entryorder; gültige gesendete Teilfills bleiben geschützt. Historisches Setup + unabhängiger Liveevent wird vor Order abgewiesen, legitimer Liveanker mit historischen Ergänzungen bleibt möglich. Fremdposition in späteren Accountantworten verhindert Entry und Recoveryversand. Entry 4,80 → Reduktion 50 % → Ziele 115/125 → TP1 1,20 gefüllt ergibt Stop auf Entry100, Rest1,20. Nullbudget allein löst keinen Einstand aus.

Tests mit Root-Interpreter und explizitem Worktree-PYTHONPATH: `$env:PYTHONPATH='<worktree>/src'; $env:AQLA_LOCAL_NETWORK_TESTS='1'; & 'C:/Users/nikla/Downloads/aqla agent/.venv/Scripts/python.exe' -m pytest -q -p no:cacheprovider --basetemp '<worktree>/.cache/qa'`. Gezielte Tests zuerst, anschließend gesamte Suite; Exit0. Ruff mit Root `ruff.exe check --no-cache src tests hermes_plugin scripts`; `git diff --check` muss leer sein. Kein Install notwendig; vorhandene unveränderte Dependencies nutzen.

## Abschluss

Nur eigenen Worktree committen, nicht Root, mergen oder pushen. Bei Brancherzeugung `codex/` verwenden. Reviewer pflegt Index und Docs. Bericht: STATUS COMPLETE/STOPPED; STEPS mit echten Ergebnissen; FILES CHANGED; NOTES einschließlich möglicher Abweichungen und nicht geprüfter Grenzen. Bei Scopekonflikt, unklarer aktueller Fremdposition oder nicht erfüllbaren Kriterien melden; keine unsichere Umgehung.
