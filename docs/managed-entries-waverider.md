# Zwei Entries, Börsenauswahl und Waverider

`entry_range_choice=split` teilt eine belegte Zone in zwei Limits an ihren
Grenzen. Ohne andere belegte Positionsgewichtung erhält jedes die halbe
Gesamtmenge. Zwei getrennt genannte ursprüngliche Orders verwenden
`entry=split` und genau zwei `entry_legs`; sie sind keine Range. Die einzelnen
Orderformen, Preise, ausdrücklichen Gewichte und Verzögerungen brauchen
zugeordnete Originalbelege. Mehrere unabhängige Setups, ein dritter Einstieg
oder ein unvollständiger Zusatzentry bleiben gesperrt.

Ein Setup reserviert gemeinsam Risiko, Margin und einen Kontomarkt. Der Plan
prüft beide Entries und ihre TP-Mengen gegen native Mindestgrößen. Er erhöht
keine Menge, um ein Minimum zu erreichen, und lässt keinen Entry verschwinden.
Die Ausführung aktiviert den nächsten Entry erst nach geklärter Ausführung
und bestätigtem Schutz der vorhandenen eigenen Position. Ein Teilfill beendet
den Rest dieses Legs. Die übrigen ursprünglich geplanten Entries behalten
ihre eigenen Identitäten.

Eine vor dem Versand belegbar gescheiterte Hebelkonfiguration nutzt das
vorhandene begrenzte Wiederaufnahmebudget. Alte signierte Versuche bleiben
unverändert erhalten; neue Identitäten entstehen nur bei belegtem
`not_sent`, ohne Entry-Fills oder Entry-Operationen. Unbekannte Sendungen
werden weiterhin exakt abgeglichen und nie als ungesendet wiederholt.

Bei ausdrücklich konfigurierter adaptiver TP-Verteilung wird dieselbe native
Mindestgrößenregel auch auf jede Teilposition angewendet. Beide Entries
verwenden den längsten gemeinsamen ausführbaren Zielpräfix. Das gilt ebenso
für eine spätere Mengenbegrenzung beim Börsenwechsel. Andere konfigurierte
TP-Verteilungen werden dabei nicht in adaptive Verteilungen geändert.

Bestehende gespeicherte Pläne behalten ihren bisherigen Ablauf. Neue Profile
verwenden `split`; bestehende Profile behalten ihren ausdrücklich gespeicherten
Wert, bis der Betreiber ihn ändert. Ein Quellen-Edit setzt weder Frist noch
Versuchszähler zurück und eröffnet keinen weiteren Trade.

## Preis und Börse

Limitpreise sind bindend. Bessere Fills bleiben zulässig, wenn Stop, Ziele und
Risiko passen. Ausdrücklich verzögerte Entries behalten ihre Auslösebedingung.
Ein bereits erreichter absoluter erster TP oder eine Stop-Invalidierung
verhindert einen späten Einstieg. Vor dem ersten tatsächlichen Fill ist eine
provisorische ROE-Zielberechnung kein Nachweis eines erreichten TP.

Der Router vergleicht konfigurierte, freigegebene Alternativen desselben
Netzwerks anhand der innerhalb des Limits ausführbaren ersten Entry-Menge.
Danach entscheiden Preisvorteil und konfigurierte Reihenfolge. Best Bid/Ask
ohne Mengen belegt keine beliebig große Liquidität. Fehlende Daten und nicht
ausgewählte Vergleichspläne verleihen keine Ausführungsfreigabe.

Ein bereits wartendes Setup darf einmal wechseln. Dafür müssen alle alten
Entries und Schutzkinder nachweislich beendet sein, sämtliche Operationen
geklärt und Position sowie Entry-Fills null sein. Ein Checkpoint hält die
Übergabe auch über Neustarts fest. Neue Menge und Frist überschreiten die
ursprünglichen Grenzen nicht; das Zielkonto prüft seine eigenen Limits.
Ein Fill während des Stornos beendet den Wechsel. Nach einem Fill findet
keine Übertragung auf eine andere Börse statt.

