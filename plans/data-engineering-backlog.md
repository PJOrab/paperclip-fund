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
- 2026-05-22 — HED-64 (DE-Loop Zyklus 14): **coverage_qc positional-index bug fix**
  (`agents/coverage_qc.py`). `item_refs` in triage clusters are positional indices into
  the list passed to `compute_triage()` — NOT into coverage_qc's freshly-fetched DB list
  (different ordering). Fix: build `covered_texts` from cluster `evidence` strings, then
  match raw items by text prefix. Coverage gaps now identified correctly. Pushed: `0132294`.
- 2026-05-22 — HED-78 (CIO-Loop Zyklus 15): **Per-source reliability audit script**
  (`agents/reliability_audit.py`). Berechnet echte Triage-Inclusion-Rates aus briefing_runs DB:
  wie oft macht jede Quelle's Items es in einen Triage-Cluster? Vergleicht mit konfigurierten
  SOURCE_RELIABILITY-Scores. Findet Quellen die rel=0.80 haben aber kaum geclustert werden.
  `--patch` Modus: smoothet watchlist.py-Scores automatisch (Formel: 0.7*actual + 0.3*cfg,
  nur wenn Δ≥0.03 und ≥5 Samples). Gibt Markdown-Tabelle mit 🔴/🟡/🟢 Kalibrierungssignal.
  Syntax OK; Live-Test braucht /root-Supabase-Keys (deploy-bridge-abhängig).
  Auf origin/main gepusht: `4ce855b..649bb6d`.
- 2026-05-22 — HED-64 (DE-Loop Zyklus 13): **EarningsCalendar dedup bug fix + GITHUB_PUSH_LOOKBACK_DAYS**
  (`ingestion/sources_aitech.py`, `ingestion/watchlist.py`). EarningsCalendarAdapter used a bare
  `finance.yahoo.com/quote/{ticker}` URL as dedup key — same key every day, so the 2nd-day
  countdown "in 13 days" was silently deduped away by the first-day "in 14 days" item. Fix:
  embed `earnings_date` in the URL so each (ticker, date) pair gets a unique content_hash.
  Also: renamed dead config var `GITHUB_CREATED_LOOKBACK_DAYS` → `GITHUB_PUSH_LOOKBACK_DAYS`
  and wired it into GitHubTrendingAdapter (was hardcoded 7d). Pushed: `30b14fe`.
