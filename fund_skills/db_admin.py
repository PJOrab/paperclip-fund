#!/usr/bin/env python3
"""db-admin: run SQL / schema changes on the fund's Supabase Postgres.

Subcommands:
  tables                          -> list public tables + column count
  describe --table NAME           -> columns of a table
  query  --sql "SELECT ..."       -> run a read query, print rows as JSON
  exec   --sql "CREATE TABLE ..." -> run DDL/DML (additive changes run freely;
                                      destructive ones are refused unless --force)
  migrate --file path.sql         -> run a .sql file in one transaction

Connection: built from SUPABASE_DB_URL (host/user/db) + SUPABASE_DB_PASSWORD
(read raw, no interpolation) — robust against special chars. Session pooler / SSL.

Safety: DROP / TRUNCATE / DELETE|UPDATE without WHERE / ALTER ... DROP / GRANT /
REVOKE are destructive and refused unless --force is given. Per COMPANY.md,
destructive changes also require explicit CEO approval — do not pass --force on
your own initiative; escalate.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

FUND_DIR = os.environ.get("FUND_DIR", "/srv/ai-tech-fund")
_RAW = dotenv_values(str(Path(FUND_DIR) / ".env"), interpolate=False)

DESTRUCTIVE = re.compile(
    r"\b(drop\s+(table|schema|database|view|index|column)|truncate\b|revoke\b|grant\b"
    r"|alter\s+table\s+\S+\s+drop\b)", re.IGNORECASE)
DML_NO_WHERE = re.compile(r"\b(delete\s+from|update)\b(?:(?!\bwhere\b).)*?(;|$)",
                          re.IGNORECASE | re.DOTALL)


def connect():
    import time
    import psycopg2
    u = urlparse(_RAW.get("SUPABASE_DB_URL", ""))
    pw = _RAW.get("SUPABASE_DB_PASSWORD") or u.password
    if not pw:
        print("missing SUPABASE_DB_PASSWORD", file=sys.stderr)
        sys.exit(2)
    # Project ref from SUPABASE_URL (e.g. https://<ref>.supabase.co)
    ref = urlparse(_RAW.get("SUPABASE_URL", "")).hostname or ""
    ref = ref.split(".")[0]
    db = (u.path or "/postgres").lstrip("/") or "postgres"
    # Prefer the DIRECT connection (stable); fall back to the (load-balanced,
    # occasionally flaky) session pooler. A couple of retries smooth transients.
    targets = []
    if ref:
        targets.append(dict(host=f"db.{ref}.supabase.co", port=5432, user="postgres"))
    if u.hostname:
        targets.append(dict(host=u.hostname, port=u.port or 5432, user=u.username))
    last = None
    for t in targets:
        for _ in range(3):
            try:
                return psycopg2.connect(password=pw, dbname=db, connect_timeout=20,
                                        sslmode="require", **t)
            except psycopg2.OperationalError as ex:
                last = ex
                if "authentication failed" in str(ex):
                    time.sleep(1.0)
                    continue
                break
    raise last if last else RuntimeError("no DB target available")


def is_destructive(sql: str) -> bool:
    return bool(DESTRUCTIVE.search(sql) or DML_NO_WHERE.search(sql))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tables")
    d = sub.add_parser("describe"); d.add_argument("--table", required=True)
    q = sub.add_parser("query"); q.add_argument("--sql"); q.add_argument("--file")
    e = sub.add_parser("exec"); e.add_argument("--sql"); e.add_argument("--file"); e.add_argument("--force", action="store_true")
    m = sub.add_parser("migrate"); m.add_argument("--file", required=True); m.add_argument("--force", action="store_true")
    a = ap.parse_args()

    def read_sql(args):
        if getattr(args, "sql", None):
            return args.sql
        path = getattr(args, "file", None)
        return (sys.stdin.read() if path in (None, "-") else open(path).read())

    conn = connect()
    conn.autocommit = False
    cur = conn.cursor()
    try:
        if a.cmd == "tables":
            cur.execute("""select t.table_name, count(c.column_name)
                           from information_schema.tables t
                           left join information_schema.columns c
                             on c.table_name=t.table_name and c.table_schema=t.table_schema
                           where t.table_schema='public'
                           group by t.table_name order by t.table_name""")
            print(json.dumps([{"table": r[0], "columns": r[1]} for r in cur.fetchall()]))
        elif a.cmd == "describe":
            cur.execute("""select column_name, data_type, is_nullable
                           from information_schema.columns
                           where table_schema='public' and table_name=%s
                           order by ordinal_position""", (a.table,))
            print(json.dumps([{"column": r[0], "type": r[1], "nullable": r[2]} for r in cur.fetchall()]))
        elif a.cmd == "query":
            cur.execute(read_sql(a))
            cols = [c.name for c in cur.description] if cur.description else []
            rows = [dict(zip(cols, r)) for r in cur.fetchall()] if cols else []
            print(json.dumps(rows, default=str, ensure_ascii=False))
        elif a.cmd in ("exec", "migrate"):
            sql = read_sql(a) if a.cmd == "exec" else open(a.file).read()
            if is_destructive(sql) and not a.force:
                print(json.dumps({"refused": True,
                    "reason": "Destructive statement (DROP/TRUNCATE/DELETE|UPDATE without WHERE/GRANT/REVOKE). "
                              "Per COMPANY.md this needs explicit CEO approval. Escalate; do not self-authorize --force."}))
                conn.rollback(); sys.exit(3)
            cur.execute(sql)
            conn.commit()
            print(json.dumps({"ok": True, "rowcount": cur.rowcount}))
    except Exception as ex:  # noqa: BLE001
        conn.rollback()
        print(json.dumps({"error": type(ex).__name__, "detail": str(ex)[:300]}))
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
