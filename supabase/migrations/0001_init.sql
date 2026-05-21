-- ============================================================================
-- AI/Tech Fund — Datenfeed: Grundschema
-- ----------------------------------------------------------------------------
-- Speichert die Rohdaten aus den bestehenden macro-agent-Adaptern (APIs +
-- Scraper). Enrichment / Events / Theses kommen in späteren Migrationen.
-- In Supabase: SQL Editor öffnen und dieses Skript ausführen
-- (oder via `supabase db push`).
-- ============================================================================

create extension if not exists pgcrypto;

-- ----------------------------------------------------------------------------
-- sources: Referenztabelle mit Zuverlässigkeits-Score je Quelle
-- (gespiegelt aus macro-agent SOURCE_RELIABILITY)
-- ----------------------------------------------------------------------------
create table if not exists public.sources (
    name        text primary key,
    reliability numeric not null default 0.25 check (reliability between 0 and 1),
    kind        text
);

insert into public.sources (name, reliability, kind) values
    ('reuters',            0.95, 'news'),
    ('bloomberg',          0.95, 'news'),
    ('ap_news',            0.90, 'news'),
    ('government_official',0.85, 'official'),
    ('central_bank',       0.98, 'official'),
    ('fred_macro',         0.95, 'macro'),
    ('cftc_positioning',   0.85, 'positioning'),
    ('options_flow',       0.80, 'positioning'),
    ('market_data',        0.75, 'market'),
    ('ais_maritime',       0.85, 'osint'),
    ('adsb_military',      0.70, 'osint'),
    ('macro_analysis',     0.70, 'analysis'),
    ('x_search',           0.60, 'social'),
    ('pizzint',            0.75, 'osint'),
    ('news',               0.60, 'news'),
    ('x_osint',            0.50, 'social'),
    ('unknown',            0.25, 'unknown')
on conflict (name) do update
    set reliability = excluded.reliability,
        kind        = excluded.kind;

-- ----------------------------------------------------------------------------
-- raw_items: der unprozessierte Datenfeed (eine Zeile = ein gefetchtes Item)
-- ----------------------------------------------------------------------------
create table if not exists public.raw_items (
    id           uuid primary key default gen_random_uuid(),
    content_hash text not null unique,          -- Dedup: md5(text[:200] + source)
    adapter      text not null,                 -- welcher Adapter (GDELT, NewsAPI, ...)
    source       text not null references public.sources(name) on update cascade,
    text         text not null,                 -- Headline / Inhalt
    url          text,
    reliability  numeric,                        -- aus Item oder source-Default
    raw          jsonb,                          -- Original-Dict des Adapters
    fetched_at   timestamptz not null default now()
);

create index if not exists raw_items_fetched_at_idx on public.raw_items (fetched_at desc);
create index if not exists raw_items_source_idx      on public.raw_items (source);
create index if not exists raw_items_adapter_idx     on public.raw_items (adapter);

-- ----------------------------------------------------------------------------
-- ingestion_runs: Lauf-Protokoll (Observability, ersetzt run_history.json)
-- ----------------------------------------------------------------------------
create table if not exists public.ingestion_runs (
    id              uuid primary key default gen_random_uuid(),
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    status          text not null default 'running',  -- running | ok | error
    items_fetched   int not null default 0,
    items_inserted  int not null default 0,
    per_adapter     jsonb,                              -- {"GDELT": 5, "NewsAPI": 8, ...}
    errors          jsonb                               -- {"X/Suche": "401 ...", ...}
);

create index if not exists ingestion_runs_started_at_idx on public.ingestion_runs (started_at desc);
