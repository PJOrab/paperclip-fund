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
import json
import sys
from datetime import datetime, timezone, timedelta

from ingestion.db import client
from . import prompts as P
from . import claude_cli as C
from fund_skills.validate_output import validate as _validate


def _check(schema: str, data: dict) -> None:
    """Log schema violations; non-fatal so the pipeline doesn't die on a single bad field."""
    errs = _validate(schema, data)
    if errs:
        _log(f"[validate/{schema}] WARNING — schema violations: {errs}")


def _cross_check_devil_conviction(theses: list[dict], critiques: list[dict]) -> None:
    """Warn when devil verdict contradicts thesis conviction (reject + conviction > 0.40)."""
    verdict_map = {c["id"]: c for c in critiques if isinstance(c, dict) and "id" in c}
    for t in theses:
        if not isinstance(t, dict):
            continue
        tid = t.get("id")
        critique = verdict_map.get(tid)
        if not critique:
            continue
        verdict = critique.get("verdict")
        conv = t.get("conviction")
        if verdict == "reject" and isinstance(conv, (int, float)) and conv > 0.40:
            _log(f"[validate/cross] WARNING — thesis {tid} conviction={conv} but devil=reject; "
                 f"editor must drop or move to Beobachten (≤0.40)")
        elif verdict == "caution" and isinstance(conv, (int, float)) and conv > 0.55:
            _log(f"[validate/cross] WARNING — thesis {tid} conviction={conv} but devil=caution; "
                 f"cap should be ~0.55")

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
def read_recent_items(window_hours: int, limit: int = 600) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    rows = (client().table("raw_items")
            .select("source,text,url,reliability,fetched_at")
            .gte("fetched_at", cutoff).order("fetched_at", desc=True)
            .limit(limit).execute().data or [])
    # Sort primary sources (high reliability) first so triage LLM sees them at
    # the top of the prompt rather than buried after hundreds of editorial items.
    # Secondary sort by fetched_at descending preserves recency within each tier.
    rows.sort(key=lambda r: (-(r.get("reliability") or 0.0), r.get("fetched_at") or ""))
    return rows


