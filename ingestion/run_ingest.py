"""
Ingestion-Entrypoint: führt alle Adapter aus und schreibt nach Supabase.

  python -m ingestion.run_ingest            # einmaliger Lauf
  python -m ingestion.run_ingest --dry-run  # Adapter laufen, KEIN DB-Schreiben
  python -m ingestion.run_ingest --loop --interval 20   # Endlosschleife
"""
import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

from . import adapters


def _telegram_alert(text: str) -> None:
    """Send a plain-text alert to Telegram. Silently no-ops if env vars missing."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        # Try loading from .env in the fund root
        try:
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).resolve().parent.parent / ".env")
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        except ImportError:
            pass
    if not token or not chat_id:
        return
    try:
        payload = json.dumps({"chat_id": chat_id, "text": text[:4096]}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)  # noqa: S310
    except Exception:  # noqa: BLE001
        pass


def run_once(dry_run: bool = False) -> None:
    print(f"\n=== Ingestion-Lauf {'(DRY RUN)' if dry_run else ''} ===")
    items, per_adapter, errors = adapters.collect()

    for name, n in per_adapter.items():
        flag = f"  ERROR: {errors[name]}" if name in errors else ""
        print(f"  [{name:>14s}] → {n:3d} items{flag}")
    print(f"  {'-'*44}")
    print(f"  Total gefetcht: {len(items)}")

    # Dead-adapter health check: adapters returning 0 items without a logged error
    # are silently dropping data. Surface them so operators notice and can investigate.
    silent_zeros = [n for n, c in per_adapter.items() if c == 0 and n not in errors]
    for name in silent_zeros:
        errors[name] = "0 items fetched — possible feed outage or config change"
    if silent_zeros:
        print(f"  ⚠  DEAD ADAPTERS (0 items, no error): {', '.join(silent_zeros)}")

    # Alert on ANY degraded adapter (errored OR silently empty). A run that loses
    # adapters — e.g. the shared macro HTTP layer going missing, which now degrades
    # gracefully instead of killing the run — must page loudly rather than silently
    # shipping a thin feed into the briefing.
    if errors:
        detail = "\n".join(f"• {n}: {errors[n]}" for n in sorted(errors))
        _telegram_alert(
            f"⚠️ AI/Tech Fund — {len(errors)}/{len(per_adapter)} Adapter degraded "
            f"({len(items)} items total)\n{detail}"
        )

    # Alert immediately on high-signal items that can't wait until next briefing:
    # S-1/IPO filings, items with [OUTLOOK:] guidance, and high-priority BIG_EVENT matches.
    _urgent: list[dict] = []
    try:
        from agents.coverage_qc import classify_item as _classify
        _HIGH_SOURCES = {"sec_registration", "earnings_result"}
        for it in items:
            src = it.get("source", "")
            txt = it.get("text") or ""
            if src in _HIGH_SOURCES:
                _urgent.append(it)
                continue
            if "[OUTLOOK:" in txt:
                _urgent.append(it)
                continue
            hits = _classify(txt)
            if any(prio == "high" for _, prio in hits):
                _urgent.append(it)
    except Exception:
        pass
    if _urgent and not dry_run:
        snippets = []
        for it in _urgent[:5]:
            src = it.get("source", "?")
            tickers = ",".join(it.get("tickers") or []) or "—"
            txt = (it.get("text") or "")[:100]
            snippets.append(f"• [{src}] {tickers}: {txt}")
        _telegram_alert(
            f"🚨 AI/Tech Fund — {len(_urgent)} high-signal item(s) neu:\n"
            + "\n".join(snippets)
        )

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
