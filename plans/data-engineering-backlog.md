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
- 2026-05-22 — HED-129 (DE Loop): **`_telegram_alert` stdlib-only — `requests` dep removed**
  (`ingestion/run_ingest.py`, pushed `0f73556`). The Telegram alert function used `import requests`
  inside a try/except, violating the no-extra-deps principle from HED-125. If `requests` is absent
  from the live venv the alert silently no-ops. Replaced with `urllib.request.Request` (stdlib) —
  same pattern as `fetch_url`. Verified `python3 -c "from ingestion import run_ingest"` passes.
- 2026-05-22 — HED-129 (DE Loop): **FundingNewsAdapter AI/Tech relevance filter** (`ingestion/sources_aitech.py`,
  `1df8646` — done by prior session). Added `_FUNDING_RELEVANCE_RE` to drop off-topic consumer/lifestyle
  items (fragrance tech, beauty booking, kids streaming) from TechCrunch Startups / VentureBeat feeds.
  Result: 27 → 15 funding_news items per run; purely AI/Tech/Semis/cloud/energy signals pass through.
- 2026-05-22 — HED-125 (DE Loop): **native stdlib `fetch_url`/`fetch_json` — hard macro-agent dependency removed**
  (`ingestion/adapters.py`, `ingestion/sources_aitech.py`, `ingestion/test_dedup.py`, pushed `7072a16`).
  Closes the NEXT-CYCLE CANDIDATE from HED-121: every HTTP adapter previously called `m.fetch_url`/`m.fetch_json`
  on the macro module, so when the optional overlay was absent `_m()` returned `None` → `AttributeError` per
  adapter (caught) → near-zero items, even though the fetches are generic HTTP. Added self-contained
  `urllib`-based `fetch_url(url, headers?, timeout)` → text ("" on fail) and `fetch_json(...)` → parsed JSON
  (None on fail), gzip-aware, **stdlib-only on purpose** (no `requests` runtime dep added to the live venv).
  Routed all 20 adapter HTTP calls through them via sed; macro `_m()` retained ONLY for the genuinely
  macro-gated bits (NewsAPI key getattr @1284, X/XGraphQLAdapter @1434, SOURCE_RELIABILITY overlay).
  **Verified macro-ABSENT (this env has no ~/macro-agent):** `collect()` → 737 items across 14 adapters,
  0 errors (only arXiv 0 = live source HTTP 429, transient); dedup (10) + watchlist-sync (6) tests pass;
  new offline guardrail asserts `fetch_url`/`fetch_json` return ""/None without raising on an unreachable host.
  Note: leftover `m = _m()` in ~13 adapters is now harmless (cached None, ignored) — left in place to avoid a
  risky bulk edit; candidate cleanup for a later cycle.
- 2026-05-22 — HED-121 (DE Loop): **gitignore `*.db/*.sqlite*` + independent e2e verification of macro-fallback robustness**
  (`.gitignore`, pushed `f15c4dd`). I independently diagnosed the same critical bug the CIO fixed in
  `c3cdc96` (Zyklus 49b, ~7 min earlier): `_load_macro()` raised `SystemExit` (a `BaseException`) when the
  macro-agent overlay was absent → bypassed `collect()`'s per-adapter `except Exception` and aborted the
  WHOLE run with no output/alert. **My adapters.py/run_ingest.py fix converged byte-identical to the CIO's,
  so the only NET new change I shipped was the `.gitignore` safeguard** (stray local `fund.db` was untracked
  AND un-ignored → fund-data commit risk; now `*.db/*.sqlite*` ignored). Verified end-to-end on top of the
  CIO's fix: `build_adapters()`→15 adapters (was SystemExit), `run_once(dry_run)` completes with 6 items +
  degraded-adapter summary instead of total abort, py_compile clean.
  ⚠ **LOOP-CONVERGENCE NOTE:** DE loop (HED-121) and CIO master loop (HED-117 Zyklus 49b) picked the SAME
  improvement within minutes — duplicated diagnosis effort. Going forward, before building, check
  `git log origin/main -5` for a just-shipped CIO fix on the same target.
  ✅ **NEXT-CYCLE CANDIDATE — DONE in HED-125** (native `fetch_url/fetch_json`, pushed `7072a16`).
  📌 **NEW NEXT-CYCLE CANDIDATE (LOW):** ~13 now-dead `m = _m()` assignments remain in `sources_aitech.py`
  (adapters that used `m` only for fetching). Harmless (cached None) but dead; remove surgically — keep the
  live ones at NewsAPI @1284 + X adapter @1434. Low value, do only when touching those adapters anyway.
