# Data-Engineering Dauer-Verbesserung — Backlog

Owner: Data-Engineer (`78b79ccb-7011-4753-b282-584d6136bfb6`), reports to CIO.
Driver: **self-assigned routine `384b40df-7ddc-44f9-a2c2-bbea271ebe63` ("🔧 Data-Engineering Loop", alle 30 Min, parent HED-53)** — established by HED-56 so DE works in parallel without waiting on CIO delegation. (The CIO master loop `8b07245b`/HED-53 still runs separately; this DE loop is scoped to data quality / coverage / pipeline robustness only.) Each cycle: pick THE ONE most valuable
improvement, build end-to-end, test, commit to local `main` in `/srv/ai-tech-fund` (= live;
the pipeline runs from there). Log here (Ideas + Done). No duplicates. Blocker → note + move on.

Guardrails (COMPANY.md): destructive DB/infra + real money need CEO approval; everything else autonomous.

## Standing constraints / known blockers
- **GO-LIVE PATH (CORRECTED 2026-05-22, HED-42 + CIO verification — supersedes the old "/srv is live" claim):**
  The LIVE briefing pipeline AND dashboard run from **`/root/ai-tech-fund`** (root-owned, unreadable to the
  `paperclip` uid), which tracks **`origin/main`** (`git@github.com:PJOrab/paperclip-fund.git`). Proof:
  `n8n/ai_tech_briefing.workflow.json` + `scripts/run_ingest.sh` invoke `/root/ai-tech-fund/venv/bin/python`;
  `/var/www/html/fund/index.html` is root-owned and rebuilt on schedule from /root. **Editing or committing in
  `/srv/ai-tech-fund` does NOT make anything live.** Improvements only deploy when they land on `origin/main`
  AND `/root` pulls it. ⇒ Treat "land it on origin/main" as the definition of done for any go-live work.
- **`/srv` and `origin/main` are UNRELATED histories** — no common ancestor (24 local-only vs 12 origin commits,
  verified `git merge-base` empty). So a plain `git push origin HEAD:main` from /srv is rejected (non-fast-forward)
  and also fails on corrupt/root-owned objects. **DO NOT force-push** origin/main (would destroy its 12 commits;
  destructive infra ⇒ CEO approval). Correct path = fresh-clone origin/main → `git am` / cherry-pick the new
  improvement diffs on top → push (fast-forward). This is the established workaround (see CIO memory git_push_access).
- **`/srv/.git/objects` partly root-owned** → `git fetch`/`push` from /srv fail ("insufficient permission ... .git/objects").
  Operator unblock: `chown -R paperclip:paperclip /srv/ai-tech-fund/.git/objects` (root only — agent cannot).
- **Open operator-dependent unknown:** does `/root/ai-tech-fund` auto-`git pull origin/main` before each run/rebuild?
  Unverifiable by agents (/root unreadable). If it does NOT, even a correct push to origin/main will not deploy.
  Escalated to board — see HED-43 / approval.
- Real trades / money movement: never. CEO-approval gate.

## Ideas (prioritized)
- [b] **QC / Coverage-Gap-Check after each briefing** — compare delivered briefing vs. raw_items;
  any missed big event (IPO/S-1, funding, launch) → auto-open a coverage-bug ticket to DE. (HIGH)
- [c] **Thesis track-record scoring (score-past-calls)** — score past theses (see CIO memory
  theses_2026-05-21, theses_2026-05-21_hed9, devil_verdicts) vs. real price action; surface hit-rate.
  Fund's moat = conviction calibration. (HIGH)
- [a] **Go-live loop** — stable `/srv/paperclip-fund` checkout + venv, cron `git pull`, SKILL paths.
  GitHub-publish piece gated by HED-18 (operator). Local-commit go-live already works. (MED, partly blocked)
- Dashboard: thesis track-record view, sector views, alerts, charts. (MED)
- Data quality: dedup tuning, per-source reliability scoring, missing-field backfill. (MED)
- More adapters / sources: sector coverage gaps, additional fundings/launches feeds. (MED)

## In-flight (parallel workstreams — HED-37 cycle, 2026-05-22 ~00:06 UTC)
- HED-48 — Sektor-Ansicht (S1-S6 Ticker-Kacheln) in dashboard/build.py (Analyst Carl)
- HED-49 — Score-past-calls Entry-Baselines vorausladen für 2026-06-04-Routine (SrAnalyst Pip)
- HED-50 — S3-Thesen-Finalisierung (Strategist Magnus)
- HED-52 — Red-Team MSFT/AMZN Attention-Gap (Scepticer Edward)
- HED-46 — S3-Briefing-Zusammenfassung PLTR/ORCL (Editor Mark, in_progress)
- HED-23 — Dashboard Responsive-Fixes (DataEng, blocked → deploy-bridge)
- HED-44 — HED-23 Live Design-Review (Designer Felix, blocked)

