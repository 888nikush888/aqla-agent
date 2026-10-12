# Python und Decision Models für die automatische Quellenverarbeitung

Die native Hermes-Erweiterung besitzt einen optionalen Python-Worker. Er liest den aktuellen Quellenkontext, lässt ein Decision Model typisierte Entscheidungen treffen, validiert Originalbelege und verwendet die vorhandenen MCP-Handelspfade. Preise und Zitate stammen aus Python-Kandidaten; das Decision Model besitzt keine ausführbaren Werkzeuge. Der Betreiberchat bleibt bei Hermes. Standard ist `inception/mercury-decide:free` über OpenRouter; generative Aufgaben verwenden OpenCode Go mit OpenRouter-Fallback.

## Konfiguration

In `hermes-home/config.yaml` unter `plugins.entries.aqla-events.settings`:

```yaml
source_processing:
  mode: shadow  # legacy, shadow, hybrid
  model: inception/mercury-decide:free
  key_env: OPENROUTER_API_KEY
  calibration_file: calibration/decision-source-v5.json
  timeout_seconds: 5
  max_context_characters: 60000
```

Den OpenRouter-Key ausschließlich in der geschützten Laufzeitumgebung setzen. Der HTTP-Client sendet ausschließlich an `https://openrouter.ai/api/v1/systemone`, ohne Weiterleitungen und ohne automatisch übernommene Proxy-Umgebung. Der generische Adapter verwendet den System-One-Vertrag für typisierte Fragen, keine Chat-Completions. Andere OpenRouter-Decision-Modelle werden über `model` gewählt. Die konkrete Modellrevision stammt aus OpenRouters Decision-Katalog (`/api/v1/models?output_modalities=decisions`); optional kann `model_revision` ausdrücklich festgesetzt werden. Kalibrierung und Cache berücksichtigen Modellkennung, konkrete Revision und Fragenversion. Ein unerwarteter Versionswechsel wird abgewiesen. Eine Offline-Prüfung friert die Revision für beide Datengruppen ein.

- `legacy` ist der Installationsstandard und die Rücknahmeoption.
- `shadow` bewertet Quellen in einem unabhängigen Worker zusätzlich, erzeugt aber keine Trades, Klassifikationsänderungen oder eigene Nachrichtenzustellung. Hermes bleibt für die Verarbeitung zuständig; die Anbieterantwort verzögert seine Quellen-Wakeups nicht.
- `hybrid` übernimmt den Ablauf in Python. Nur Quellen im validierten Kalibrierungsprofil verwenden Decision Model-Ergebnisse automatisch. Andere Quellen sowie unsichere/ungültige Entscheidungen verwenden einen kurzen, werkzeuglosen Hermes-Modellaufruf. Anbieter und Quoten-Fallback werden nativ aufgelöst; das Quellen-Gespräch wird nicht mitgesendet.

Eine gültige Kalibrierung benötigt separate Quellthreads für Kalibrierung und Holdout, mindestens 30 Fälle je Gruppe, vollständige Annotationen, alle sechs Nachrichtenklassen, Deutsch/Englisch/Russisch und die kritischen Reply-/Edit-/Prozent-/Dezimal-/Injection-/Mehrfachsetup-/Bericht-mit-Aktion-Fälle. Alle vorgesehenen Fälle müssen ausgewertet sein. Erforderlich sind mindestens 30 unabhängig akzeptierte Holdout-Gruppen, mindestens 95 % korrekte akzeptierte Fälle und null kritische Fehlentscheidungen einschließlich übersehener Handlungsanweisungen. Mindestens vier korrekt akzeptierte echte Signale und vier echte Handelsupdates prüfen den Aktionspfad. Jede freigegebene Quelle benötigt mindestens drei echte Kalibrierungs- und fünf echte Holdout-Gruppen; eine Nachrichtenart wird nur mit einem korrekten Kalibrierungsfall und zwei korrekten akzeptierten Holdout-Fällen erlaubt. Synthetische Fälle prüfen Robustheit, legitimieren aber keine ungetesteten Quellen. Die Holdout-Prüfung verwendet die zuvor festgelegten Schwellen. Die Freigabe ist an den Hash des geprüften Master-Skills gebunden; Änderungen benötigen neue Kalibrierung. Fehlende oder ungültige Profile erlauben keine Decision Model-Aktivierung.

