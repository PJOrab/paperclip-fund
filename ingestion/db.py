"""Supabase-Anbindung: Upsert von raw_items + Lauf-Protokoll."""
from datetime import datetime, timezone

from supabase import create_client, Client

from . import config

_client: Client | None = None


def client() -> Client:
    global _client
    if _client is None:
        config.require_supabase()
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return _client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_run() -> str:
    res = client().table("ingestion_runs").insert({"status": "running"}).execute()
    return res.data[0]["id"]


def finish_run(run_id: str, *, status: str, items_fetched: int,
               items_inserted: int, per_adapter: dict, errors: dict) -> None:
    client().table("ingestion_runs").update({
        "finished_at": _now(),
        "status": status,
        "items_fetched": items_fetched,
        "items_inserted": items_inserted,
        "per_adapter": per_adapter,
        "errors": errors or None,
    }).eq("id", run_id).execute()


def ensure_sources(items: list[dict]) -> None:
    """
    Registriert alle in den Items vorkommenden Quellen in der sources-Tabelle,
    bevor raw_items eingefügt wird (sonst Foreign-Key-Verletzung für neue
    Kategorien wie z.B. 'crypto_intel' aus den X-Account-Tiers).
    Bestehende Einträge (mit kuratierter reliability/kind) werden NICHT überschrieben.
    """
    by_name: dict[str, dict] = {}
    for it in items:
        name = it.get("source") or "unknown"
        if name not in by_name:
            rel = it.get("reliability")
            by_name[name] = {
                "name": name,
                "reliability": rel if rel is not None else 0.25,
            }
    if by_name:
        (client().table("sources")
         .upsert(list(by_name.values()), on_conflict="name", ignore_duplicates=True)
         .execute())


_UPSERT_CHUNK = 100  # Supabase PostgREST: safe batch size avoids payload/timeout limits


def upsert_raw_items(items: list[dict]) -> int:
    """
    Upsert auf content_hash (Duplikate werden ignoriert).
    Sendet in Chunks von _UPSERT_CHUNK Zeilen, damit große Runs (600+ Items) nicht
    an Supabase-Payload-/Timeout-Grenzen scheitern.
    Gibt die Anzahl tatsächlich eingefügter Zeilen zurück.
    """
    if not items:
        return 0
    ensure_sources(items)
    # Innerhalb des Batches auf content_hash deduplizieren (sonst ON CONFLICT-Fehler)
    by_hash = {it["content_hash"]: it for it in items}
    rows = list(by_hash.values())

    inserted = 0
    db = client()
    for i in range(0, len(rows), _UPSERT_CHUNK):
        chunk = rows[i: i + _UPSERT_CHUNK]
        res = (
            db.table("raw_items")
            .upsert(chunk, on_conflict="content_hash", ignore_duplicates=True)
            .execute()
        )
        # Bei ignore_duplicates enthält res.data nur die wirklich neu eingefügten Zeilen
        inserted += len(res.data or [])
    return inserted
