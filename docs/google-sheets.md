# Google-Sheets-Journal

aqla enthält einen zweiten Python-MCP für Google Sheets. Der native Hermes-Gateway lädt ihn als `sheets`; ein eigener Hintergrundtask liefert automatisch die Geschäftsdaten. Das Modell muss keine Logzeilen schreiben. Ohne Konfiguration bleibt die Funktion deaktiviert, der MCP kann seinen Einrichtungsstatus melden.

## Welche Daten erscheinen?

| Tab | Inhalt |
|---|---|
| `aqla Trades` | Quelle, Markt, Richtung, Zustand, geplanter/tatsächlicher Entry, Mengen, SL/TPs, ursprüngliches und reserviertes Risiko, realisierte PnL und Gebühren |
| `aqla Signale` | Originaltexte und Captions, IDs, Replies/Edits, Kontextmarkierung, Klassifikation, Trade-Updates und Gründe/Ablehnungen |
| `aqla Orders` | Entry-, SL-, TP- und Close-Identitäten, Größen/Preise, beobachteter Börsenstatus und Order-ID |
| `aqla Fills` | Eigene tatsächlich beobachtete Ausführungen, Zeit, Menge, Preis und Gebühren-Token |
| `aqla Vorgänge` | Dauerhafte Ausführungs-ID, Nonce, Phase, Request und Ergebnis, einschließlich unklarer/abgelehnter Aufträge |
| `aqla Regeln` | Konto, Netzwerk, wirksame Nutzerregeln, Quellen und Pause |
| `aqla Ereignisse` | Zustandsänderungen mit Zeitpunkt, Sequenz und Vorher-/Nachher-Fakten |

Die automatisch erzeugten Tabs haben Filter und eine feste Kopfzeile. Die internen ID-/Versionsprüfungen verwenden versteckte Metadaten-Spalten. Eigene zusätzliche Tabs bleiben unberührt. Originaltexte mit `=` werden als Text geschrieben und nicht als Sheets-Formeln ausgeführt. Sehr große einzelne Felder werden mit sichtbarem Hinweis gekürzt; der technische Originalzustand bleibt lokal erhalten.

Für eigene Sortierungen Filteransichten oder zusätzliche Analyse-Tabs verwenden. Generierte Datenzellen, Kopfzeilen und Metadatenspalten während der Synchronisierung nicht manuell verschieben, überschreiben oder löschen. Die Tabelle dient der Auswertung; Änderungen dort steuern keine Trades.

## Warum bleibt state.json?

`data/state.json` sichert Order-IDs, Nonces, Regeln, Bindungen, Wiederanlauf und noch nicht bestätigte Journalübertragungen atomar. Sheets übernimmt die lesbare Analyse und Historie. Ein Google-Ausfall verzögert die Tabelle, ohne den Quellenleser, die Hermes-Wakeups oder den Börsenschutz anzuhalten. Es gibt keine zweite Analyse-Datei und keine Datenbank.

Die Übertragung erfolgt mindestens einmal mit stabilen Zeilen-IDs. Nach verlorener Antwort werden vorhandene IDs erneut gelesen, damit kein zweiter Datensatz angehängt wird. Bestätigungen beziehen sich auf den exakten Datenstand; spätere lokale Änderungen bleiben ausstehend. Noch nicht exportierte Signale werden nicht durch die lokale Aufbewahrungsgrenze gelöscht. Sheets-Zellen liefern keine Handelsberechtigung und verändern keine Regeln, Mengen oder Aufträge.

## Auf dem VPS einrichten