def _triage_max_clusters(n_items: int) -> int:
    """Scale cluster target with feed size: ~1 per 20 items, floor 6, cap 20.
    Floor lowered to 6 so quiet days can return fewer clusters rather than padding."""
    return max(6, min(20, n_items // 20))


def compute_triage(rows: list[dict]) -> list[dict]:
    max_cl = _triage_max_clusters(len(rows))
    out = C.call_json(P.triage_user(rows, max_clusters=max_cl), system=P.TRIAGE_SYSTEM,
                      model=MODEL["triage"], timeout=300)
    clusters = out.get("clusters", []) if isinstance(out, dict) else (out or [])
    _check("triage", {"clusters": clusters})
    for cl in clusters:  # Belege auflösen, damit nachgelagerte Stufen Kontext haben
        refs = cl.get("item_refs", []) or []
        cl["evidence"] = [
            rows[i]["text"][:(400 if (rows[i].get("reliability") or 0.0) >= 0.85 else 300)]
            for i in refs if isinstance(i, int) and 0 <= i < len(rows)
        ]
    return clusters


def compute_analyst(clusters: list[dict]) -> dict:
    out = C.call_json(P.analyst_user(clusters), system=P.ANALYST_SYSTEM,
                      model=MODEL["analyst"])
    _check("analyst", out)
    return out


def compute_thesis(analyses: list[dict]) -> dict:
    out = C.call_json(P.thesis_user(analyses), system=P.THESIS_SYSTEM,
                      model=MODEL["thesis"])
    _check("thesis", out)
    return out


def compute_devil(theses: list[dict], analyses: list[dict] | None = None) -> dict:
    out = C.call_json(P.devil_user(theses, analyses=analyses), system=P.DEVIL_SYSTEM,
                      model=MODEL["devil"])
    _check("devil", out)
    return out


def _fetch_prev_briefing() -> str | None:
    """Return the briefing from ~24h ago (window: 20-36h back) for a meaningful delta.
    Falls back to the most recent done run if no run falls in that window."""
    try:
        now = datetime.now(timezone.utc)
        lo = (now - timedelta(hours=36)).isoformat()
        hi = (now - timedelta(hours=20)).isoformat()
        res = (client().table("briefing_runs")
               .select("briefing_md")
               .eq("status", "done")
               .not_.is_("briefing_md", "null")
               .gte("created_at", lo)
               .lte("created_at", hi)
               .order("created_at", desc=True)
               .limit(1)
               .execute())
        if res.data:
            return res.data[0]["briefing_md"] or None
        # Fallback: any prior done run (at least skip the current run's triage window)
        cutoff = (now - timedelta(hours=4)).isoformat()
        res2 = (client().table("briefing_runs")
                .select("briefing_md")
                .eq("status", "done")
                .not_.is_("briefing_md", "null")
                .lte("created_at", cutoff)
                .order("created_at", desc=True)
                .limit(1)
                .execute())
        return (res2.data[0]["briefing_md"] or None) if res2.data else None
    except Exception:
        return None


def compute_editor(triage: dict, theses: list[dict], critiques: list[dict],
                   prev_briefing: str | None = None) -> str:
    _cross_check_devil_conviction(theses, critiques)
    return C.call(P.editor_user(triage, theses, critiques, prev_briefing=prev_briefing),
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
        _log(f"[triage] run {rid}: {len(clusters)} Cluster aus {len(rows)} Items (max_clusters={_triage_max_clusters(len(rows))})")
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
                  lambda r: {"devils_advocate": compute_devil(
                      (r.get("theses") or {}).get("theses", []),
                      analyses=(r.get("analysis") or {}).get("analyses", []),
                  )})


def stage_editor():
    run = _latest("editor")
    if not run:
        _log("[editor] keine briefing_runs-Zeile mit status=editor"); return
    rid = run["id"]
    try:
        prev = _fetch_prev_briefing()
        md = compute_editor(run.get("triage") or {},
                            (run.get("theses") or {}).get("theses", []),
                            (run.get("devils_advocate") or {}).get("critiques", []),
                            prev_briefing=prev)
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
    # Post-briefing track-record update: score past theses vs price action (best-effort)
    try:
        from agents.score_past_calls import write_track_record as _score
        summary = _score(days=60)
        _log(f"[editor] {summary}")
    except Exception as score_err:
        _log(f"[editor] score_past_calls non-fatal error: {score_err}")


def stage_coverage_qc(run_id: str | None = None, open_tickets: bool = True,
                      max_tickets: int = 5):
    """Post-briefing QC: detect big events missed by the delivered briefing.

    Finds the latest done run (or a specific run_id), runs coverage_qc.analyze(),
    optionally opens Paperclip Coverage-Bug issues for each gap, and persists results
    to the coverage_qc table. Prints a summary line to stdout so n8n can log it.
    """
    import fund_skills.coverage_qc as qc  # local import: optional dependency

    t = client().table("briefing_runs")
    if run_id:
        data = t.select("*").eq("id", run_id).limit(1).execute().data
    else:
        data = t.select("*").eq("status", "done").order("created_at", desc=True).limit(1).execute().data
    if not data:
        _log("[coverage_qc] no done briefing run found"); return

    run = data[0]
    _log(f"[coverage_qc] analyzing run {run['id']}")
    result = qc.analyze(run)
    tickets = qc.open_tickets(result, max_tickets) if open_tickets else []
    qc.persist(result, tickets)

    gap_count = len(result["gaps"])
    ticket_count = sum(1 for tk in tickets if tk.get("issue"))
    summary = (f"Coverage-QC run {str(run['id'])[:8]}: "
               f"{result['items_scanned']} items, {result['big_events']} big events, "
               f"{gap_count} gaps, {ticket_count} tickets filed")
    _log(f"[coverage_qc] {summary}")
    print(summary)


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
    critiques = compute_devil(th, analyses=analyses.get('analyses', [])); _log(f"[pipeline] devil ok")
    cr = critiques.get("critiques", critiques) if isinstance(critiques, dict) else critiques
    prev = _fetch_prev_briefing()
    md = compute_editor({"clusters": clusters}, th, cr, prev_briefing=prev)
    print(md)


def main():
    ap = argparse.ArgumentParser(description="AI/Tech Fund — Agenten-Pipeline")
    ap.add_argument("stage", choices=["triage", "analyst", "thesis", "devil",
                                       "editor", "coverage_qc", "pipeline"])
    ap.add_argument("--window", type=int, default=24, help="Zeitfenster in Stunden (triage/pipeline)")
    ap.add_argument("--run-id", help="briefing_runs ID (coverage_qc only)")
    ap.add_argument("--no-tickets", action="store_true", help="skip Paperclip ticket creation (coverage_qc)")
    ap.add_argument("--max-tickets", type=int, default=5)
    args = ap.parse_args()

    if args.stage == "triage":       stage_triage(args.window)
    elif args.stage == "analyst":    stage_analyst()
    elif args.stage == "thesis":     stage_thesis()
    elif args.stage == "devil":      stage_devil()
    elif args.stage == "editor":     stage_editor()
    elif args.stage == "coverage_qc":
        stage_coverage_qc(run_id=getattr(args, "run_id", None),
                          open_tickets=not getattr(args, "no_tickets", False),
                          max_tickets=getattr(args, "max_tickets", 5))
    elif args.stage == "pipeline":   pipeline(args.window)


if __name__ == "__main__":
    main()
