-- ============================================================================
-- AI/Tech Fund — Agenten-Pipeline: briefing_runs
-- ----------------------------------------------------------------------------
-- Ein Lauf = eine Zeile. Jede Agenten-Stufe füllt ihre Spalte und setzt status
-- auf die NÄCHSTE Stufe. Die n8n-SSH-Nodes triggern die Stufen der Reihe nach;
-- jede Stufe arbeitet auf der jüngsten Zeile mit ihrem status (kein Arg-Passing).
-- In Supabase: SQL Editor → dieses Skript ausführen.
-- ============================================================================

create table if not exists public.briefing_runs (
    id               uuid primary key default gen_random_uuid(),
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    -- status = nächste auszuführende Stufe:
    -- analyst → thesis → devil → editor → done  (oder 'error')
    status           text not null default 'analyst',
    window_hours     int  not null default 24,
    triage           jsonb,   -- ausgewählte/geclusterte Items (Haiku)
    analysis         jsonb,   -- Analysteneinschätzung je Cluster (Sonnet)
    theses           jsonb,   -- investierbare Thesen (Opus)
    devils_advocate  jsonb,   -- Gegenpositionen je These (Opus)
    briefing_md      text,    -- finales CEO-Briefing (Opus, Markdown)
    error            text
);

create index if not exists briefing_runs_status_idx     on public.briefing_runs (status);
create index if not exists briefing_runs_created_at_idx  on public.briefing_runs (created_at desc);