## In-flight (parallel workstreams — HED-33 cycle, 2026-05-21)
- HED-23 — Dashboard Responsive-Fixes + Design-Tokens (DE 78b79ccb) [carryover]
- HED-29 — Dashboard design: thesis track-record view (Designer Felix 9109d2a0) [carryover]
- HED-30 — Briefing clarity v2: tighten structure per CEO-Praeferenzen (Editor Mark 8b605ddb) [carryover]
- HED-31 — Devil-verdict calibration + red-team rubric (Edward 13d5ef76) [carryover]
- HED-32 — Sector/thematic taxonomy + thesis mapping (Magnus 6d24d173) — CIO-approved, finalizing
- HED-35 — S3 AI-Software thesis initiation PLTR/ORCL/NOW/CRWD (Pip 7d1517dd) — closes S3 in-universe gap from HED-32
- HED-36 — Attention-gap analysis AMD/ASML/MSFT/AMZN (Carl 35bd17bf) — uncovered in-universe names in S1/S2

## Board-level open question
- **S5 Energy/Power universe expansion** — we name power-grid strain as the key risk to the AI-capex
  thesis but have no in-universe ticker to express it. Surfaced by HED-32; pending board confirmation
  (request_confirmation on HED-32, board-addressed). Decision = whether to widen the investable universe.

## Done
- 2026-05-22 — CIO-Master-Loop (HED-63 Zyklus): **Designer-UX-Loop angestoßen.** Gap: DE hatte
  eine eigene fortlaufende Schleife (HED-64), Designer Felix nicht — entgegen dem Mandat
  („Designer: Dashboard-UX … richte ihm eine EIGENE fortlaufende Arbeitsschleife ein"). Child
  HED-67 an Felix: eigene 30-Min-Routine selbst anlegen (self-only-Auth, CIO kann das nicht) +
  erste UX-Verbesserung sofort starten. Briefing-IC-Rollen (Devil/Editor/Senior Analyst/Carl)
  bewusst NICHT mit Off-Cycle-Make-Work belegt (Budgetdisziplin, continuous_improvement_loops).
  In-Review-Queue (HED-39 Track-Record-Section, HED-54 ARM-Watchlist) korrekt board-gated geparkt.
- 2026-05-22 — HED-56 (DE-Loop Zyklus 1): **Dedup-Key stabilisiert** (`ingestion/adapters.py`).
  `content_hash` war `md5(text[:200]+source)`; HN/GitHub-Text trägt eine volatile Metrik
  (Punkte/Stars), die jeden 30-Min-Fetch hochtickt → gleiche Story bekam jeden Zyklus einen
  neuen Hash und re-ingestierte 8-10x/Tag (siehe feed_noise_patterns). Neu: Key = kanonische
  URL (Fragment/Tracking-Params raus, http/https gefaltet, trailing slash weg), sonst
  normalisierter Text (Badge `[HN Npts]`/`[GitHub ★N]` gestrippt) + source. Test
  `ingestion/test_dedup.py` (stdlib-only) grün. Auf origin/main gepusht: `ddb5a7a..6011ba7`
  (commit `6011ba7`). Live erst nach Operator-Deploy-Fix (HED-34). idea „Data quality / dedup-tuning".
- 2026-05-21 — HED-25: Thesis track-record scoring (score-past-calls) shipped (Pip). idea [c].
- 2026-05-21 — HED-26: Funding/VC-news ingestion adapter (TechCrunch + Crunchbase RSS) shipped (DE).
- 2026-05-21 — HED-27: Coverage-QC automation post-briefing gap-check shipped (Carl). idea [b] cont.
- 2026-05-21 — HED-13: SEC registration/IPO adapter (S-1/S-1A/F-1/424B) + off-watchlist triage +
  extensible watchlist + standing rule. Closed the SpaceX S-1 coverage gap (HED-12).
- 2026-05-21 — HED-20 (Zyklus 1): **(b) QC/Coverage-Gap-Check** wired end-to-end.
  - `fund_skills/coverage_qc.py` already existed; added `coverage_qc` stage to `agents/run.py`
    and a new `QC: Coverage-Check` n8n SSH node (fires after Telegram delivery).
  - Live test on run 11a62db6: 611 items, 39 big events, **6 gaps detected** (Exa $250M,
    Hark $700M Series A, Mercury $5.2B, Cohere model launch, etc.) — report-only pass.
  - Files live in `/srv/ai-tech-fund` (git commit pending VPS chown fix — see known blockers).