## Ablauf und Wiederanlauf

Python erzeugt Kandidaten aus aktuellen Revisionen mit Quellen-ID, Textposition und Originalzitat. Gleiche Zahlen an verschiedenen Stellen bleiben getrennt. Mehrdeutige Zahlenformate behalten mögliche Normalisierungen. Zahlen aus unverbundenen früheren Setups und Zielnummerierungen stehen nicht als Handelswerte zur Auswahl. Mathematisch eindeutige Mengenwörter wie Viertel/quarter/четверть und Hälfte/half/половину liefern ausschließlich Kandidaten für eine Positionsreduktion; sie bleiben als Originalwort belegt. Überfüllte Auswahlmengen führen zur Ausnahmebehandlung. Standardklassifikationen benötigen einen Decision Model-Aufruf; Entries und Updates eine zweite Prüfung von Absicht, Feldrollen und Setup-Konsistenz.

Der Modellkontext enthält den Originalanker, seine tatsächlichen Reply-Vorfahren und relevante eigene Setups. Unverbundene Kanalnachrichten verbrauchen keine Interpretationstokens und machen gespeicherte Entscheidungen nicht ungültig. Python bestimmt eigene Setup-Bindungen einschließlich bereits akzeptierter Management-Anweisungen über alle Account- und Venue-Scopes. Ein Edit allein beweist keine ausgeführte Position. Explizite Entry-Formulierungen dürfen nicht als `unspecified` verloren gehen; reine Marktkommentare erzwingen keinen Ordertyp. Numerische Rollen, die einer gewählten Update-Aktion widersprechen, werden gemeinsam abgewiesen.

`source_jobs` im privaten Aqla-State speichert Kontextfingerabdruck, Fragen-/Kalibrierungsversion, Ergebnis, Plan-IDs und Bearbeitungsschritt. Geänderte Kontexte oder Worker-Generationen können alte Ergebnisse nicht anwenden. Bereits gebundene Quellen und unklare Finanzoperationen bleiben bei der bestehenden Reconciliation. Unveränderte fehlgeschlagene Jobs starten keine erneute Modellschleife; pro Anker und Politik sind höchstens drei fehlgeschlagene Kontextversuche möglich. Eine neue Nachrichtenrevision oder freigegebene Interpretationspolitik erzeugt einen neuen Auftrag. Fehler bleiben als Quellenarbeit und Systemmeldung sichtbar.

Im Hybrid-Modus ist die automatische Quellenautorität dem Worker vorbehalten. Betreibersteuerung, Entry-Pause und die bestehenden Handelsprüfungen bleiben wirksam. Quellenleserfehler und Inhaltskonflikte werden als versionierte Statusübergänge gemeldet, ohne Modellaufruf.

Vorübergehend fehlende Börsenvorschauen bleiben als `waiting_preview` gespeichert. Python setzt ausschließlich noch nicht eingereichte Ziele mit derselben belegten Interpretation fort, auch wenn andere Teilpositionen bereits offen sind. Ein MCP-Neustart löst verwaiste reine Vorschau-Lesesperren; bestehende Finanzoperationen behalten ihre Reconciliation. Veränderte Originalnachrichten, Fragenversionen oder Freigabeprofile verhindern weitere Einreichungen aus dem alten Job. Bereits ausgeführte Orders bleiben beim bestehenden Recovery-Pfad. Für einen unveränderten gültigen Wiederanlauf entsteht kein neuer Modellaufruf.

## Benachrichtigungen

