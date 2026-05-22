# Coverage-QC — Post-Briefing Gap-Check (HED-27)

Automatischer Quality-Gate, der nach jedem Briefing-Run prüft, ob das
ausgelieferte Briefing eine Großmeldung verpasst hat, die im Feed-Fenster lag.
Automatisierter Nachfolger des manuell gefundenen HED-24 Exa-$250M-Miss.

## Was es tut

Für einen Briefing-Run:

1. Re-derived das `raw_items`-Fenster des Runs (`window_hours`, default 24h vor
   `created_at` aus `briefing_runs`).
2. Erkennt Großmeldungen mit konservativen Heuristiken: IPO/S-1-Registrierungen,
   größere Fundings (Betrag an Raise-Verb/Runde/Bewertung gebunden), Major-
   Launches (Modell/Chip/Plattform/API).
3. Vergleicht jede Großmeldung gegen das ausgelieferte Briefing
   (`briefing_runs.briefing_md` + Triage-Cluster-Titel/why/tickers) per
   Entity-Matching (Ticker-Aliase + Watchlist + bekannte Private Player; sonst
   Fallback auf den Proper-Noun-Betreff).
4. Jede unbedeckte Großmeldung ist ein **Coverage-Gap**: wird gemeldet und mit
   `--open-tickets` automatisch als Coverage-Bug-Ticket beim Data-Engineer
   eingestellt (gleiche Konvention wie HED-24).

Ergebnisse landen additiv in der `coverage_qc`-Tabelle (Audit + Cross-Run-Dedup;
dasselbe Subjekt wird nicht innerhalb von 7 Tagen erneut ticketed).

## Aufruf

```bash
# Report-only gegen einen bestimmten Run:
python scripts/coverage_qc.py --run-id <RUN_ID>

# Gaps als Coverage-Bug-Tickets einstellen (Flood-Guard: --max-tickets, default 5):
python scripts/coverage_qc.py --run-id <RUN_ID> --open-tickets

# Letzter abgeschlossener Run (status=done), maschinenlesbar:
python scripts/coverage_qc.py --json

# QC-Lauf ohne coverage_qc-Zeile zu schreiben:
python scripts/coverage_qc.py --run-id <RUN_ID> --no-persist
```

Verifiziert gegen Run `11a62db6-758c-467b-bfd9-3d56ce8ac312` — findet den
Exa-$250M-Miss (HED-24) zuverlässig.

## Konfiguration

Aus `.env` (siehe `ingestion/config.py` für Supabase):

- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` — DB-Zugriff (`raw_items`, `briefing_runs`).
- `PAPERCLIP_API_BASE` (default `http://127.0.0.1:3100`), `PAPERCLIP_API_KEY`,
  `PAPERCLIP_COMPANY_ID`, `PAPERCLIP_DE_AGENT_ID` — Ticket-Erstellung.

## Aufbau

`scripts/coverage_qc.py` ist ein dünner Entry-Point auf die kanonische
Implementierung in `fund_skills/coverage_qc.py` (keine Logik-Duplikation).
Fehler werden geloggt, nicht geworfen — ein Post-Run-Hook crasht nie.
