---
name: db-admin
description: >
  Inspect and modify the fund's Supabase Postgres database directly — list and
  describe tables, run read queries, and apply ADDITIVE schema changes (new tables,
  columns, indexes) when expanding data sources, adding scoring/track-record tables,
  or enriching the model. Use whenever the work needs the database extended or queried
  beyond the REST helpers. Destructive changes are refused without CEO approval.
---

# db-admin

Direct SQL access to the fund's Postgres (Supabase). Use this to evolve the schema as the fund grows — e.g. a new adapter needs a table, or you want a `thesis_scores` table for the track-record loop.

Connects via the stable direct connection (built from `SUPABASE_DB_URL` + `SUPABASE_DB_PASSWORD`, with pooler fallback). Read-only helpers and additive DDL run freely; **destructive** statements require CEO approval.

## Inspect
```bash
/srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/db_admin.py tables
/srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/db_admin.py describe --table raw_items
```

## Read query
```bash
/srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/db_admin.py query --sql "select source, count(*) from raw_items group by 1 order by 2 desc"
```

## Change the schema (additive — runs freely)
```bash
/srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/db_admin.py exec --sql "create table if not exists thesis_scores (id uuid primary key default gen_random_uuid(), thesis_id text, scored_at timestamptz default now(), outcome text, pnl numeric)"
# or a migration file:
/srv/ai-tech-fund/venv/bin/python /srv/ai-tech-fund/fund_skills/db_admin.py migrate --file supabase/migrations/0003_xyz.sql
```
Prefer `create table if not exists`, `alter table ... add column`, `create index` — additive, reversible.

## Safety (hard rule)
- **Destructive statements** — `DROP`, `TRUNCATE`, `DELETE`/`UPDATE` without a `WHERE`, `GRANT`/`REVOKE` — are **refused** (the tool returns `{"refused": true}`). Per `COMPANY.md` they need explicit **CEO approval**. Do NOT pass `--force` on your own initiative; escalate to the CIO/board with the exact statement and why.
- Test additive changes mentally first; prefer `if not exists`. Commit any new migration `.sql` to the repo so the change is tracked.
- Never put DB credentials in code, logs, or comments.