- 2026-05-22 — HED-117 (CIO Zyklus 49): **coverage_qc: buyback + dividend patterns; graceful macro fallback; better degraded-adapter alert**
  (`agents/coverage_qc.py`, `agents/prompts.py`, `ingestion/adapters.py`, `ingestion/run_ingest.py`).
  (1) Two new BIG_EVENT_PATTERNS (11→13 total): `buyback/high` catches "$50B share repurchase
  program" / "authorizes new buyback"; `dividend/medium` catches "declares special dividend of
  $3.00/share" / "increases quarterly dividend" / "initiates a dividend". 8/8 new pattern tests
  pass. Previously both events were completely invisible to coverage QC despite being common
  for AAPL/META/MSFT.
  (2) `triage_user()` earnings_calendar description updated with new consensus-estimate format
  from Zyklus 48 ("est. EPS $X.XX, rev $X.XB") and instruction to carry estimates into the
  cluster 'why' for downstream actual-vs-consensus comparison.
  (3) `_load_macro()` no longer calls SystemExit if macro-agent is absent — degrades gracefully
  with a warning and _macro_missing flag. Missing optional overlay was killing entire ingest runs.
  (4) Telegram adapter-degradation alert now covers ALL degraded adapters (errored + silent zeros)
  with per-adapter detail lines; fixed bug where silent_zeros were added to errors after the check.
  All 6 tests pass. Pushed: `8996511` + `c3cdc96`.
- 2026-05-22 — HED-117 (CIO Zyklus 48): **EarningsCalendarAdapter: add consensus EPS + revenue estimates**
  (`ingestion/sources_aitech.py`). Items previously showed "[AVGO] Earnings in 12 days (2026-06-03)"
  with no benchmark — analyst had to know consensus from memory. Now:
  "[AVGO] Earnings in 12 days (2026-06-03) — Broadcom Inc.; est. EPS $2.39, rev $22.1B".
  Pulls `Earnings Average` and `Revenue Average` from `yf.Ticker.calendar` (same dict already
  fetched for the date — zero extra API calls). Graceful: absent fields → est_suffix="" →
  previous format preserved. Live-verified: AVGO $2.39/$22.1B, MRVL $0.79/$2.4B, DELL $2.95/$35.7B,
  CRM $3.13/$11.1B, SNOW $0.32/$1.3B, CRWD $1.07/$1.4B. All 6 tests pass. Pushed: `a77eaec`.
- 2026-05-22 — HED-117 (CIO Zyklus 47): **Form 4: add total dollar value to insider trade summaries**
  (`ingestion/sources_aitech.py`). `_summarize_form4()` showed shares×VWAP but not total $.
  A CEO buying 10,000 shares at $219 ($2.2M conviction buy) and a director buying 200 shares
  ($44K routine nibble) looked equally significant to triage. New `_fmt_dollar()` helper
  prepends the aggregate open-market dollar volume to the signal line:
  "OPEN-MARKET BUY $2.2M — P open-market buy +10,000 @ $219.51; holds 50,000 after".
  Only fires for discretionary P (buy) and S (sell) codes — grants/exercises/tax
  withholding are unaffected. Triage can now tier insider-trade importance by magnitude
  without arithmetic ($1M+ = importance 4; $100K-$1M = importance 3; <$100K = 2).
  All 6 tests pass. Pushed: `17c1a35`.
- 2026-05-22 — HED-117 (CIO Zyklus 46): **n8n: fix QC node ordering (critical regression fix)**
  (`n8n/ai_tech_briefing.workflow.json`). Zyklus-43 introduced a production bug: Editor→QC→Telegram
  sent QC JSON stdout to the CEO instead of the German briefing. Fixed: Editor→Telegram→QC so
  coverage_qc runs post-delivery as a side-effect. Pushed: `993f246`.
- 2026-05-22 — HED-117 (CIO Zyklus 45): **TRIAGE_SYSTEM: add ITEM_REF ACCURACY self-verification step**
  (`agents/prompts.py`). Systematic mis-indexing in triage item_refs degraded analyst grounding —
  observed in run 11a62db6: Meta-layoffs cluster cited a Codex tweet + CAD repo while the real
  layoff item sat under a different cluster. New rule: before outputting item_refs, verify each
  index mentions the cluster's primary ticker/company/synonym; remove mismatches; empty list >
  wrong list; cite 1-3 representative indices only. 6/6 tests pass. Pushed: `f3a11de`.
- 2026-05-22 — HED-117 (CIO Zyklus 44): **coverage_qc: fix env-var loading + API defaults for production**
  (`fund_skills/coverage_qc.py`, `.env.example`). Two silent failures in production ticket-filing:
  (1) `CFG = dotenv_values(...)` ignored `PAPERCLIP_API_KEY` injected via SSH env → tickets never filed.
  Fix: `CFG = {**dotenv_values(...), **os.environ}` so runtime vars override .env.
  (2) `PAPERCLIP_API_BASE` defaulted to `http://127.0.0.1:3100` → wrong host in any non-local run.
  Fix: default to `https://paperclip.hedgingalpha.com`.
  (3) `PAPERCLIP_COMPANY_ID` now has hardcoded fallback so it never silently skips ticket creation.
  Added Paperclip vars to `.env.example`. All 6 tests pass. Pushed: `694d16a`.
