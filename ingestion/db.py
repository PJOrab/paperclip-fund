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


def upsert_raw_items(items: list[dict]) -> int:
    """
    Upsert auf content_hash (Duplikate werden ignoriert).
    Gibt die Anzahl tatsächlich eingefügter Zeilen zurück.
    """
    if not items:
        return 0
    # Innerhalb des Batches auf content_hash deduplizieren (sonst ON CONFLICT-Fehler)
    by_hash = {it["content_hash"]: it for it in items}
    rows = list(by_hash.values())

    res = (
        client()
        .table("raw_items")
        .upsert(rows, on_conflict="content_hash", ignore_duplicates=True)
        .execute()
    )
    # Bei ignore_duplicates enthält res.data nur die wirklich neu eingefügten Zeilen
    return len(res.data or [])