Begründungen und Entscheidungscodes erzeugt Python aus festen Vorlagen. Die Klassifikationsbelege decken den gesamten aktuellen Originaltext ohne zusätzliche Modellfrage ab. Ein unvollständiger oder mehrdeutiger Auftrag mit positiver oder unsicherer Handelsabsicht geht zur begrenzten generativen Prüfung. Nur ausreichend sicher negative, kalibrierte und nicht deaktivierte Intent-Entscheidungen dürfen ohne diese Prüfung gespeichert werden. Berichte und sachfremde Nachrichten dürfen keine neue Handelsanweisung verdecken. Die ursprüngliche Kommunikationsabsicht wird unabhängig vom historischen Empfang oder dem Nachrichtenalter bewertet; die aktuelle Ausführbarkeit bestimmt Python.

Eine private `notification_outbox` friert Texte, Empfänger, Konto, Netzwerk und Meldungsversionen ein. Nur ihr nativer Worker sendet über den vorhandenen Hermes-Adapter. Erfolgreiche Teile bleiben gespeichert; erfolgreicher Gesamtversand und versioniertes Fakten-ACK werden gemeinsam geschrieben. Ein alter ACK entfernt keine neue Meldungsversion.

Bei einem unklaren Sendestand wird eine mögliche Dublette ausdrücklich gekennzeichnet. Fehlgeschlagene Sendungen verwenden Backoff und bis zu fünf automatische Versuche. Erschöpfte Jobs haben den Status `exhausted`; ein neu verbundener nativer Adapter kann sie wieder aufnehmen. Für eine gezielte administrative Wiederaufnahme steht der geschützte interne `integration`-Auftrag `delivery_retry` zur Verfügung. Bekannte erfolgreiche Teile werden nicht erneut gesendet. Für diesen Pfad wird keine zusätzliche Hermes-Final-Response-Ledger-Schleife angelegt.

## Offline-Vergleich und Kalibrierung

Das Prüfprogramm besitzt keinerlei Bot-, MCP- oder Orderwerkzeuge:

```sh
aqla-evaluate-decisions --dataset annotated-sources.json --output decision-report.json --sources QUELLEN_CHAT_ID --master hermes-home/skills/aqla-master/SKILL.md --write-profile calibration/decision-source-v5.json
```

Es verwendet den OpenRouter-Key aus der Umgebung und kann API-Kosten verursachen. Jede Dataset-Zeile besitzt `id`, `chat_id`, `messages`, `expected`, `split` (`calibration` oder `holdout`) und `group_id` zur Trennung vollständiger Quellthreads. Replys können `reply_to_id` angeben. `language` nennt `de`, `en` oder `ru`; `tags` verwenden `reply`, `edit`, `percent`, `decimal`, `injection`, `multi_setup`, `report_with_action`. Für eine Aktivierungsfreigabe müssen `annotation_complete: true` und sämtliche kritischen Interpretationsfelder in `expected` vorliegen, auch mit explizitem `null` oder leeren Listen. Die synthetischen `tests/fixtures/context_cases.json` können ohne Aktivierungsprofil geprüft werden; deren Teilannotationen reichen nicht zur Produktionsfreigabe.

Für reale Replies und Edits akzeptiert `messages` vollständige `SourceEvent`-Objekte mit unveränderten Nachrichten-IDs, Zeitstempeln und Reply-/Thread-Bindungen; `anchor_event_id` kann den aktuellen Auftrag ausdrücklich bestimmen. `message_event_ids` erhält bei Bedarf gespeicherte Event-Identitäten aus älteren Aqla-Versionen; zugelassen sind ausschließlich die aktuelle oder bekannte alte Hashform der unveränderten Originalrevision. Fehlende Empfangszeiten synthetischer Fälle werden deterministisch aus ihren Revisionszeiten erstellt. Die Annotation umfasst auch `source_refs` und die vollständigen `evidence`-Einträge. Korrekte Zahlen bei falscher Quellenzuordnung reichen daher nicht für eine erfolgreiche Qualitätsprüfung.