- 2026-05-22 — HED-106 (DE-Loop Zyklus 42): **coverage_qc: quarterly_results + foreign_filing patterns**
  (`agents/coverage_qc.py`, `agents/test_pipeline.py`). `earnings_surprise` pattern required
  "beats/misses estimates" language — missed TSM-style 6-K quarterly reports ("revenue of NT$839B,
  up 41.6% YoY"). Two new BIG_EVENT_PATTERNS (9→11 total):
  (1) `quarterly_results` (high): matches "Q1 2026 revenue/results", "reports quarterly earnings",
  "fourth quarter results", "annual revenue" — catches plain periodic earnings reports.
  (2) `foreign_filing` (high): matches "[EDGAR 6-K Foreign Issuer Report]" / "[EDGAR 20-F...]" — any
  6-K or 20-F missed by triage auto-triggers a coverage-bug ticket.
  13 new tests added (analyst_action, exec_change, quarterly_results x4, foreign_filing x3, negative).
  All tests pass. Pushed: `37fa742`.
- 2026-05-22 — HED-106 (DE-Loop Zyklus 41): **Smart 6-K text extraction — skip SEC header, extract press release**
  (`ingestion/sources_aitech.py`). Previous 6-K extraction fell back to first 400 chars of stripped
  HTML, which always returned SEC boilerplate (Form 6-K header, address, Exchange Act references).
  New `_extract_6k_text()`: (1) finds CFO/CEO signature block to locate end of header; (2) searches
  after header for dateline pattern (e.g. "HSINCHU, Taiwan, May 15, 2026") marking embedded press
  release; (3) exhibit-99.1 fallback: extracts exhibit title from Exhibits table for exhibit-only 6-Ks
  (e.g. "ASML discloses 2026 AGM results"), trimmed at SIGNATURES; (4) final fallback for long tails.
  Live-verified on 3 real filings: TSM Vanguard sale (embedded PR) ✓, TSM Q1 financial statements
  (exhibit description) ✓, ASML AGM results (exhibit description) ✓. All tests pass. Pushed: `cba85db`.
- 2026-05-22 — HED-106 (DE-Loop Zyklus 40): **SEC 6-K and 20-F for foreign issuers TSM/ASML/ARM**
  (`ingestion/watchlist.py`, `ingestion/sources_aitech.py`, `agents/prompts.py`).
  TSM (Taiwan), ASML (Netherlands), and ARM (Cayman) are foreign private issuers — they file
  6-K (material events + quarterly results, equivalent to 8-K) and 20-F (annual report,
  equivalent to 10-K). All three were completely dark in the EDGAR adapter. Live SEC check
  confirmed active recent filings: TSM 6-Ks on 2026-05-15/2026-05-12, ARM 2026-05-06,
  ASML 2026-04-23. Changes: (1) "6-K", "6-K/A", "20-F" added to EDGAR_FORMS; (2) sec_6k
  (rel=0.93) and sec_20f (rel=0.96) in SOURCE_RELIABILITY; (3) _edgar_form_meta() mappings;
  (4) text extraction: 20-F uses _extract_10q_text(is_10k=True); 6-K uses _extract_8k_text()
  + paragraph fallback; (5) TRIAGE_SYSTEM + triage_user() guidance (ALWAYS include, importance 4-5).
  All tests pass. Pushed: `3c8c80a`.
- 2026-05-22 — HED-106 (DE-Loop Zyklus 39): **SEC 10-Q and 10-K added to EDGAR adapter**
  (`ingestion/watchlist.py`, `ingestion/sources_aitech.py`, `agents/prompts.py`).
  10-Q (quarterly) and 10-K (annual) earnings filings were completely dark in the pipeline —
  EDGAR_FORMS only covered 8-K, Form 4, and 13D/G. These are the highest-signal periodic SEC docs:
  when a watchlist company files its 10-Q, the MD&A section contains official revenue/EPS figures
  and any guidance revision. Added: (1) "10-Q"/"10-K" to EDGAR_FORMS; (2) `sec_10q`/`sec_10k` to
  SOURCE_RELIABILITY at 0.97 (highest in pipeline); (3) `_extract_10q_text()` with Item-2/Item-7
  MD&A regex + revenue-mention fallback; (4) `_edgar_form_meta()` mappings; (5) text extraction
  wired into EDGARAdapter.fetch() (try/except isolated, 30s timeout); (6) 10-Q/10-K triage guidance
  in both TRIAGE_SYSTEM and triage_user() (importance 5, category=earnings, always include).
  TRIAGE_SYSTEM importance rubric also updated to mention 10-Q/10-K at importance 4-5.
  Verified: form meta, extraction (Item2/Item7/fallback), dedup+sync tests pass. Pushed: `b6dd3b6`.
- 2026-05-22 — HED-106 (DE-Loop Zyklus 38): **coverage_qc: analyst_action + exec_change patterns**
  (`agents/coverage_qc.py`). Added 2 new event types to BIG_EVENT_PATTERNS (was 7, now 9):
  (1) `analyst_action` (medium): upgrades/downgrades/initiations/target raises/cuts — matches Yahoo Finance
  RSS upgrade language e.g. "Goldman Sachs initiates NVDA at Buy", "raises price target for GOOGL".
  (2) `exec_change` (high): CEO/CFO/CTO/COO departures and appointments — matches 8-K 5.02 items
  ("Exec Departure/Appointment") + press-release text e.g. "Satya Nadella appointed CEO".
  Both are market-moving events with zero prior coverage-gap detection. 12/12 pattern tests pass.
  Pushed: `b230a09`.
- 2026-05-22 — HED-106 (DE-Loop Zyklus 37): **FRED macro overlay: VIX + SP500 + NASDAQ added**
  (`ingestion/sources_aitech.py`). Added VIXCLS (CBOE VIX), SP500, NASDAQCOM to `FREDMacroAdapter.SERIES`.
  All three are daily series (free via fredgraph.csv, no API key), fresh within 24h.
  VIX gives analyst a live risk-regime signal (>20=elevated fear, >30=crisis onset); SP500/NASDAQ give
  market-context for AI/Tech thesis positioning (momentum, risk-on/off). Added "index" format kind
  (comma-sep float) for equity index levels. Live test: VIX=17.44 (Δ-3.4%), SP500=7,445.72 (Δ+0.2%),
  NASDAQ=26,293.10 (Δ+0.1%). FRED adapter now covers 10 macro series (was 7). Pushed: `74f0ee7`.
- 2026-05-22 — HED-104 (DE-Loop Zyklus 36): **Single source of truth for SEC watchlist-name filter + sync guardrail**
  (`ingestion/watchlist.py`, `ingestion/sources_aitech.py`, `ingestion/test_watchlist_sync.py`). First ran a
  live reachability+freshness sweep of ALL 15 RSS feeds (TECH/FUNDING/PRESS_WIRE/ENERGY) — every one is
  **200 + fresh (≤24h)**, incl. `crunchbase_news` which now returns 200/10 items (the Zyklus-35 403 was a
  transient Cloudflare gate, no fix needed). So no dead-feed work this cycle. Instead fixed a latent
  dedup/maintenance hazard: `SECBroadEventsAdapter._WATCHLIST_NAMES` was a hardcoded 26-name frozenset copied
  from `TICKERS`. A future ticker add (e.g. the pending S5-energy / S3-expansion universe changes) that forgets
  to also edit that copy would make the off-watchlist 8-K sweep STOP skipping that company → it emits a
  duplicate of the richer EDGARAdapter item (wastes premium SEC triage budget; silent). Now derived from a new
  `WATCHLIST_NAME_FRAGMENTS` dict (single floor) in watchlist.py, and `test_watchlist_sync.py` asserts
  `set(keys) == set(TICKERS)` + the adapter set is the derived set + skip-logic spot-checks (NVIDIA Corp skipped,
  Cohere not). Future ticker adds now fail the test loudly instead of leaking dupes. Verified: both test suites
  green (sync 6/6, dedup 8/8), package imports clean, skip-set size 26. Pushed: `2f06991` (origin/main).
- 2026-05-22 — HED-102 (DE-Loop Zyklus 35): **Dead PressWire feeds fixed — GlobeNewswire restored, BusinessWire removed**
  (`ingestion/watchlist.py`). Live reachability sweep of all RSS feeds surfaced that BOTH
  `PRESS_WIRE_RSS_FEEDS` entries were silently dead → `PressWireAdapter` had been contributing
  **0 items every cycle** (the per-feed try/except swallows the failure, so no error logged):
  (1) `globenewswire_tech` (`.../subjectcode/SC/typeofnews/PressRelease`) returned **HTTP 400** —
  "SC" is not a valid subjectcode. Replaced with the Technology *industry* feed
  (`https://www.globenewswire.com/RssFeed/industry/9576-Technology/feedTitle/Technology`) —
  verified status 200, 20 fresh items, **18/20 pass the AITECH filter**: AMD EPYC "Venice" ramp,
  AMD $10B Taiwan ecosystem, ASML buyback, Applied Materials/Broadcom, POET $400M financing,
  Skyworks/Qorvo M&A, Ambarella. (Rejected `subjectcode/22` alt: mostly Nordic managerial-
  transaction filings — Danske Bank/ISS A/S noise.)
  (2) `businesswire_tech` (`feed.businesswire.com/rss/home/?rss=G22`) now returns a **1001-byte
  empty stub (0 items)** — BusinessWire deprecated anonymous RSS; probed several feed codes +
  industry endpoints, all 0-item stubs or 403. Removed as dead weight (cf. wsj_tech Zyklus 34).
  **Follow-up:** `crunchbase_news` (FUNDING_RSS) returns 403 (Cloudflare/UA gate) — candidate
  for a future cycle (needs a working UA or alternative rounds source). Verified: config parses,
  dedup tests green, live filter yield. Pushed: `c8c2502`.
- 2026-05-22 — HED-99 (DE-Loop Zyklus 34): **Dead/broken TECH_RSS feeds fixed: wsj_tech removed, wired_ai URL corrected**
  (`ingestion/watchlist.py`). Closes the follow-up flagged in Zyklus 33. Two feeds were dead weight:
  (1) `wsj_tech` (feeds.a.dj.com/rss/RSSWSJD.xml) — verified status 200 but **frozen at 2025-01-27**
  (16 months stale); RSS_LOOKBACK_DAYS dropped 100% of its items every cycle → removed from config.
  (2) `wired_ai` (added Zyklus 32) — the tag slug `artificial-intelligence` **404'd silently** (returned
  None, contributing 0 items since it was added). Correct slug is `ai`:
  `https://www.wired.com/feed/tag/ai/latest/rss` — verified status 200, fresh items (2026-05-22).
  Live end-to-end test (urllib-stubbed macro bridge, since macro-agent isn't present locally):
  **56 items / 7 feeds, malformed=0**; wired_ai now contributes 8 fresh AI items/cycle
  (e.g. "Can OpenAI's 'Master of Disaster' Fix AI's Reputation Crisis?"), wsj_tech absent.
  Net: real coverage gain (Wired AI live for the first time) + dead-feed cleanup. Pushed: PENDING.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 33): **TechRSSAdapter freshness filter (RSS_LOOKBACK_DAYS)**
  (`ingestion/sources_aitech.py`). TechRSSAdapter was the ONLY RSS adapter with no date
  cutoff — every other RSS family (Funding/Energy/PressWire/Fed/BLS/Yahoo) already filters
  by `RSS_LOOKBACK_DAYS`. Without it, stale feed items were ingested every 30-min cycle as
  "fresh" (fetched_at=now), burning triage slots. Live isolation test surfaced the concrete
  harm: **`wsj_tech` (feeds.a.dj.com/rss/RSSWSJD.xml) is frozen at Jan-2025** and was pumping
  8 sixteen-month-old articles into raw_items every cycle — now correctly dropped. The 6 live
  feeds (techcrunch_ai, arstechnica, theverge_ai, mit_tech_review, cnbc_tech, theregister)
  keep all fresh items: 48 items, malformed=0. Items without a parseable date are kept
  (coverage > precision, identical to FundingNewsAdapter). dedup tests still green; 17 adapters
  build. **Follow-up noted:** `wsj_tech` is a dead/frozen feed and `wired_ai`
  (wired.com/feed/tag/artificial-intelligence/latest/rss, added Zyklus 32) fetch-fails (None) —
  both candidates for removal/replacement in a future cycle; the lookback fix neutralizes
  wsj_tech's stale output regardless. Pushed: `7d7f216`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 32): **The Register + Wired AI added to TECH_RSS_FEEDS**
  (`ingestion/watchlist.py`). TECH_RSS_FEEDS covered 6 feeds (TechCrunch AI, Ars Technica,
  The Verge AI, MIT Tech Review, WSJ Tech, CNBC Tech). Two editorial gaps remained:
  (1) The Register — enterprise/cloud/chip-manufacturing coverage; hyperscaler capex,
  datacenter builds, GPU/CPU competitive dynamics from the enterprise lens (NVDA/AMD/INTC/TSMC).
  (2) Wired AI — AI policy, regulation, safety, foundation-model breakthroughs; bridges
  MIT Tech Review (academic) and TechCrunch (startup). No new adapter code — TechRSSAdapter
  already handles both RSS (<item>) and Atom (<entry>). Per-feed isolation preserves
  robustness. Pushed: `1a41051`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 31): **Evidence truncation fix: 300→400 chars for high-rel items in analyst stage**
  (`agents/run.py`). Zyklus 30 fixed triage display (triage_user) to show 400 chars for
  items with rel>=0.85. The evidence list built in compute_triage() — passed to the
  analyst stage — still hard-coded 300 chars for all items, meaning analyst clusters
  received truncated 8-K snippets, earnings results, and Fed releases despite the triage
  fix. Applied the same rel>=0.85 → 400-char logic to the evidence comprehension.
  Tested 6/6 cases correct. Pushed: `226043f`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 30): **Triage item text limit: 300→400 chars for high-reliability sources**
  (`agents/prompts.py`). Triage truncated all items to 300 chars regardless of source
  reliability. High-reliability primary sources (SEC 8-K at rel=0.95, earnings_result
  at 0.88, Fed/BLS at 0.90-0.92) extract 400 chars of content and previously lost the
  final 100 chars in triage formatting. Items with rel >= 0.85 now get 400-char limit;
  generic editorial/social items (rel < 0.85) stay at 300. Covers 9 high-reliability
  source types. No new inputs needed — pure quality improvement on existing pipeline.
  Pushed: `01fc685`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 29): **Press wire adapter (BusinessWire + GlobeNewswire)**
  (`ingestion/sources_aitech.py`, `ingestion/watchlist.py`, `ingestion/adapters.py`).
  Zero press wire coverage existed — official company press releases (earnings releases,
  product launches, partnership announcements, guidance updates) arrived hours after
  editorial coverage and before the 8-K hits EDGAR. New `PressWireAdapter` follows
  the `FundingNewsAdapter` pattern: per-feed try/except isolation, RSS_LOOKBACK_DAYS
  cutoff, URL dedup across feeds. AI/tech relevance gate (`AITECH_KEYWORDS` +
  `NOTABLE_PRIVATE_PLAYERS`) filters out non-AI corporate PRs from the broad sector
  feeds. source=`press_wire`, reliability=0.78 (company-authored primary source,
  above editorial tech news 0.60, below SEC filings). Feeds: BusinessWire Technology
  + GlobeNewswire PressRelease. Registered as "Press Wire" in `build_adapters()`.
  Tested keyword filter: 8/8 correct. Pushed: `8940394`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 28): **Earnings-result detection in Yahoo Finance items**
  (`ingestion/sources_aitech.py`, `ingestion/watchlist.py`, `agents/prompts.py`).
  Actual earnings beats/misses arriving via Yahoo Finance RSS were indistinguishable
  from generic financial news (source=yahoo_finance, rel=0.72). Added `_EARNINGS_RESULT_RE`
  regex covering beat/miss/top/exceed/fell-short patterns, quarterly-EPS header format
  ("Q2 EPS:"), and factual revenue/profit move patterns. Detection runs before the
  analyst-action check (priority order: earnings_result > analyst_action > yahoo_finance).
  Matching headlines get `source="earnings_result"`, `reliability=0.88`, and
  `[Earnings · TICKER]` prefix. Added `earnings_result: 0.88` to `SOURCE_RELIABILITY`.
  Added EARNINGS RESULTS blocks to both `TRIAGE_SYSTEM` and `triage_user()`:
  always importance 4-5, category='earnings', miss=5, beat=4-5. Tested: 17/17
  classification cases correct (11 positive, 6 negative). Pushed: `4ab548e`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 26): **Analyst-action detection in Yahoo Finance items**
  (`ingestion/sources_aitech.py`, `ingestion/watchlist.py`, `agents/prompts.py`).
  Analyst upgrades, downgrades, and PT changes arrived as generic `yahoo_finance`
  items (rel=0.72) — triage had no way to distinguish them from general news. Added
  `_ANALYST_ACTION_RE` regex covering upgrade/downgrade/raises-PT/lowers-PT/initiates-
  coverage/reiterates/maintains/overweight/underweight patterns. In
  `YahooFinanceTickerAdapter.fetch()`: matching headlines get `source="analyst_action"`,
  `reliability=0.85`, and `[Analyst · TICKER]` prefix. Non-matching items unchanged.
  Added `analyst_action` to `SOURCE_RELIABILITY`. Added ANALYST ACTIONS block to
  `triage_user()`: importance 3-5, category='sentiment', cluster by ticker.
  Tested: 10/10 headline classification correct. Pushed: `9a59e5a`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 25): **Triage macro-signal guidance for Fed/BLS**
  (`agents/prompts.py`). MacroFedAdapter (Zyklus 23) and MacroBLSAdapter (Zyklus 24) ship
  Fed/BLS items into triage, but triage had no instructions for handling them — risking
  drop or generic clustering without thesis links. Added MACRO SIGNALS block to both
  `TRIAGE_SYSTEM` and `triage_user()`: fed_macro/bls_macro items = thesis risk factors
  (not trade signals); importance tiering (rate decision = 4-5, CPI/jobs = 3-4, routine
  speech = 2-3); required AI/Tech link (rate path → capex → MSFT/GOOGL/AMZN/META spend,
  NVDA/ANET/VRT demand). Parallel to how earnings_calendar has special handling.
  Pushed: `15fa04c`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 24): **BLS macro adapter**
  (`ingestion/sources_aitech.py`, `ingestion/adapters.py`, `ingestion/watchlist.py`).
  Triage had Fed policy signals (Zyklus 23) but not the underlying economic data
  (CPI, PPI, jobs) that forces Fed action. New `MacroBLSAdapter` fetches the BLS
  latest-releases RSS (`bls.gov/feed/bls_latest.rss`): CPI, PPI, JOLTS, payrolls,
  GDP advance estimates. Same pattern as `MacroFedAdapter`. Source key `bls_macro`,
  reliability=0.92 (official gov statistics). Registered as "BLS Macro" in
  `build_adapters()`. Pushed: `cf19950`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 23): **Federal Reserve macro adapter**
  (`ingestion/sources_aitech.py`, `ingestion/adapters.py`, `ingestion/watchlist.py`).
  Zero macro context in pipeline. Fed rate decisions directly affect AI capex thesis:
  higher rates raise data-center financing costs and tighten hyperscaler capex budgets.
  New `MacroFedAdapter` fetches two official Fed RSS feeds: `press_monetary.xml` (FOMC
  rate decisions, policy statements) and `press_speeches.xml` (Fed chair / governor
  speeches, forward guidance). Pattern identical to `EnergyNewsAdapter` (per-feed
  try/except isolation, RSS_LOOKBACK_DAYS=3). Source key `fed_macro`, reliability=0.90
  (official primary source). Registered as "Fed Macro" between Energy/Power and Yahoo
  Finance. Syntax verified + unit-tested. Pushed: `8e4c805`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 22): **Per-item-type reliability for 8-K filings**
  (`ingestion/sources_aitech.py`). All 8-K items shared reliability=0.95 regardless of
  content quality — a boilerplate Item 9.01 (exhibit attachment) competed with Item 2.02
  (earnings) for the same triage cluster budget. Added `_8K_ITEM_RELIABILITY` dict (16 item
  codes) and `_8k_item_reliability()` function. Extended `_extract_8k_text()` to return
  `(snippet, item_num, label)` 3-tuple so `item_num` is available at the call site.
  `EDGARAdapter.fetch()` applies the per-item override when present; falls back to base
  `sec_8k` reliability for unlisted items. Effective ranges: Earnings/Acquisition/Change-of-
  Control → 0.97; Reg FD → 0.85; Financial Statements attachment → 0.75. Form 4 / 13D/G
  unaffected. Unit-tested: 4/4 cases correct. Pushed: `376ae54`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 21): **8-K Item-type classification**
  (`ingestion/sources_aitech.py`). 8-K filings appeared as "[EDGAR 8-K] NVDA..."
  regardless of event type — triage could read "Item 2.02" but had no structured label
  to distinguish earnings from acquisitions, exec departures, or change-of-control.
  Added `_8K_ITEM_LABELS` dict (21 SEC Reg S-K item codes) and `_8k_item_label()`;
  extended `_extract_8k_text()` to capture the Item number group and return
  `(snippet, label)` tuple. EDGARAdapter now emits structured labels:
  "[EDGAR 8-K:Earnings Results]", "[EDGAR 8-K:Acquisition/Disposal]",
  "[EDGAR 8-K:Exec Departure/Appointment]", etc. Unknown items fall back to
  "Material Event" — no regression. Form 4 / 13D/G paths unaffected (`item_type=""`).
  Unit-tested: 4/4 label mappings correct. Pushed: `4a2bf3e`.