Native Buchereignisse wecken gezielte Verwaltung im vorhandenen Backend.
Sie sind keine Autorisierung: Vor neuem Versand werden Konto, Quelle,
Schutz, Budgets und frische Preise erneut geprüft. Buchbeobachtungen enthalten
keine erfundene Mark-/Stopreferenz; finanzielle Guards lesen diese gesondert.
Die bestehende Zwei-Sekunden-Grenze bleibt erhalten.

## Profiloption Waverider

Die Profilparameter sind:

| Feld | Bedeutung | Vorgabe |
| --- | --- | --- |
| `management_mode` | `standard` oder `waverider` | `standard` |
| `waverider_flex_percent` | Flex-Anteil je tatsächlich ursprünglichem Fill | `50` |
| `waverider_until_tp` | TP-Nummer 1–20 oder `last`; höchstens vorhandenes letztes TP | `1` |
| `waverider_lifetime` | `until_target`: Wellen bis zum gewählten TP; `signal_deadline`: zusätzlich an ursprünglicher Entry-Frist beenden | `signal_deadline` (Bestandskompatibilität) |

Kern und Flex werden bereits nach dem ersten Fill getrennt geführt. Long
wird ausschließlich reduziert und günstiger zurückgekauft, Short reduziert
und teurer wieder aufgebaut. Der Preisvorteil muss nach Gebühren und
Ausführungskosten bestehen. Ohne geeigneten Rücklauf bleibt der verkaufte
Teil außerhalb des Marktes. Es entstehen keine Gegenpositionen.

Die deterministischen Startregeln verwenden geschlossene Minutenkerzen,
ATR(14), die tatsächliche Kontogebühr und ausführbare Buchtiefe. Eine günstige
Bewegung muss mindestens ein ATR beziehungsweise das Doppelte konservativer
Zykluskosten erreichen. Die Umkehr benötigt mindestens ein halbes ATR oder
zwei Ticks und drei unterschiedliche Beobachtungen über mindestens zwei
Sekunden. Die Regeln benötigen keine zusätzlichen Modellaufrufe.

Ein Setup erhält ein durchgehendes Verlustbudget. Verluste, Gebühren und
belastendes Funding werden berücksichtigt; Gewinne erhöhen das Budget nicht.
Rückkäufe bleiben auf belegte vorherige Flex-Verkäufe, verbleibende Rechte,
ursprüngliche Mengenobergrenze und die aktuellen Kontolimits begrenzt.
Reguläre Exits reduzieren die Rückkaufrechte. Der wirksame Stop wird niemals
gelockert. Quelle und Betreiber behalten Vorrang vor optionalen Wellen.

Optionale Gewinnmitnahmen klären die betroffenen normalen TP-Orders vor der
Reduktion und stellen passende Ziele für die tatsächlich verbleibende Menge
wieder her. Der Stop bleibt bestehen. Ein unbekannter Versand wird anhand
seiner ursprünglichen Identität abgeglichen; er erzeugt keine Ersatzorder.
Auch ein Maker-Wellenauftrag darf keine unbegrenzt wartende Restmenge halten.

Mit `waverider_lifetime=until_target` bleiben Wellen nach dem ersten echten
Entry bis zum gewählten TP aktiv, standardmäßig TP1. Ein bestätigter Teilfill
dieses TP oder eine beobachtete Berührung beendet Wellen dauerhaft. Die
ursprüngliche Signalfrist storniert weiterhin alle ungefüllten ursprünglichen
Entries; sie verlängert keine Entry-Zone. Nur belegte Flex-Verkäufe dürfen
danach innerhalb desselben Budgets wieder aufgebaut werden. Vorübergehende
Lesefehler erhalten je Wellenauftrag höchstens fünf Versuche in fünf Minuten;
ein erschöpfter, nachweislich ungesendeter Auftrag wird verworfen.

