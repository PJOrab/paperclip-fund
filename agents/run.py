"""
Agenten-Pipeline-Entrypoint. Wird von n8n per SSH-Node stufenweise getriggert:

  python -m agents.run triage     # legt neuen Run an, wählt/clustert Items (Haiku)
  python -m agents.run analyst    # Sonnet
  python -m agents.run thesis      # Opus
  python -m agents.run devil       # Opus  (Devil's Advocate)
  python -m agents.run editor      # Opus  → druckt Briefing-Markdown auf stdout

Jede Stufe arbeitet auf der jüngsten briefing_runs-Zeile mit passendem status
(kein Argument-Passing zwischen n8n-Nodes nötig).

Zum Testen ohne briefing_runs-Tabelle:
  python -m agents.run pipeline    # alle 5 Stufen in-memory, druckt Briefing
"""
import argparse
import sys
from datetime import datetime, timezone, timedelta

from ingestion.db import client
from . import prompts as P
from . import claude_cli as C

# Modell je Rolle — auf Wunsch alle auf Opus 4.7 ('opus' = neuestes Opus).
MODEL = {"triage": "opus", "analyst": "opus",
         "thesis": "opus", "devil": "opus", "editor": "opus"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Reine Stufen-Funktionen (keine DB) — von DB-Stufen UND pipeline genutzt
# ---------------------------------------------------------------------------
def read_recent_items(window_hours: int, limit: int = 400) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    return (client().table("raw_items")
            .select("source,text,url,reliability,fetched_at")
            .gte("fetched_at", cutoff).order("fetched_at", desc=True)
            .limit(limit).execute().data or [])


def compute_triage(rows: list[dict]) -> list[dict]:
    out = C.call_json(P.triage_user(rows), system=P.TRIAGE_SYSTEM,
                      model=MODEL["triage"], timeout=240)
    clusters = out.get("clusters", []) if isinstance(out, dict) else (out or [])
    for cl in clusters:  # Belege auflösen, damit nachgelagerte Stufen Kontext haben
        refs = cl.get("item_refs", []) or []
        cl["evidence"] = [rows[i]["text"][:300] for i in refs
                          if isinstance(i, int) and 0 <= i < len(rows)]
    return clusters


def compute_analyst(clusters: list[dict]) -> dict:
    return C.call_json(P.analyst_user(clusters), system=P.ANALYST_SYSTEM,
                       model=MODEL["analyst"])


def compute_thesis(analyses: list[dict]) -> dict:
    return C.call_json(P.thesis_user(analyses), system=P.THESIS_SYSTEM,
                       model=MODEL["thesis"])


def compute_devil(theses: list[dict]) -> dict:
    return C.call_json(P.devil_user(theses), system=P.DEVIL_SYSTEM,
                       model=MODEL["devil"])


def compute_editor(triage: dict, theses: list[dict], critiques: list[dict]) -> str:
    return C.call(P.editor_user(triage, theses, critiques),
                  system=P.EDITOR_SYSTEM, model=MODEL["editor"]).strip()


# ---------------------------------------------------------------------------
# DB-Helfer
# ---------------------------------------------------------------------------
def _latest(status: str):
    res = (client().table("briefing_runs").select("*").eq("status", status)
           .order("created_at", desc=True).limit(1).execute())
    return res.data[0] if res.data else None


def _update(run_id: str, fields: dict) -> None:
    fields["updated_at"] = _now()
    client().table("briefing_runs").update(fields).eq("id", run_id).execute()


def _fail(run_id: str, stage: str, e: Exception):
    _update(run_id, {"status": "error", "error": f"{stage}: {type(e).__name__}: {e}"})
    _log(f"[{stage}] FAILED: {e}")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# DB-gebundene Stufen (für n8n)
# ---------------------------------------------------------------------------
def stage_triage(window: int):
    rows = read_recent_items(window)
    if not rows:
        _log("[triage] keine raw_items im Zeitfenster"); return
    run = (client().table("briefing_runs")
           .insert({"status": "triage", "window_hours": window}).execute().data[0])
    rid = run["id"]
    try:
        clusters = compute_triage(rows)
        _update(rid, {"triage": {"clusters": clusters}, "status": "analyst"})
        _log(f"[triage] run {rid}: {len(clusters)} Cluster aus {len(rows)} Items")
        print(rid)
    except Exception as e:
        _fail(rid, "triage", e)


def _middle_stage(status_in: str, status_out: str, label: str, fn):
    run = _latest(status_in)
    if not run:
        _log(f"[{label}] keine briefing_runs-Zeile mit status={status_in}"); return
    rid = run["id"]
    try:
        _update(rid, fn(run))
        _update(rid, {"status": status_out})
        _log(f"[{label}] run {rid} → {status_out}")
    except Exception as e:
        _fail(rid, label, e)


def stage_analyst():
    _middle_stage("analyst", "thesis", "analyst",
                  lambda r: {"analysis": compute_analyst((r.get("triage") or {}).get("clusters", []))})


def stage_thesis():
    _middle_stage("thesis", "devil", "thesis",
                  lambda r: {"theses": compute_thesis((r.get("analysis") or {}).get("analyses", []))})


def stage_devil():
    _middle_stage("devil", "editor", "devil",
                  lambda r: {"devils_advocate": compute_devil((r.get("theses") or {}).get("theses", []))})


def stage_editor():
    run = _latest("editor")
    if not run:
        _log("[editor] keine briefing_runs-Zeile mit status=editor"); return
    rid = run["id"]
    try:
        md = compute_editor(run.get("triage") or {},
                            (run.get("theses") or {}).get("theses", []),
                            (run.get("devils_advocate") or {}).get("critiques", []))
        _update(rid, {"briefing_md": md, "status": "done"})
        _log(f"[editor] run {rid} → done")
        print(md)  # stdout → n8n Telegram-Node
    except Exception as e:
        _fail(rid, "editor", e)
    # Post-briefing QC: flag missed big events as coverage-bug tickets (best-effort)
    try:
        from agents.coverage_qc import main as _qc_main
        import sys as _sys
        _sys.argv = ["coverage_qc", "--run-id", rid]
        _qc_main()
    except Exception as qc_err:
        _log(f"[editor] coverage_qc non-fatal error: {qc_err}")


# ---------------------------------------------------------------------------
# In-Memory-Pipeline (Test ohne briefing_runs-Tabelle)
# ---------------------------------------------------------------------------
def pipeline(window: int):
    rows = read_recent_items(window)
    _log(f"[pipeline] {len(rows)} Items im Fenster")
    clusters = compute_triage(rows);                 _log(f"[pipeline] triage: {len(clusters)} Cluster")
    analyses = compute_analyst(clusters);            _log(f"[pipeline] analyst ok")
    theses = compute_thesis(analyses.get('analyses', analyses)); _log(f"[pipeline] thesis ok")
    th = theses.get("theses", theses) if isinstance(theses, dict) else theses
    critiques = compute_devil(th);                   _log(f"[pipeline] devil ok")
    cr = critiques.get("critiques", critiques) if isinstance(critiques, dict) else critiques
    md = compute_editor({"clusters": clusters}, th, cr)
    print(md)


def main():
    ap = argparse.ArgumentParser(description="AI/Tech Fund — Agenten-Pipeline")
    ap.add_argument("stage", choices=["triage", "analyst", "thesis", "devil",
                                       "editor", "pipeline"])
    ap.add_argument("--window", type=int, default=24, help="Zeitfenster in Stunden (triage/pipeline)")
    args = ap.parse_args()

    if args.stage == "triage":    stage_triage(args.window)
    elif args.stage == "analyst": stage_analyst()
    elif args.stage == "thesis":  stage_thesis()
    elif args.stage == "devil":   stage_devil()
    elif args.stage == "editor":  stage_editor()
    elif args.stage == "pipeline": pipeline(args.window)


if __name__ == "__main__":
    main()