- 2026-05-22 — HED-89 (DE-Loop Zyklus 20): **8-K primary-document text extraction**
  (`ingestion/sources_aitech.py`). EDGAR 8-K items showed only filing-metadata description ("8-K")
  — triage had no content to reason about. Added `_extract_8k_text()`: fetches the primary 8-K HTML,
  strips scripts/styles/tags/entities, finds the first `Item \d+\.\d+` paragraph, returns ≤400 chars.
  Wired into `EDGARAdapter.fetch()` for `form=="8-K"` analogously to the Form 4 XML enrichment
  (try/except wrapped — any failure falls back to metadata description, adapter isolation intact).
  Live-tested on NVDA Q1-2026 earnings 8-K (filed 2026-05-20, SEC 200, compliant UA):
  "Item 2.02 Results of Operations... NVIDIA issued a press release announcing its results for the
  quarter ended April 26, 2026…" Pushed: `58d862f`.
  idea "Briefing clarity / 8-K content".
- 2026-05-22 — HED-89 (DE-Loop Zyklus 19): **SC 13D/13G beneficial-ownership filings in EDGAR**
  (`ingestion/watchlist.py`, `ingestion/sources_aitech.py`). EDGARAdapter only captured 8-K + Form 4.
  Activist stakes (SC 13D) and >5% ownership changes (SC 13G/A) are high-signal catalysts for
  watchlist names — completely dark before. Added the four 13D/G form types to `EDGAR_FORMS` and
  generalized the form→(source,label) mapping into new module-level `_edgar_form_meta()` so 13D/G get
  source `sec_13dg` (reliability 0.93, new in SOURCE_RELIABILITY) + proper labels
  ("Aktivisten-Stake 13D" / "Passive >5%-Beteiligung 13G") instead of being mislabeled "8-K".
  Live-tested against SEC (200, compliant UA): real 13G/A filings for NVDA/AMD/TSM/ASML parse and
  normalize correctly; current 5-day window had none (rare filings) but the path is verified. The
  Form-4 ownership-XML enrichment only fires for form=="4", so 13D/G skip it safely. Pushed: `a6b5fd0`.
  idea "Data quality / SEC coverage — activist & >5% stakes".
