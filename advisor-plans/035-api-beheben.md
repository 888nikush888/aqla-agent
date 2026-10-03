# Plan 035: Collateral, Full-MCP-Preis-/Signaturscope und WS-Aufbau korrigieren

Geplant auf `d5d6e602c814375501fe614a2f7fee5a7ead2495`, 03.10.2026. P2, Aufwand M, Risiko mittel. Expliziter Behebungsauftrag. Gemeinsame Integration mit Plan033; Scopehunks getrennt halten.

Status: abgeschlossen. Executorcommit `8399988`, zusammengeführt als `b354ea2`; unabhängige Wire-/Signatur-/Subscription-Gegenproben und 498 gemeinsame Tests bestanden.

## Auftrag und Scope

Im isolierten Worktree `C:/Users/nikla/.codex/worktrees/fix-final-api/aqla agent` IDs 28, 30, 31 und 32 beheben. Scope: `api.py` außer AccountSnapshot/account_snapshot, `state.py` nur notwendige Operations-Scopevalidierung, `streams.py`; falls nötig nur Market in models.py; SDK-/Native-/Multisig-/Accountbinding-/Streamtests und neue passende API-Regressionsdateien. trades.py, AccountSnapshot, native Hermes, Sheets, Docs, CI nicht ändern. Scopebedarf vorher melden.

Full-MCP bewusst vollständig erhalten; automatische Strecke bleibt USDC-Perps. Keine DB, neuen Caches, Features, globalen SDK-Monkeypatches oder pauschalen Dependencyupdates. Unknown-Operationen niemals erneut senden. Principal, Netzwerk, effektives Konto und exakte Wire-Identitäten erhalten; historische Bindungen konservativ prüfen.

## Schritte

1. Driftcheck muss leer sein. Perp `markets` ignoriert `meta.collateralToken` und setzt Modeldefault USDC. Echtes Collateral bestimmen; unbekanntes/Nicht-USDC darf automatische `market`-Zulassung nicht passieren. Rohe Full-MCP-Funktionen müssen solche Märkte weiter bedienen. Basismarkt-USDC-Vertrag und vorhandene Legacydaten konservativ berücksichtigen; keine stillschweigende USDH/USDe-Umrechnung.
2. Native SDK `_slippage_price` klassifiziert jedes Asset>=10000 als Spot, also auch HIP-3-Perps>=100000. SDK-Facade-Helfer anhand Aqla-Produkttyp auf gültiges Raster bringen. Unabhängige native/Spot/HIP3 Wire-Vektoren, beide Seiten und market_open/market_close testen. Keine Änderung expliziter Nutzer-Slippage.
3. SDK `set_referrer` und weitere Principal-L1-Aktionen signieren mit vault=None, Capture schickt pauschal konfiguriertes Vault. Aktionstyp/Signier-Scope explizit zuordnen, Signatur/Wirepayload/durable Operationbindung konsistent machen oder unzulässigen Scope vor Versand verweigern. Alle betroffenen SDK-Aktionen prüfen, nicht nur Referrer. Multisig-/User-Signed-/Vault-Tradingpfade erhalten. Keine blinde Re-Signatur oder Aufweichung der Accountbindung.
4. Streams kopiert gewünschte Subscriptions vor awaits; während `ready=False` hinzugefügte/entfernte Wünsche erreichen Socket nicht. Aufbau und gewünschten Zustand synchronisieren, keine Duplikate, Hinzufügen/Entfernen beim Resubscribe testen. Exchange-Writes bei Reconnect weiter nie wiederholen.

## Nachweise und Done-Kriterien

Root `.cache/final-api-audit-repro.py`, `final-stream-audit-repro.py` lesen. Früherer Collateralnachweis: aktiver lokaler HIP3-Metavertrag collateralToken360 wurde als USDC akzeptiert; echte Tokenmetadaten unterscheiden USDH. Regression für 0/anderes/unbekanntes Collateral und weiterhin funktionierenden Full-MCP-Pfad. HIP3 Mid0.0123, szDecimals2: p0.012915 muss durch gültigen Perppreis ersetzt werden. Unabhängige Signatur-Recovery anhand tatsächlich gesendetem Payload muss bei Vaultkonfiguration dieselbe Wallet ergeben; betroffene Principal-Aktionen signieren null-Vault oder werden vor Versand konkret abgewiesen. Neu gewünschte ETH-Subscription während pausiertem BTC-Send ist nach Verbindungsaufbau tatsächlich gesendet; entsprechendes Unsubscribe wirksam. Verlorener Actionpost bleibt genau einmal gesendet.

Root `.venv/Scripts/python.exe` mit Worktree-PYTHONPATH; pytest gezielt, danach gesamte Suite, frisches ignoriertes basetemp und AQLA_LOCAL_NETWORK_TESTS=1. Ruff check sowie git diff --check sauber, API check-coverage weiterhin vollständig. Unabhängige Signing-/Wiretests müssen auch Varianten abdecken, die bisherige Fakes ungeprüft akzeptierten. Primärquellen: gepinnter SDK im Root-venv, docs/API-Manifest; offizielle Hyperliquid Tick-/Signing-/HIP3-Verträge. Keine Live-Orders oder Nutzercredentials.

## Abschluss

Nur eigenen Worktree committen, kein Root/Merge/Push. Bei Brancherzeugung codex/ verwenden. Reviewer pflegt Docs/Index und veröffentlicht. STATUS COMPLETE/STOPPED; STEPS mit tatsächlichen Gates; FILES CHANGED; NOTES einschließlich Scopeentscheidungen. Bei Widerspruch zwischen konservativer Legacybindung und neuer Scopezuordnung Reviewer informieren; keine implizite Migration auf anderes Konto.
