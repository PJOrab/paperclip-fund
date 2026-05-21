"""
Ingestion-Entrypoint: führt alle Adapter aus und schreibt nach Supabase.

  python -m ingestion.run_ingest            # einmaliger Lauf
  python -m ingestion.run_ingest --dry-run  # Adapter laufen, KEIN DB-Schreiben
  python -m ingestion.run_ingest --loop --interval 20   # Endlosschleife
"""
import argparse
import time

from . import adapters


def run_once(dry_run: bool = False) -> None:
    print(f"\n=== Ingestion-Lauf {'(DRY RUN)' if dry_run else ''} ===")
    items, per_adapter, errors = adapters.collect()

    for name, n in per_adapter.items():
        flag = f"  ERROR: {errors[name]}" if name in errors else ""
        print(f"  [{name:>14s}] → {n:3d} items{flag}")
    print(f"  {'-'*44}")
    print(f"  Total gefetcht: {len(items)}")

    if dry_run:
        for it in items[:5]:
            print(f"    · [{it['source']}] {it['text'][:90]}")
        print("  (Dry run — nichts gespeichert)")
        return

    # DB-Import erst hier, damit --dry-run ohne supabase-Paket/Keys läuft
    from . import db
    run_id = db.start_run()
    try:
        inserted = db.upsert_raw_items(items)
        status = "error" if errors else "ok"
        db.finish_run(run_id, status=status, items_fetched=len(items),
                      items_inserted=inserted, per_adapter=per_adapter, errors=errors)
        print(f"  Neu in DB gespeichert: {inserted} (Duplikate übersprungen)")
    except Exception as e:
        db.finish_run(run_id, status="error", items_fetched=len(items),
                      items_inserted=0, per_adapter=per_adapter,
                      errors={**errors, "_db": f"{type(e).__name__}: {e}"})
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description="AI/Tech Fund — Datenfeed-Ingestion")
    ap.add_argument("--dry-run", action="store_true", help="Adapter laufen, kein DB-Schreiben")
    ap.add_argument("--loop", action="store_true", help="Endlosschleife")
    ap.add_argument("--interval", type=int, default=20, help="Minuten zwischen Läufen (--loop)")
    args = ap.parse_args()

    if not args.loop:
        run_once(dry_run=args.dry_run)
        return

    while True:
        try:
            run_once(dry_run=args.dry_run)
        except Exception as e:
            print(f"  Lauf fehlgeschlagen: {e}")
        print(f"  Nächster Lauf in {args.interval} min …")
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