- 2026-05-22 — HED-64 (DE-Loop Zyklus 18): **Chunked upsert_raw_items — 100 rows/batch**
  (`ingestion/db.py`). Large runs (600+ items) sent in one HTTP call risk hitting Supabase
  PostgREST payload/timeout limits. Split into batches of `_UPSERT_CHUNK=100` rows; inserted
  count accumulated across chunks so callers get the correct total. No behaviour change for
  normal runs; silently safer for large ones. Pushed: `4b5d9d0`.
- 2026-05-22 — HED-77 (DE-Loop Zyklus 15): **Dead-adapter Telegram alert**
  (`ingestion/run_ingest.py`). When the dead-adapter health check (Zyklus 13) detects silent-zero
  adapters, `_telegram_alert()` now fires a Telegram notification so the operator is paged
  without polling logs. Silently no-ops when TELEGRAM_BOT_TOKEN/CHAT_ID are absent.
  Closes the monitoring loop opened by Zyklus 13. Pushed: `807843d`.
- 2026-05-22 — HED-64 (DE-Loop Zyklus 17): **YahooFinanceTickerAdapter description enrichment**
  (`ingestion/sources_aitech.py`). Yahoo Finance RSS `<description>` fields contain substantive article
  summaries (e.g. AMD CEO Taiwan capacity ramp, IREN 5-year NVDA contract context) not just headlines.
  Now appends via `_rss_desc` helper (200-char, total 450). Live-tested: 3/3 enriched. Pushed: `77cfce8`.