Für eine Freigabe ist eine ausdrückliche `group_id` je Quellthread erforderlich. Dieselbe reale Originalnachricht einschließlich ihrer Edits darf auch bei unterschiedlichen Gruppen-IDs nicht in beiden Splits vorkommen; solche Datensätze werden vor dem ersten Modellaufruf abgewiesen.

Der Sammelmodus erfasst unsichere Antworten zur Kalibrierung, erteilt aber niemals eine Ausführungsfreigabe. Anschließend laufen unveränderte Holdout-Antworten durch dieselbe vollständige Prüfung wie die Produktion. Schwellen entstehen ausschließlich aus Kalibrierungsantworten; eine falsche Antwort mit Konfidenz 1 sperrt die betreffende Frage statt sie an der Grenze zuzulassen. Rohgenauigkeit, akzeptierte Fälle, Rückstellungen und tatsächlich freigabefähige Quellenarten werden getrennt berichtet.

Offline-Kontexte können mit `revision_time_context` als semantischer Zeitpunktvergleich erstellt werden: nur bis zur Ankerrevision verfügbare Nachrichtenänderungen, anschließend die letzten 20 Nachrichten und tatsächlichen Reply-Vorfahren. Dies reproduziert keine verzögerte Telegram-Zustellung; spätere Edits dürfen nicht in frühere Testfälle gelangen.

Das Programm speichert jeden Rohbefund atomar in einer privaten Checkpoint-Datei neben dem Bericht. Ein identischer Lauf nimmt dort ohne doppelte Modellaufrufe wieder auf; andere Datensätze, Master-Texte, Fragenversionen oder Modellrevisionen werden abgewiesen. Anbieterfehler unterbrechen den Lauf und benötigen eine ausdrückliche Wiederholung mit `--retry-provider-failures`. `--request-interval` begrenzt die Anfragefrequenz (Standard vier Sekunden); `--timeout` steuert ausschließlich das Offline-Anfragelimit (Standard 30 Sekunden). Fehlende Verbrauchs- und Kostendaten bleiben gesondert sichtbar. `without_fallback_share` zählt nur korrekt akzeptierte echte Holdout-Fälle mit tatsächlich validierter Quellen- und Artenfreigabe; es ist keine Messung des Livebetriebs.

Optional akzeptiert `--baseline baseline.json` vorhandene generative Verbrauchsdaten als Liste mit denselben eindeutigen `id`-Werten sowie `input_tokens` und `cache_read_tokens`. Für ungeklärte Fälle wird konservativ der vollständige bisherige Input weitergerechnet. Die daraus berechnete Reduktion ist ausdrücklich eine Projektion; die tatsächlichen Hybrid-Kosten müssen anschließend aus den Laufzeitmetriken verglichen werden.

Vergleichswerte: richtige Werte und Belege, falsche Handelsfreigaben, verpasste Aktionen, Anteil ohne generatives Modell, Laufzeit und vollständige Kosten inklusive Decision Model, Verifikation und Ausnahmebehandlung. Die private Verbrauchsmessung in `cache/aqla-usage.sqlite3` unterscheidet `decision`, `decision_verification` und `interpretation_fallback`; Quellenjobs enthalten keine Gesprächskompression oder Skill-Reviews. Input- und Output-Tokens sowie tatsächliche Kosten stammen aus OpenRouters `usage`-Antwort. Für Mercury Decide Free werden keine festen Tokenpreise angenommen; fehlende Kosten bleiben unbekannt statt null Dollar. Die Pilotziele von 80 % Verarbeitung ohne generativen Fallback und 90 % weniger generativem Input benötigen reale Vergleichsdaten.

Laufzeitmessungen enthalten die Auftrags-ID, den Hash der Quellenrevision und Cache-Hits zur getrennten Auswertung je Nachricht. Fallback-Kosten verwenden Hermes' Preisauflösung mit Preisversion und Status (`actual`, `estimated`, `included`, `unknown`). Bei unbekannter Route, fehlendem Preis oder einer nicht belegbaren Anbieterumschaltung bleibt `cost_usd` ausdrücklich `null`.
