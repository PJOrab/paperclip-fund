# AI/Tech Fund — Datenfeed (Schritt 1)

Grundstein des Datenfeeds: speichert die Rohdaten aus den bestehenden
`macro-agent`-Adaptern (APIs + Scraper) in einer **Supabase**-Datenbank.

Architektur-Übersicht: [`docs/architecture.md`](docs/architecture.md).

## Aufbau

```
ingestion/
  config.py        # .env laden, Supabase-Keys, macro-agent-Pfad
  adapters.py      # Brücke zu den macro-agent-Adaptern (importiert main.py)
  db.py            # Supabase-Client: upsert raw_items + Lauf-Protokoll
  run_ingest.py    # Entrypoint
supabase/migrations/
  0001_init.sql    # Schema: sources, raw_items, ingestion_runs
```

Es wird **nichts dupliziert**: `adapters.py` importiert das vorhandene
`~/macro-agent/main.py` und nutzt dessen Adapter-Klassen (GDELT, Zentralbanken,
NewsAPI, FRED, CBOE P/C, CFTC COT, X/Suche) direkt wieder.

## Setup

1. **Supabase-Projekt** anlegen (supabase.com) und das Schema einspielen:
   - Dashboard → SQL Editor → Inhalt von `supabase/migrations/0001_init.sql` ausführen.
2. **.env** anlegen:
   ```bash
   cp .env.example .env
   # SUPABASE_URL + SUPABASE_SERVICE_KEY aus Project Settings → API eintragen
   ```
3. **Abhängigkeiten** installieren:
   ```bash
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   ```

## Nutzung

```bash
# Adapter testen, OHNE in die DB zu schreiben (kein Supabase nötig):
python -m ingestion.run_ingest --dry-run

# Einmaliger Lauf → Supabase:
python -m ingestion.run_ingest

# Kontinuierlich, alle 20 Minuten:
python -m ingestion.run_ingest --loop --interval 20
```

## Datenmodell

- **`sources`** — Quellen + Zuverlässigkeits-Score (0–1).
- **`raw_items`** — der Feed; dedupliziert über `content_hash`
  (`md5(text[:200] + source)`). Originaldaten in `raw` (jsonb).
- **`ingestion_runs`** — Protokoll je Lauf (Counts, per-Adapter, Fehler).

## Dashboard

MVP-Dashboard unter **https://hedgingalpha.com/fund/** — zeigt Workflow-Diagramm,
Feed-Statistik und das letzte Briefing (Thesen + Devil's Advocate).

```bash
python -m dashboard.build            # baut /var/www/html/fund/index.html
python -m dashboard.build --stdout   # HTML auf stdout (Test)
```
Generator liest Supabase serverseitig und bettet die Daten statisch ein (keine
Secrets im Browser). Cron baut alle 15 min neu; nginx serviert statisch.

## Nächste Schritte

- Adapter-Queries von Makro/Geopolitik auf **AI/Tech-Equities** umstellen
  (EDGAR 8-K/Form 4, IR-RSS, arXiv, GitHub-Trends, HN).
- Enrichment-Layer (Dedup → Ticker-Mapping → Importance-Score) + `events`-Tabelle.
