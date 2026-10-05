# Adaptive TP-Verteilung und Stop-Leiter

Die Preise kommen weiterhin aus dem Signal. `take_profit_allocation=adaptive`
verteilt die tatsächliche gefüllte Menge auf maximal fünf Signalziele:

| Vorhandene Ziele | Mengenanteile |
|---|---|
| 1 | 100 % |
| 2 | 50 / 50 % |
| 3 | 50 / 25 / 25 % |
| 4 | 50 / 25 / 12,5 / 12,5 % |
| 5 oder mehr | 50 / 25 / 12,5 / 7,5 / 5 % an TP1–TP5 |

Weitere Signalziele werden gespeichert, aber nicht gehandelt. Vorschauen nennen
die ursprüngliche Anzahl in `source_take_profit_count`. `first_target` nutzt
stattdessen 100 % an TP1. Alle Exit-Orders sind Reduce-only; der native SL wird
vorab zusammen mit dem Entry angelegt. Mengenrundung gibt den Rest an das letzte
Ziel, sodass keine Menge verloren geht. Börsenmindestgrößen gelten weiterhin.

`stop_management=tp_ladder` ersetzt die einzelne Einstandsregel:

- Nach vollständig ausgeführtem TP1: Stop auf tatsächlichen Entry-VWAP.
- Nach vollständig ausgeführten TP1–TP3: Stop auf den effektiven TP1-Preis.
- Nach vollständig ausgeführten TP1–TP4: Stop auf den effektiven TP2-Preis.

Nur nachgewiesene eigene Fills lösen die Schritte aus. Teilfills reichen nicht;
Stufen jenseits der vorhandenen Ziele entfallen. Ein bereits engerer Stop wird
nicht ausgeweitet. Preise erfüllter Ziele werden vor späteren Quellenupdates
gespeichert. Stopabsichten und Operationsidentitäten bleiben über Neustarts
stabil; unbestätigte Stop-Schritte werden zuerst abgeglichen. Der alte Stop bleibt
bis zur Bestätigung des Ersatzes bestehen. Wenn der neue Stop bereits vom
aktuellen Kurs überschritten ist, gilt die vorhandene Reduce-only-Schließung.

Telegram: „Profil Standard: adaptive TP-Verteilung und automatische TP-Stop-Leiter“
oder „Profil A: 100 Prozent an TP1“. Geldrisiko, Hebel und Margin ändern sich
dadurch nicht. Neue Profilwerte gelten nur für zukünftige Trades. Alte Profile
und gespeicherte Trades behalten über Standardwerte die bisherigen Regeln.
