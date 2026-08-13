# Bündnisverpflichtungs-Monitor

Verfolgt automatisiert, ob Deutschland seine Zusagen auf NATO-, EU- und
Bundesebene einhält, aufweicht oder verzögert — dargestellt als Ampel
(🔴 Rot / 🟡 Gelb / 🟢 Grün / ⚪ Grau) mit Quelle, Datum und Begründung zu
jeder Einschätzung. Läuft komplett kostenlos über GitHub Actions + GitHub Pages.

## Wie es funktioniert

```
Beobachten          Bewerten              Aggregieren           Darstellen
(Quellen abrufen) → (Keyword-Abgleich    → (neuester Fund      → (Dashboard mit
                      pro Zusage)           bestimmt Ampel)       Klick zur Quelle)
```

1. **`scripts/fetch_and_assess.py`** ruft die in `config/quellen.yaml`
   definierten Quellen ab (Bundestag DIP-API, RSS-Feeds, einfache HTML-Seiten
   ohne Feed), filtert alles vor dem 01.01.2025 heraus, und prüft jeden Fund
   per Keyword-Abgleich gegen die in `config/zusagen.yaml` definierten
   Bündnis-Zusagen.
2. Ein **GitHub Actions Workflow** (`.github/workflows/monitor.yml`) führt
   das Skript täglich automatisch aus und committet das Ergebnis
   (`docs/data/status.json`) zurück ins Repo.
3. **`docs/index.html`** liest diese Datei und stellt sie über GitHub Pages
   als Dashboard dar.

Nichts ist eine Blackbox: Jede Ampel zeigt, welcher Fund sie ausgelöst hat,
mit Datum und Link zur Originalquelle.

## Einrichtung (einmalig, ca. 10 Minuten)

1. **Neues GitHub-Repository anlegen** (kann privat oder öffentlich sein —
   bei öffentlichen Repos sind die Actions-Minuten unbegrenzt).
2. Diesen gesamten Ordnerinhalt in das Repo hochladen (z.B. per Drag&Drop im
   Browser über "Add file → Upload files", oder per `git push`).
3. **GitHub Pages aktivieren**: Repo → Settings → Pages → "Deploy from a
   branch" → Branch `main`, Ordner `/docs` → Save.
   Das Dashboard ist danach unter `https://DEIN-NUTZERNAME.github.io/DEIN-REPO/`
   erreichbar.
4. **Workflow-Rechte prüfen**: Repo → Settings → Actions → General →
   "Workflow permissions" → "Read and write permissions" auswählen
   (nötig, damit der Bot die Ergebnisse zurück ins Repo committen darf).
5. **Ersten Lauf manuell auslösen**: Repo → Actions → "Bündnisverpflichtungs-Monitor"
   → "Run workflow". Danach läuft er automatisch täglich um 06:00 UTC.

Fertig — kein Server, keine Kosten, kein API-Key zwingend nötig.

## Optional: eigenen DIP-API-Key hinterlegen

Das Skript nutzt standardmäßig einen öffentlichen Test-Schlüssel der
Bundestags-API (DIP). Für den Dauerbetrieb empfiehlt sich ein eigener
Schlüssel:

1. Formlose Mail an `parlamentsdokumentation@bundestag.de` mit Bitte um
   einen API-Key für DIP.
2. Im Repo unter Settings → Secrets and variables → Actions → "New repository
   secret" → Name `DIP_API_KEY`, Wert der eigene Schlüssel.

## Konfiguration anpassen

- **`config/zusagen.yaml`** — welche Bündnis-Zusagen getrackt werden und mit
  welchen Schlüsselwörtern sie erkannt werden. Am Anfang lohnt es sich, nach
  den ersten echten Läufen `docs/data/log.jsonl` durchzusehen und Begriffe zu
  ergänzen, die dort auftauchen, aber noch nicht erfasst sind.
- **`config/quellen.yaml`** — welche Quellen abgefragt werden. Für EUR-Lex
  ist ein personalisierter RSS-Feed nötig (My EUR-Lex → gespeicherte Suche →
  "Create in My RSS feeds"), da EUR-Lex keine öffentlichen Sammel-Feeds ohne
  Login anbietet.
- **`MIN_DATUM`** und **`LOOKBACK_TAGE_FUER_AMPEL`** in
  `scripts/fetch_and_assess.py` — Datumsgrenze bzw. wie weit für die aktuelle
  Ampel zurückgeschaut wird (Standard: 120 Tage).

## Bekannte Grenzen

- **HTML-Scan-Quellen** (NATO-Newsroom, BMVg, Auswärtiges Amt, Rat der EU)
  haben kein zuverlässiges Veröffentlichungsdatum im Seitencode. Das Skript
  nutzt ersatzweise das Datum, an dem der Link zum ersten Mal gesehen wurde
  ("first seen"). Das ist im Dashboard mit `*` gekennzeichnet.
- Die Bewertung ist **regelbasiert** (Keyword-Matching), kein KI-Textverständnis.
  Das ist bewusst so gewählt, um bei 0€ Kosten zu bleiben. Verneinungen,
  Ironie oder komplexe Satzstellungen können falsch eingeordnet werden — die
  verlinkte Originalquelle ist deshalb immer Teil der Ampel.
- Ein optionaler Ausbau mit der Claude-API (bessere Textbewertung, geringe
  Kosten) ist im Code an der Funktion `assess_item()` vorbereitet, aber nicht
  aktiviert.

## Nächste Schritte, die sich anbieten

- Nach den ersten Läufen: Keyword-Listen in `zusagen.yaml` anhand von
  `docs/data/log.jsonl` verfeinern.
- Benachrichtigung bei Farbwechsel (z.B. Slack/Discord-Webhook) ergänzen.
- Weitere Zusagen/Quellen hinzufügen.