`signal_deadline` behält das bisherige zusätzliche Zeitlimit. Stop oder eine
übergeordnete Schließung beenden beide Modi. Bereits beendete Waverider werden
durch eine Profiländerung nicht wieder aufgenommen. Es gibt keinen erzwungenen
Wiederaufbau. Bestehende Positionen behalten ihre normale Absicherung.

## Shadow und Qualitätsfreigabe

Die Profilwahl allein gibt keine echten Wellenorders frei. Ohne passende
Qualifikation zeigt Waverider **Shadow** beziehungsweise einen konkreten
fehlenden Beleg. Fehlende Kontogebühren, Kerzen oder Tiefe pausieren Wellen;
normale Position und Schutz werden weiter verwaltet. Ein nicht unterstützter
Adapter darf fehlende Gebühren niemals als null behandeln.

Die Beobachtungen liegen separat in `waverider-replay.sqlite3` neben dem
Finanzzustand. Die Datei enthält private Setup-Metadaten und gehört nicht
ins öffentliche Repository. Markt-Ticks werden nicht in die finanzielle
JSON-Struktur kopiert. Nur relevante Zustandswechsel und Checkpoints werden
im Trade gespeichert. Die private Beobachtungsdatei ist auf 512 MiB begrenzt.

Der wiederverwendbare Offline-Lauf ist:

```text
aqla-evaluate-waverider --dataset PRIVATE.json --output PRIVATE-report.json
aqla-evaluate-waverider --from-replay waverider-replay.sqlite3 --output PRIVATE-report.json
```

Er konstruiert keinen Börsentransport. Er vergleicht dieselben zeitlich
geordneten, belegten Preis-/Tiefensnapshots mit Standardverwaltung und
Waverider. Kosten, mindestens eine Sekunde Ausführungslatenz und ein Abschlag
auf sichtbare Tiefe werden berücksichtigt. Kerzen allein qualifizieren
keine Ausführung. Geldwerte verschiedener Börsen/Netze/Währungen werden
getrennt verglichen und nicht stillschweigend addiert.

### Gebühren, Funding und ein aktuelles Split-Profil im Replay

Ein privater Datensatz kann `target_parameters` mit den aktuellen vollständigen
Profilparametern enthalten. Dann wird jeder Fall aus seiner ursprünglichen
`interpretation` mit dem Produktionsplaner neu berechnet. Das Replay verwendet
das unveränderte ursprüngliche Risiko- und Mengenbudget und verlängert keine
Frist. Die daraus abgeleitete Budgetgrenze ist kein historischer Kontostand.
Beide Vergleichswege verwenden denselben neu berechneten Plan; die tatsächlichen
Fills eines alten Ein-Entry-Plans werden nicht als hypothetische Split-Fills
übernommen. Eine Ablehnung durch den Produktionsplaner bleibt sichtbar und
wird nicht als Performance-Fall mit null Gewinn ausgegeben.

Native Daten brauchen eine durchgehende Beobachtungsreihe bis zur ursprünglichen
Frist, weiterhin mit höchstens zwei Sekunden alten Preisbelegen. Eine
`fee_schedule` bindet Taker-Raten an Börse, Netz, Konto, Markt, Quote-Währung und
ihren belegten Gültigkeitszeitraum. Aktuelle Kontosnapshots dürfen nicht in die
Vergangenheit datiert werden; Maker-Fillgebühren ersetzen keine Taker-Raten.
Der Beobachtungsexport erhält Gebührenbelege und ihre Zeitgrenzen. Die native
Hyperliquid-Kontogebühr berücksichtigt den aktiven Referral-Rabatt einmal.

`funding` enthält tatsächliche native Abrechnungen mit `time`, `rate`,
`reference_price`, `reference_kind`, `market`, `exchange`, `network`, `currency`
und `settled=true`. Für Hyperliquid ist die Preisbasis `oracle`, für Aster
`mark`. `funding_coverage` dokumentiert Start, Ende, die Herkunft
`native_settled_funding_history` und die Prüfsumme des Originalbelegs. Die
stündlichen Hyperliquid-Abrechnungen müssen vollständig sein. Nur eine Rate
ohne belegten Abrechnungspreis ist noch kein vollständiger Funding-Beleg.