- 2026-05-22 — HED-64 (DE-Loop Zyklus 16): **Description enrichment for FundingNews + EnergyNews adapters**
  (`ingestion/sources_aitech.py`). Both adapters were title-only while TechRSSAdapter gained descriptions
  in Zyklus 15. Extracted `_rss_text`/`_rss_desc` as module-level helpers (DRY). Now all three RSS adapter
  families include article summaries (150-char) so triage sees content context, not just headlines.
  Live-tested: Crunchbase descriptions parse correctly. Pushed: `b2fd3c0`.
- 2026-05-22 — HED-64 (DE-Loop Zyklus 15): **CNBC Tech RSS + TechRSSAdapter description enrichment**
  (`ingestion/watchlist.py`, `ingestion/sources_aitech.py`). Added CNBC Technology feed (first-mover
  on earnings reactions, analyst calls, M&A). TechRSSAdapter: extract `<description>`/`<summary>`
  and append to item text (150-char truncation) so triage reasons about content not just headlines.
  Bumped per-feed item limit 5→8 (6 feeds × 8 = 48 vs 30). Unified html.unescape(). Pushed: `0e0c780`.
- 2026-05-22 — HED-78 (CIO-Loop Zyklus 18): **Fix prev-briefing window for Δ seit gestern**
  (`agents/run.py`). Cycle 17 bug: `_fetch_prev_briefing()` was returning the most recent
  done run (~30 min ago), making the delta section trivial. Fixed to query the 20-36h window
  (yesterday's briefing); falls back to any run >4h old. Pushed: `0013660`.
- 2026-05-22 — HED-78 (CIO-Loop Zyklus 17): **Previous briefing context for Δ seit gestern**
  (`agents/run.py`, `agents/prompts.py`). `_fetch_prev_briefing()` queries the last done
  `briefing_runs.briefing_md` and passes it to `editor_user()` as a `YESTERDAY'S BRIEFING`
  block (truncated 1500 chars). Editor can now write a grounded delta instead of guessing.
  No-op when no prior run exists (first-run safe). Pushed: `c087187`.
- 2026-05-22 — HED-78 (CIO-Loop Zyklus 16): **Earnings-diese-Woche section in CEO briefing**
  (`agents/prompts.py` `editor_user()`). Extracts earnings_calendar evidence from triage clusters
  (regex on `[TICKER] Earnings in N days (YYYY-MM-DD)`) and pre-populates a
  `## 📅 Earnings diese Woche` section (≤8 entries, ≤7-day items first). Section is omitted
  entirely when no earnings items present. Char budget raised 1200→1400. Pushed: `d8ed9bb`.
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
- 2026-05-22 — HED-77 (DE-Loop Zyklus 14): **EDGAR lookback 3→5 days + arXiv cs.AR** (`ingestion/watchlist.py`).
  `EDGAR_LOOKBACK_DAYS`: 3→5 — late-Friday SEC filings were potentially missed on Monday runs (3-day window could truncate at Friday midnight). `ARXIV_CATEGORIES`: added `cs.AR` (computer architecture — GPU micro-architecture, AI accelerator papers, custom ASIC design). Directly relevant to NVDA/AMD moat thesis. `ARXIV_MAX`: 30→35. Pushed: `ff249f1..457c192`. idea "Coverage / EDGAR + arXiv hardware".
- 2026-05-22 — HED-77 (DE-Loop Zyklus 13): **Dead-adapter health check** (`ingestion/run_ingest.py`).
  Adapters returning 0 items without a logged error now emit `⚠ DEAD ADAPTERS` warning and are written into the `errors` dict → stored in `ingestion_runs` DB row. Previously a dead feed blended in with the count table; now immediately visible in run output and queryable from DB. Pushed: `63e845e..ff249f1`. idea "Data quality / silent failure detection".
- 2026-05-22 — HED-77 (DE-Loop Zyklus 12): **HN_MIN_POINTS 80→60** (`ingestion/watchlist.py`).
  Semiconductor ticker queries (TSMC/ASML/AMD, added Zyklus 11) rarely hit 80pts. Material stories at 60-79pts were being dropped at source. Correct tradeoff: over-capture at ingest + triage AI filters noise (already reliability-weighted since Zyklus 5). Pushed: `99d0e3a..501139a`. idea "Data quality / HN floor tuning".
- 2026-05-22 — HED-77 (DE-Loop Zyklus 11): **HN queries: semiconductor + TSMC + ASML + AMD** (`ingestion/watchlist.py`).
  HN_QUERIES 12→16. TSMC/ASML/AMD are top-5 watchlist positions with zero prior HN coverage. HN routinely surfaces capex stories ("TSMC Arizona expansion"), export controls ("ASML EUV to China"), and competitive dynamics ("AMD MI300X inference") before mainstream press. Pushed: `062b3b0..99d0e3a`. idea "Coverage / semiconductor HN".
- 2026-05-22 — HED-77 (DE-Loop Zyklus 10): **GitHub topics: robot-learning + embodied-ai added** (`ingestion/watchlist.py`).
  Pairs with arXiv cs.RO (Zyklus 8). GITHUB_TOPICS 6→8: `robot-learning` (sim-to-real, RL for robots — Isaac Lab, MuJoCo, Lerobot; signals NVDA Jetson/GPU demand from robotics) + `embodied-ai` (LLM + physical-action interfaces; tracks convergence of foundation models with robot control). GitHub repos lag arXiv by weeks but reveal which codebases practitioners actually adopt. Pushed: `7b18383..aedccad`. idea "Coverage / embodied AI GitHub".
- 2026-05-22 — HED-77 (DE-Loop Zyklus 9): **NOTABLE_PRIVATE_PLAYERS expanded 35→70** (`ingestion/watchlist.py`).
  Added 35 new 2024-2026 AI companies: Harvey AI ($300M legal AI), Cognition/Devin ($175M software agents), Sierra AI ($175M), Poolside ($500M code AI), ElevenLabs ($180M voice AI), Magic ($465M), Imbue ($200M), Writer ($200M enterprise AI), Glean ($260M enterprise search), Luma AI, Pika Labs, Suno, Stability AI, Inflection (MSFT acquisition), Character.ai, H Company, Black Forest Labs, Nous Research, Moonshot AI/Kimi, 01.ai, StepFun. These are the companies most likely to file S-1s, get acquired, or raise material rounds — now auto-surfaced by SEC registrations adapter and funding feeds even without generic AITECH_KEYWORDS match. Pushed to origin/main: `0e0c780..22f3c68`. idea "Coverage / private players".
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
