# Betrieb und Migration

Es gibt einen Handels-Writer über der lokalen SQLite-Datenbank. Dauerhafter Commit geht einem Börsenauftrag voraus. Unklare Sendeausgänge werden mit ihren vorhandenen Identitäten abgeglichen; aggressives Wiederholen ist keine Wiederherstellung.

## Lokale Daten

Handelsdaten, Order-/Fill-Identitäten, Gebühren, Funding, Kontobindungen, Nonces und eigene Mengen bleiben erhalten. Journalansichten lesen diese Belege direkt. Ersetzte eigene Orders und aus dem aktiven Quellenkontext entfernte Nachrichten werden für das lokale Journal archiviert. Anzeigewerte verleihen niemals Handelsautorität.

Hermes hält seine Gespräche separat in `hermes-home/state.db`. Frühere JSONL-Verläufe besitzen weiterhin einen überprüften Importweg. Private Exporte, Datenbanken, Zugänge und Laufzeitverzeichnisse gehören nicht in Git oder Auslieferungspakete.

## Umstellung auf das Telegram-Journal

1. Vorherigen Zustand und Konfiguration sichern und die Migration isoliert gegen dieselben finanziellen Belege prüfen.
2. Nur eine geprüfte, CI-grüne Softwareversion einspielen. Keinen zweiten Telegram-Empfänger und keinen zweiten Writer starten.
3. Alte externe Exportwarteschlangen samt Fingerabdrücken als historische Metadaten erhalten; nicht als zugestellt markieren. Die eigentlichen Handelsbelege werden nicht umgeschrieben.
4. Aktive externe Tabellenprozesse, Konfiguration, Zugänge und überholte Paketdateien entfernen. Das frühere externe Dokument bleibt unverändert außerhalb des Betriebs.
5. Native eigene Mengen und SL/TP, manuelle Positionen, Risiken, Journalwerte und mindestens zwei vollständige erfolgreiche Abgleichzyklen vergleichen.

Waverider-Freigaben sind an Software, Profil und Konto gebunden. Eine autorisierte Softwaremigration darf eine bestehende Zustimmung nur bei belegter unveränderter fachlicher Wirkung auf den neuen Softwarestand übertragen. Keine neuen Konten/Parameter aufnehmen, keine beendeten Waverider wieder starten und keine historische Qualifikation erfinden.

Bei Fehlern Software und passende Konfiguration zurücknehmen. Die fortgeschriebene Finanzdatenbank niemals gegen ein altes Backup austauschen. Einmalige Migrationen bleiben soweit nötig wiederholbar, ohne Trade-IDs oder Nonces zurückzusetzen.

## Störungen

Das Menü zeigt Zeitpunkt und Vollständigkeit der letzten Kontobeobachtung. Frische, Signer, Inventar, Ownership und Schutz werden vor Wirkungen weiterhin geprüft. Ein fehlender/alter Snapshot ist keine bestätigte Nullposition. Kurzfristiges `protecting` allein beweist keinen Schutzverlust; entscheidend sind native eigene Schutzorders.

Native Ausführung verwaltet bestehende eigene Positionen weiter. Eine neue Kontosperre wird nicht eigenständig durch einen Beobachter aufgehoben. Manuelle Positionen desselben Markts werden niemals einem alten geschlossenen Trade zugerechnet.

Meldungen, Faktenverarbeitung und bestätigte Telegram-Zustellung bleiben getrennte Zustände. Fehlgeschlagene Zustellungen haben begrenzte Wiederholungen. Lokale Verlaufsansichten bleiben auch bei nicht zugestellter Nachricht lesbar.