Abrechnungen und ursprüngliche Fills werden zeitlich zusammengeführt. Funding
gilt für die dann gehaltene Menge; eine unbewiesene Reihenfolge gleichzeitiger
Originalfills und Funding sperrt den Vergleich. Zahlungen gehen in das
Nettoergebnis ein. Belastendes Funding verbraucht zusätzlich das durchgehende
Verlustbudget; spätere Gutschriften füllen es nicht wieder auf.

Der Bericht unterscheidet `cases_requested`, vollständig ausgewertete `cases`,
fehlende Belege (`data_errors`), reguläre Planablehnungen
(`admission_rejections`) und kritische Fehler. Fehlende Setups bleiben im
Bericht sichtbar. Eine Freigabe benötigt alle angeforderten Fälle und das
Kostenmodell `fees-funding-profile-v2`; das Live-Gate prüft außerdem den
passenden Börsen-, Netzwerk- und Währungsbereich. Daten einer anderen Umgebung
oder ein nur umbenanntes altes Profil verleihen keine Live-Berechtigung.

Die Live-Schranke benötigt mindestens 30 getrennte Holdout-Setups und zehn
abgeschlossene Wellen, keine kritischen Fehler sowie mindestens gleichwertiges
Nettoergebnis und keinen höheren Drawdown. Entwicklungs- und Holdout-Familien
dürfen sich nicht überschneiden. Synthetische Daten können diese Freigabe
nicht erzeugen. Eine qualifizierte Datei benötigt außerdem eine Signatur,
den Software-Hash, die passenden Profilparameter und eine geprüfte Börse.
Software- oder Parameteränderungen heben die passende Freigabe damit auf.

Ein Betreiber kann diese historische Qualitätsbedingung ausdrücklich aufheben.
Dafür gibt es eine getrennte, mit dem privaten Integrationsschlüssel signierte
Datei `waverider-operator-live-<profile_hash>.json` neben dem Finanzzustand.
Ihr Typ ist `waverider_operator_live_v1`; `historical_quality_waived=true`,
Betreiber, Zustimmungsprüfsumme, Ausstellungszeit, Software-/Profilhash sowie
die ausdrücklich erlaubten Konto- und Börsen-/Netz-/Währungsbereiche sind
verpflichtend. Nur danach neu angelegte Waverider-Pläne werden freigegeben.
Diese Betreiberfreigabe ist kein bestandener historischer Qualitätsnachweis.
Der Trade hält die Herkunft als `live_authority=operator` fest. Löschen der
Datei, ein Versionswechsel oder eine Profiländerung entzieht diese Freigabe.
Quelle, eigene Fill-Zuordnung, Kontolimits, SL, frische Preise, Gebühren,
Liquidität und ursprüngliches Verlustbudget werden weiterhin unverändert
geprüft. Die Berechtigung wird vor optionalen Positionsänderungen und erneut
vor dem Orderversand geprüft; ursprüngliche Schutzverwaltung bleibt erhalten.

## Journal und Betrieb

Ursprüngliche Entry-Fills bestimmen weiterhin die Quellen-ROE-Basis und die
ursprüngliche Initialmargin. Wiederholte Rückkäufe sind Umsatz, keine neue
Initialmargin. Das Journal hält alle eigenen Orders, Fills, Gebühren und
Funding-Vorgänge zusammen in einem Setup. Replaced Orders und beendete
Börsenversuche bleiben nachvollziehbar; Quellenbewertung zählt die Familie
einmal. Waverider verwendet das ursprüngliche freigegebene Risikobudget als
feste R-Basis.

Tests und Qualifikation verwenden isolierten Zustand und Fake-Börsen.
Bestehende Finanzdaten werden nicht zurückgesetzt. Ein Rückfall deaktiviert
neue Entries und Wellen, behält aber Schutz und Verwaltung vorhandener
Positionen. Alte Software darf keine aktiven Datensätze dieser neuen
Lifecycle-Version übernehmen.