1. Eine eigene leere Google-Tabelle in deinem Google-Konto anlegen.
2. Im gewünschten Google-Cloud-Projekt die Google Sheets API aktivieren und ein Servicekonto erstellen. Dessen JSON-Zugang auf dem VPS als `/opt/aqla/secrets/google-service-account.secret` speichern; Dateirechte `600`, Besitzer `aqla`.
3. Die Tabelle für die `client_email` dieses Servicekontos als **Bearbeiter** freigeben. Zugang nur zu der gewünschten Tabelle geben. Kein Domain-wide Delegation und keine Drive-API sind erforderlich.
4. In `/opt/aqla/config.yaml` setzen:

   ```yaml
   google_sheets:
     enabled: true
     spreadsheet_id: "ID_AUS_DEM_GOOGLE_SHEETS_LINK"
     credentials_file: secrets/google-service-account.secret
     sync_interval_seconds: 30
     batch_size: 100
   ```

   Bei `https://docs.google.com/spreadsheets/d/ABC123.../edit` ist die ID der Teil zwischen `/d/` und `/edit`. Eine Tabelle je Instanz/Konto verwenden; das Journal bindet die Tabelle an Konto und Netzwerk.

5. Der Installer installiert das optionale `sheets`-Paket bereits im getrennten Aqla-venv. Bei einer manuellen Quellinstallation: `uv sync --frozen --extra sources --extra sheets`. Für einen bereits installierten VPS ausführen:

   ```sh
   sudo systemctl restart aqla-agent
   ```

   Beim ersten Abgleich entstehen die sieben Journal-Tabs automatisch; vorhandene Handelsdaten werden mit aufgenommen. Der Einstiegspunkt für einen separaten MCP-Client lautet `aqla-google-sheets-mcp --config config.yaml`.

Die Schlüsseldatei gehört nicht nach GitHub, ins Sheet, in Telegram oder in den Master-Skill. Der CI-Lauf braucht keine echten Google-Zugangsdaten.

## Über Hermes auswerten

- „Zeige mir den Stand des Sheets-Journals.“
- „Wie viele abgeschlossene Trades gab es je Quelle?“
- „Zeige die tatsächlichen Fills für diesen Trade.“
- „Vergleiche realisierte Ergebnisse nach Symbol und Richtung.“

`journal_status` zeigt den Tabellenlink und die verfügbare Struktur. `journal_read` liest begrenzte Seiten mit exakten Spaltenfiltern. `journal_statistics` berechnet Ergebnisse aus unveränderten abgeschlossenen Tradezeilen. Geänderte/inkonsistente Zeilen werden aus Statistikwerten ausgeschlossen und ausdrücklich gezählt. Der Hyperliquid-MCP zeigt in `status.journal` die ausstehenden Zeilen und den letzten bestätigten Abgleich.

Gewinnwerte sind **realisierte Ergebnisse vor Funding**. USDC-Gebühren werden aus eigenen Fills berücksichtigt; bei fehlender Gebühreninformation oder fremder Gebührenwährung bleibt Netto unbestimmt. Trefferquoten nennen nur bekannte abgeschlossene Ergebnisse. Ungeschlossene Positionen erhalten keine erfundene Mark-to-Market-PnL. Funding und die volle Hermes-Gesprächshistorie werden nicht gespiegelt.

## Prüfungen und API-Grenzen

Tests prüfen tatsächliche Journalprojektion, Gebühren/PnL, Queue-Wiederanlauf, veraltete Bestätigungen, verlorene Antworten, sortierte Zeilen, Source-Text als Literal, Kopfzeilen-/ID-Manipulation und getrennte Autorität. Native Hermes-Smokes entdecken und verwenden den neuen stdio-MCP. Reale Google-API-Zugriffe brauchen die oben eingerichtete Tabelle und wurden ohne Zugangsdaten nicht behauptet.

Die Übertragung bündelt Änderungen und begrenzt die Paketgröße. Google dokumentiert atomare Batch-Änderungen und Quoten pro Benutzer/Projekt; der Standardtakt bleibt deutlich unter den normalen Schreibquoten. Bei API-Fehlern bleiben Daten lokal ausstehend. Quellen: [Sheets API Limits](https://developers.google.com/workspace/sheets/api/limits), [Batch-Update](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/batchUpdate), [Servicekonto-Authentifizierung](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.service_account.html).
