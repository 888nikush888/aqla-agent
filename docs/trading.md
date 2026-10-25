# Handelsregeln

Hermes interpretiert Quellen; Python prüft Originalbelege, Version, Kontobindung, Profil, Risiko, aktuelle Marktdaten und Schutz. Eine Quelle besitzt keine Betreiberautorität. Echte Mehrdeutigkeit bleibt gesperrt.

## Profile und Konten

Risiko-Sizing oder fester Positionswert, maximale Positionsgröße, Signal- oder Festhebel, Margin-Modus, Kostenreserve, Slippage und Signalfrist bleiben explizite Profilwerte. Ranggewichtung und Kontolimits begrenzen die Planung zusätzlich. Mainnet/Testnet sind eigenständige Kontexte; fremde oder manuelle Positionen werden nicht verändert.

Quellenränge F–S beruhen auf belegten vollständigen Setup-Ergebnissen in Netto-R. Ranking-Ausschlüsse entfernen keine Verluste aus der Finanzhistorie. Die Standardzuordnung führt C und besser auf Mainnet, D/E/F auf Testnet; fehlende Hyperliquid-Märkte werden abgelehnt. Es gibt keinen Börsenersatz.

Profiländerungen gelten für neue Planungen. Alte Trades behalten ihre Profile und Risiken. Ihre Verwaltung benutzt eigene ausdrückliche Aufträge und die gespeicherten Freigaben.

## Entries und Ausführung

Einzelentry, Entry-Zone und zwei ausdrückliche Entries verwenden ein gemeinsames Risiko-/Mengenbudget. Die Bereichsauswahl unterstützt near, far, midpoint und split. Nachrücken eines weiteren Einstiegs erfordert die vorhandenen Budget-, Frische-, Eigentums- und Schutzprüfungen. Teilfills derselben Order sind keine Doppelorders.

Ausdrücklich verzögerte Signale warten auf ihre belegte Bedingung. Die Signalfrist ist von einer Sekunde bis sieben Tage einstellbar; der Ausgangswert bleibt fünf Minuten. Sie beginnt am ursprünglichen Signalzeitpunkt und wird durch Wiederanlauf oder spätere Profiländerungen nicht verlängert. Auch bei einer langen Signalfrist müssen Konto, Kurs und Orderbuch vor der Ausführung aktuell bestätigt sein. Standard- und Maker-only-Ausführung bleiben getrennte Optionen für Entry und TP. Maker-only wird nicht stillschweigend in eine Takerorder umgewandelt.

Express erkennt ausschließlich qualifizierte eindeutige Vorlagen und verwendet denselben Interpretations- und Handelspfad. Harmlose Kopfzeilen schaffen keine zusätzliche Handelsautorität. Unbekannte Formen gehen an die normale Interpretation. Änderungen am qualifizierten Master müssen ausdrücklich nachgeprüft werden.

## Stop und Take Profit

Backup ergänzt ausschließlich fehlende Angaben. Seine Prozentwerte und Einheiten sind explizit: Kursbewegung oder ROE auf die ursprüngliche eigene Anfangsmargin vor Gebühren/Funding. Ein konfigurierter Backup-SL von 40 Prozent Marginverlust wird unter dem tatsächlich wirksamen Hebel berechnet. Ausdrückliche Quellwerte werden nicht durch Backup überschrieben.

TP-Verteilung unterstützt gleich, halbierend, benutzerdefiniert, adaptiv und erstes Ziel. Individuelle Gewichte müssen vollständig gültig sein. Break-even und TP-Leiter verwenden tatsächliche eigene Fills und die bestätigte Restmenge. Rundung, Börsenminimum und Richtung bleiben verbindlich.

Die zusätzliche Stop-Regel `tp2_to_tp1` zieht nach vollständig bestätigten eigenen TP1- und TP2-Fills den Stop der Restposition einmalig auf den beim TP1-Abschluss festgehaltenen Zielpreis. Bei TP1 erfolgt kein automatisches Nachziehen. Ein bereits engerer Stop bleibt erhalten. Wenn TP2 die Position vollständig schließt, wird kein neuer Stop angelegt. Die normale TP-Leiter behält ihre bisherigen Stufen; das Waverider-Endziel ist davon unabhängig.

Quellen-Updates können Stop, Break-even, Ziele, Reduktion, Schließung und Entry-Storno betreffen. Erlaubte Aktionen sind im Profil gespeichert; Stop-Ausweitung bleibt gesondert beschränkt. Zusammengesetzte Verwaltung benutzt den bestehenden dauerhaften Aktionsplan mit bestätigter Mengenbasis und Reihenfolge.

## Waverider

Waverider verwaltet nur in Signalrichtung einen begrenzten Flexanteil der eigenen Position. Eine Reduktion und der spätere Wiedereinstieg müssen nach Gebühren, Fundingreserve, Slippage, Börsenminimum und ursprünglichem Lebenszeit-Risikobudget zulässig sein. Gewinne oder Fundinggutschriften vergrößern dieses Budget nicht.

Laufzeitwahl: bis zur ursprünglichen Signalfrist oder bis zum gewählten Ziel, beispielsweise TP1. Das Ziel gilt bei bestätigter nativer ausführbarer Preisbeobachtung beziehungsweise tatsächlichem TP-Fill als erreicht. Zielerreichung ist deshalb nicht automatisch ein TP-Fill. Bis-Ziel verlängert keine ursprünglichen Entry-Fristen. Stop-Ausführung/Schließung beendet den Waverider; beendete Setups bleiben beendet. Ein Stop-Update oder Break-even beendet ihn nicht: ausstehende Entries werden abgeglichen, und neue Wellenaktionen warten auf die bestätigte Stop-Ersetzung. Bestätigte Stop-Edits erlauben ausschließlich eigene Waverider-Wiedereinstiege unter den unveränderten Prüfungen; unverarbeitete Quellenänderungen bleiben gesperrt.

Die Phasen und Versuchszähler beweisen keinen Wellenzyklus. Ein gemeldeter Zyklus benötigt zugehörige echte Reduktions- und Wiedereinstiegsfills. Profilkonfiguration, Qualifikation und ausdrückliche Betreiber-Livefreigabe werden getrennt dargestellt. Der fehlende historische 30er-Nachweis wird durch eine Betreiberfreigabe nicht nachträglich als bestanden ausgewiesen.

Pausieren verhindert neue optionale Wellenaktionen; schon begonnene Operationen werden mit ihren bisherigen Identitäten abgeglichen. Regulärer Schutz und die normale Positionsverwaltung bleiben aktiv.