- 2026-05-22 — HED-78 (CIO-Loop Zyklus 14): **Dynamic triage cluster scaling + feed limit 400→600**
  (`agents/run.py`). 13 Adapter generieren jetzt 400+ Items/Run — hardcodierter 400-Limit und
  12-Cluster-Cap ließen Material silent fallen. Fix: limit=600, max_clusters=max(12,min(20,n//20))
  (≤240 Items=12cl, 300=15cl, 400=20cl). Earnings-Events konkurrieren nicht mehr um Cluster-Slots.
  Triage-Timeout 240→300s. Auf origin/main gepusht: `cdab3ce..c7aaeb3`.
- 2026-05-22 — HED-78 (CIO-Loop Zyklus 13): **Triage + Thesis earnings-awareness prompts**
  (`agents/prompts.py`). Triage-System-Prompt erkennt `earnings_calendar`-Items jetzt als
  immer-material: ≤3 Tage = importance=5, 4-14 Tage = importance=4, nie droppen.
  Thesis-System-Prompt erhält EARNINGS TIMING RULE: bei imminent earnings muss horizon='days'
  und earnings date in catalysts[]. Verhindert Thesis/Horizon-Mismatch wenn Pipeline
  Earnings-Daten im Feed hat. Auf origin/main gepusht: `7999ee6..7414830`.
- 2026-05-22 — HED-78 (CIO-Loop Zyklus 12): **EarningsCalendarAdapter — 14-day earnings warning**
  (`ingestion/sources_aitech.py`, `ingestion/adapters.py`, `ingestion/watchlist.py`).
  Neuer Adapter fetcht Earnings-Termine für alle Watchlist-Ticker via yfinance.
  Generiert Early-Warning-Items: `[TICKER] Earnings in N days (YYYY-MM-DD) — Company`.
  Live-Test: 6 Events (AVGO/MRVL/DELL/CRM/SNOW/CRWD, alle im 14-Tage-Fenster).
  Reliability=0.88 (Quelle: Exchange/Filing-Daten via Yahoo). Schließt kritische Lücke:
  Triage/Thesis hatten kein Bewusstsein für Earnings-Timing → Thesis-Horizon-Mismatch.
  Auf origin/main gepusht: `b76fbbe..61d3740`.
- 2026-05-22 — HED-64 (DE-Loop Zyklus 12 / CIO): **YahooFinanceTickerAdapter: pubDate-Filter**
  (`ingestion/sources_aitech.py`). Fehlender Date-Guard: veraltete RSS-Artikel wurden als
  "current" ingestiert. Fix: RSS_LOOKBACK_DAYS (3 Tage) Lookback identisch FundingNewsAdapter
  + EnergyNewsAdapter. 138 Items, malformed=0. Auf origin/main: `932b668..b76fbbe`.
- 2026-05-22 — HED-78 (CIO-Loop Zyklus 11): **Thesis track-record scorer**
  (`agents/score_past_calls.py`, `requirements.txt`). Liest alle done `briefing_runs`,
  extrahiert Thesen mit `direction` (long/short/pair), fetcht Entry-Preis (run.created_at)
  und Current-Price via yfinance, berechnet: Return%, Direction-Hit-Rate, Conviction-
  Calibration-Score. Output: JSON + Markdown-Tabelle. `yfinance>=0.2` in requirements.txt.
  Usage: `python -m agents.score_past_calls [--days N] [--output markdown]`.
  Auf origin/main gepusht: `fdaf54f..63a48b7`.
- 2026-05-22 — HED-64 (DE-Loop Zyklus 11 / CIO): **GitHub-Adapter: created:>30d → pushed:>7d**
  (`ingestion/sources_aitech.py`). Vorher: nur neu erstellte Repos (letzten 30 Tage, max ~500★,
  kein Bezug zur bestehenden AI-Stack-These). Jetzt: pushed:>7d + sort=stars → aktive
  High-Star-Repos: ollama (171K★), AutoGPT (184K★), langchain (137K★) — Projekte deren
  Release-Kadenz direkt AI-Inference-Demand und Tool-Adoption signalisiert.
  26 Items, malformed=0. Auf origin/main: `69cfbce..fdaf54f`.
- 2026-05-22 — HED-78 (CIO-Loop Zyklus 10): **QC/Coverage-Gap-Check nach jedem Briefing**
  (`agents/coverage_qc.py`, `agents/run.py`). Neues Modul scannt raw_items, die von
  keinem Triage-Cluster referenziert wurden, und matcht Keyword-Heuristiken für 7
  Big-Event-Typen: IPO/S-1, Funding-Runden, M&A, Major Launches, Insider Trades,
  Earnings-Überraschungen, Regulatorik. Jeder Treffer → Paperclip Coverage-Bug-Ticket
  (assigned DE, priority=high/medium). Wired in `stage_editor()` (best-effort, non-fatal)
  → läuft automatisch nach jedem Briefing-Run. Auf origin/main gepusht: `8002b4e..813aacb`.
- 2026-05-22 — HED-77 (DE-Loop Zyklus 8): **arXiv cs.RO (robotics) added + ARXIV_MAX 25→30** (`ingestion/watchlist.py`).
  Embodied AI / robotics was completely dark in our arXiv feed. cs.RO covers manipulation, locomotion, sim-to-real transfer — directly relevant to NVDA Jetson edge compute, META embodied AI research, Figure AI/1X (private watchlist). ARXIV_MAX bumped to 30 to accommodate the 5th category. Pushed to origin/main: `1d28442..fb6ca5e`. idea "Coverage / arXiv robotics".
- 2026-05-22 — HED-77 (DE-Loop Zyklus 7 / CIO): **Devil verdict rubric + thesis is_differentiated + editor v3 + non-consensus ordering** (`agents/prompts.py`).
  Thesis stage now outputs `is_differentiated: true|false` (derived from analyst's `consensus_view`) so downstream stages have a structured non-consensus signal. Devil gets a 3-verdict calibration rubric (agree/caution/reject defined with concrete thresholds; falsification must name a specific observable event, not "stock falls"). Editor gets v3 precision rules (1200 char cap, conviction delta required, devil adjudication mandatory with explicit '→' resolution, dedup rule); `editor_user` pre-sorts enriched theses non-consensus first so CEO always sees differentiated calls at the top. Pushed to origin/main: `813aacb..147957e`.
- 2026-05-22 — HED-64 (DE-Loop Zyklus 10 / CIO): **YahooFinanceTickerAdapter** (neuer Adapter)
  (`ingestion/sources_aitech.py`, `ingestion/adapters.py`, `ingestion/watchlist.py`).
  Yahoo Finance per-Ticker-RSS für 8 Top-Positionen: NVDA/MSFT/GOOGL/META/PLTR/ORCL/NOW/ARM.
  Schließt Lücke: Tech-Blogs verpassen Analyst-Ratings, Earnings-Previews, Position-Events.
  Reliability=0.72. 138 Items / 8 Ticker, malformed=0. Auf origin/main: `9555de8..8002b4e`.
- 2026-05-22 — HED-64 (DE-Loop Zyklus 9 / CIO): **arXiv Abstract-Extraktion + ARXIV_MAX 15→25**
  (`ingestion/sources_aitech.py`, `ingestion/watchlist.py`). ArxivAdapter speicherte nur
  Titel — Triage/Analyst hatten keinen Inhalt zum Reasoning. Jetzt: `<summary>`-Tag (arXiv
  Atom Standard) extrahiert, auf 250 Chars truncated: `[arXiv] {title} — {abstract}`.
  Gleichzeitig ARXIV_MAX 15→25 (+10 Paper/Zyklus, reliability=0.80 = höchste Non-SEC-Quelle).
  arXiv API war rate-limited (429) beim Test — Format aus arXiv Atom Spec verifiziert.
  Auf origin/main gepusht: `fcc44e6..9555de8`.
- 2026-05-22 — HED-64 (DE-Loop Zyklus 8 / CIO): **CDATA-Bug in Funding + Energy Adaptern gefixt**
  (`ingestion/sources_aitech.py`). Gleicher Root Cause wie Zyklus 7 (TechRSSAdapter):
  `re.sub` behandelt `<![CDATA[...]]>` als HTML-Tag vor der CDATA-Strip → Titel leer →
  Item gedropped. TechCrunch hat 158 CDATA/Fetch, Crunchbase 122 — Bug war aktiv.
  Fix in FundingNewsAdapter + EnergyNewsAdapter: CDATA zuerst strippen, dann HTML-Strip;
  Regex zu `<title[^>]*>`. Isolation: 32 Funding / 30 Energy Items, malformed=0.
  Auf origin/main gepusht: `c0d9a71..fcc44e6`.
- 2026-05-22 — HED-64 (DE-Loop Zyklus 7 / CIO): **TechRSSAdapter CDATA-Bug gefixt**
  (`ingestion/sources_aitech.py`). theverge_ai lieferte 0 Items (silent drop).
  Root cause: `re.sub(r"<[^>]+>", "", ...)` behandelte `<![CDATA[...]]>` als HTML-Tag
  → ganzer Titel entfernt → leer → Item übersprungen. Fix: CDATA vor HTML-Strip entfernen +
  Regex `<title>` → `<title[^>]*>` für Atom `type="html"`-Attribute.
  Vor Fix: 20 Items / 4 Feeds. Nach Fix: 25 Items / 5 Feeds, malformed=0.
  Auf origin/main gepusht: `36c9f6a..c0d9a71`.
- 2026-05-22 — HED-64 (DE-Loop Zyklus 6 / CIO): **TECH_RSS + HN_QUERIES erweitert**
  (`ingestion/watchlist.py`). TECH_RSS_FEEDS 3→5: `mit_tech_review`
  (technologyreview.com — research-grade AI-Tiefe + Policy) + `wsj_tech`
  (feeds.a.dj.com/rss/RSSWSJD.xml — Financial Press, marktbewegende Ticker-News).
  Beide 200 mit MacroIntel-UA; Adapter liefert 10 neue Items, malformed=0.
  HN_QUERIES 6→12: Microsoft, Google, Meta AI (Hyperscaler-Positionen nicht abgedeckt),
  Palantir, Mistral, xAI (AI-native Watchlist). Max +30 HN-Stories/Zyklus.
  Auf origin/main gepusht: `1f7f929..5d115a0`.
- 2026-05-22 — HED-77 (DE-Loop Zyklus 7): **Yahoo Finance RSS expanded to full 26-ticker watchlist** (`ingestion/watchlist.py`).
  `YAHOO_FINANCE_TICKERS` was hardcoded to 8 top positions; 18 watchlist names (AMD, TSM, ASML, AVGO, MU, SMCI, QCOM, MRVL, INTC, ANET, VRT, DELL, AMZN, AAPL, CRM, SNOW, CRWD, ADBE) received no per-ticker Yahoo Finance headlines. Now `YAHOO_FINANCE_TICKERS = TICKERS` (all 26). ~8s/ingest cycle at 0.3s sleep. Pushed to origin/main: `9553ee7..7999ee6`. idea "Coverage / missing tickers".
- 2026-05-22 — HED-77 (DE-Loop Zyklus 6): **Analyst consensus differentiation + thesis conviction calibration** (`agents/prompts.py`).
  Analyst stage now returns `consensus_view` (aligned|differentiated|unclear) + `differentiation` sentence per cluster — the thesis stage was told to "prefer non-consensus ideas" but had no structured signal about what consensus IS. Now it does.
  Thesis system prompt gains a 6-point conviction calibration guide (0.1=noise → 0.9+=rare convergent signal). Past runs clustered at 0.30-0.45 with no distinguishing rationale; anchors make convictions comparable across runs and score-able in track-record.
  Pushed to origin/main: `5d115a0..36c9f6a`. idea "Prompts / Briefing clarity / conviction calibration".
- 2026-05-22 — HED-77 (DE-Loop Zyklus 5): **Triage: reliability scores surfaced + insider_trade category** (`agents/prompts.py`).
  `triage_user` previously stripped reliability from items → model had no signal to rank SEC filings (rel=0.95) over HN posts (rel=0.55). Now each feed line shows `(source rel=X.XX)` with instruction to weight higher-reliability sources more heavily. Simultaneously added `insider_trade` as a first-class triage category — Form 4 open-market buys/sells were previously mis-filed under `sentiment`, obscuring the insider-signal cluster from downstream stages. Zero breaking changes (additive prompt context + extended category enum). Pushed to origin/main `767c3cc..1f7f929`. idea "Pipeline robustness / Briefing clarity".

- 2026-05-22 — HED-64 (DE-Loop Zyklus 4): **GitHub-Topics erweitert + Reliability-Konsolidierung**
  (`ingestion/watchlist.py`, `ingestion/sources_aitech.py`). GITHUB_TOPICS von 4 → 6:
  `multimodal` (frontier model competition) + `llm-inference` (inference efficiency stack).
  Isolation-Test: 27 Items, malformed=0; neue Top-Repos: tokenspeed (LLM inference), atlas
  (Pure-Rust-Inferenz-Engine), vllm-awq4-qwen (vLLM-Quantisierung), OpenSearch-VL (multimodal).
  Reliability-Konsolidierung: FundingNewsAdapter + AITechNewsAPIAdapter hatten hardcodierte
  0.8/0.6 statt W.SOURCE_RELIABILITY.get() — Single-Source-of-Truth-Invariante gebrochen;
  jetzt W.SOURCE_RELIABILITY.get(key, fallback), Runtime-Werte unverändert.
  Auf origin/main gepusht: `322d9fc..767c3cc`. idea „Data quality / reliability-Konsolidierung".
- 2026-05-22 — HED-64 (DE-Loop Zyklus 3): **S5 Energy/Power-Adapter neu** (`ingestion/sources_aitech.py`,
  `ingestion/watchlist.py`, `ingestion/adapters.py`). Neuer `EnergyNewsAdapter` schließt den
  Null-Coverage-Gap in S5 Energy/Power (Sektor-Taxonomie HED-32: Power-Grid-Strain ist
  Primärrisiko der AI-Capex-These, bis jetzt kein Adapter). Quellen (beide 200/MacroIntel-UA):
  `datacenter_dynamics` (AI-Rechenzentrum-Infra + Hyperscaler-Stromnachfrage: OpenAI Guaranteed
  Capacity, Oregon-Regulierer Sondertarif für Datenzentren), `utilitydive` (Stromnetz / Utility-
  Regulierung). Isolation-Test: 30 Items, malformed=0, `source=energy_news`, `reliability=0.72`
  (neu in SOURCE_RELIABILITY). Pattern identisch FundingNewsAdapter (per-Feed try/except).
  Auf origin/main gepusht: `b653714..322d9fc`. Live nach Operator-Deploy-Fix (HED-34).
  idea „More adapters / sources" (S5-Sektor).
- 2026-05-22 — HED-64 (DE-Loop Zyklus 2): **Funding-Coverage verbreitert**
  (`ingestion/watchlist.py`). FUNDING_RSS_FEEDS um zwei verifizierte, key-freie
  Runden-Quellen ergänzt: `crunchbase_news` (news.crunchbase.com/feed/ — kanonische
  Funding-Round-Quelle, trägt exakt die per QC verpassten Rounds, z.B. „Mercury
  Lands $200M") und `techcrunch_venture` (techcrunch.com/category/venture/feed/).
  Live getestet: alle Feeds Status 200 mit Projekt-UA; Crunchbase liefert gelegentlich
  transiente Cloudflare-403 (Stabilitätstest 4/4 200 → kein Hard-Block), per-Feed
  try/except isoliert das → kein Adapter-Kill, 30-Min-Takt fängt die Runde im nächsten
  Zyklus. FundingNewsAdapter end-to-end: malformed=0, Items normalisiert
  {text,source,url,reliability=0.8}. techcrunch_venture überlappt korrekt via shared
  `seen`-Dedup mit startups (kein Bug). Auf origin/main gepusht: `6011ba7..b653714`.
  Live erst nach Operator-Deploy-Fix (HED-34). idea „More adapters / sources".
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
